"""Download, SHA-256 verify, and extract pinned external tools into the managed folder.

All network downloads stream to temporary files inside ``<tools_dir>/.tmp/``, verify the full
SHA-256 digest before extracting, extract only the expected executable members (rejecting path
traversal and symlinks), and atomically replace binaries under ``<tools_dir>/bin/``.

Installed state is tracked in ``<tools_dir>/manifest.json`` so re-runs skip already-installed
tools whose pinned metadata and binary hashes still match. Passing ``archive_dir`` supports
offline installations from pre-downloaded archives with full SHA-256 verification.
"""

import contextlib
import hashlib
import json
import logging
import shutil
import stat
import tarfile
import tempfile
import zipfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Final, Literal

import httpx

from autoclipper.tools.discovery import managed_bin_dir
from autoclipper.tools.manifest import (
    ALL_TOOL_GROUPS,
    ArchiveMember,
    PlatformKey,
    ToolArtifact,
    ToolGroup,
    ToolSpec,
    detect_platform,
    get_tool_spec,
)

log = logging.getLogger(__name__)

MANIFEST_FILENAME: Final = "manifest.json"
MANIFEST_SCHEMA_VERSION: Final = "1"
_DOWNLOAD_CHUNK_BYTES: Final = 256 * 1024  # 256 KiB
_MAX_ARCHIVE_BYTES: Final = 512 * 1024 * 1024  # 512 MiB upper bound on any single download
_HTTP_TIMEOUT: Final = httpx.Timeout(connect=30.0, read=60.0, write=30.0, pool=30.0)

type InstallStage = Literal[
    "checking",
    "downloading",
    "verifying",
    "extracting",
    "done",
    "skipped",
    "failed",
]
type InstallOutcomeStatus = Literal["installed", "already_installed"]


class ToolInstallError(Exception):
    """Base error for managed tool installation failures."""


class ToolDownloadError(ToolInstallError):
    """Downloading an archive or locating an offline archive failed."""


class ChecksumMismatchError(ToolInstallError):
    """A downloaded or offline archive's SHA-256 digest did not match the pinned value."""

    def __init__(self, filename: str, *, expected: str, actual: str) -> None:
        self.filename = filename
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"SHA-256 mismatch for {filename}: expected {expected}, got {actual}. "
            "The file was discarded and not installed."
        )


class ToolArchiveError(ToolInstallError):
    """An archive was corrupt, unsafe, or missing an expected executable member."""


@dataclass(frozen=True, slots=True)
class InstallProgress:
    """Progress update emitted while checking, downloading, verifying, or extracting a tool."""

    tool: ToolGroup
    label: str
    stage: InstallStage
    message: str
    bytes_downloaded: int | None = None
    bytes_total: int | None = None

    @property
    def fraction(self) -> float | None:
        """Download progress in ``[0.0, 1.0]`` when byte counts are known, else ``None``."""
        if self.bytes_downloaded is None or self.bytes_total is None or self.bytes_total <= 0:
            return None
        return min(1.0, max(0.0, self.bytes_downloaded / self.bytes_total))


type ProgressCallback = Callable[[InstallProgress], None]


@dataclass(frozen=True, slots=True)
class ToolInstallOutcome:
    """Result of installing or verifying one tool group."""

    tool: ToolGroup
    label: str
    version: str
    status: InstallOutcomeStatus
    source_name: str
    license: str
    binaries: tuple[Path, ...]


def manifest_path(tools_dir: Path) -> Path:
    """Return the path to the managed tools manifest file inside ``tools_dir``."""
    return tools_dir / MANIFEST_FILENAME


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_DOWNLOAD_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def _load_manifest(tools_dir: Path) -> dict[str, dict[str, object]]:
    path = manifest_path(tools_dir)
    if not path.is_file():
        return {}
    try:
        raw: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict) or raw.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        return {}
    tools = raw.get("tools")
    if not isinstance(tools, dict):
        return {}
    cleaned: dict[str, dict[str, object]] = {}
    for key, value in tools.items():
        if isinstance(key, str) and isinstance(value, dict):
            cleaned[key] = dict(value)
    return cleaned


def _save_manifest(tools_dir: Path, tools_map: Mapping[str, Mapping[str, object]]) -> None:
    tools_dir.mkdir(parents=True, exist_ok=True)
    target = manifest_path(tools_dir)
    payload = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "tools": dict(tools_map),
    }
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=tools_dir,
            prefix=f".{MANIFEST_FILENAME}.",
            suffix=".tmp",
            delete=False,
        ) as tmp:
            json.dump(payload, tmp, indent=2, sort_keys=True)
            tmp.write("\n")
            tmp_path = Path(tmp.name)
        tmp_path.replace(target)
        tmp_path = None
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)


def _is_already_installed(
    spec: ToolSpec,
    tools_dir: Path,
    manifest: Mapping[str, Mapping[str, object]],
) -> tuple[Path, ...] | None:
    """Return installed binary paths when manifest and on-disk hashes match ``spec``."""
    entry = manifest.get(spec.tool)
    if entry is None:
        return None
    if entry.get("version") != spec.version or entry.get("platform") != spec.platform.key:
        return None
    expected_artifacts = [art.sha256 for art in spec.artifacts]
    if entry.get("artifact_sha256s") != expected_artifacts:
        return None
    recorded_bins = entry.get("binaries")
    if not isinstance(recorded_bins, dict):
        return None
    bin_dir = managed_bin_dir(tools_dir)
    resolved: list[Path] = []
    for name in spec.binary_names:
        expected_hash = recorded_bins.get(name)
        if not isinstance(expected_hash, str):
            return None
        binary_path = bin_dir / name
        if not binary_path.is_file():
            return None
        try:
            if _sha256_file(binary_path) != expected_hash:
                return None
        except OSError:
            return None
        resolved.append(binary_path)
    return tuple(resolved)


def _normalize_member_path(raw_name: str) -> PurePosixPath:
    cleaned = raw_name.replace("\\", "/")
    if cleaned.startswith("/"):
        raise ToolArchiveError(f"archive entry has an absolute path: {raw_name!r}")
    parsed = PurePosixPath(cleaned)
    if ".." in parsed.parts:
        raise ToolArchiveError(f"archive entry attempts path traversal: {raw_name!r}")
    return parsed


def _matches_suffix(member_path: PurePosixPath, suffix: str) -> bool:
    posix = member_path.as_posix().lstrip("./")
    return posix == suffix or posix.endswith(f"/{suffix}")


def _extract_from_zip(
    archive_path: Path,
    members: Sequence[ArchiveMember],
    staging_dir: Path,
) -> dict[str, tuple[Path, str]]:
    try:
        archive = zipfile.ZipFile(archive_path, "r")
    except (OSError, zipfile.BadZipFile) as exc:
        raise ToolArchiveError(f"could not open zip archive {archive_path.name}: {exc}") from exc

    extracted: dict[str, tuple[Path, str]] = {}
    with archive:
        regular_entries: list[tuple[PurePosixPath, zipfile.ZipInfo]] = []
        for info in archive.infolist():
            if info.is_dir():
                continue
            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode):
                continue
            parsed = _normalize_member_path(info.filename)
            regular_entries.append((parsed, info))

        for wanted in members:
            matches = [
                info
                for parsed, info in regular_entries
                if _matches_suffix(parsed, wanted.member_suffix)
            ]
            if not matches:
                raise ToolArchiveError(
                    f"archive {archive_path.name} is missing expected binary "
                    f"{wanted.member_suffix!r}"
                )
            if len(matches) > 1:
                raise ToolArchiveError(
                    f"archive {archive_path.name} has multiple entries matching "
                    f"{wanted.member_suffix!r}"
                )
            target = staging_dir / wanted.binary_name
            digest = hashlib.sha256()
            try:
                with archive.open(matches[0], "r") as src, target.open("wb") as dst:
                    while chunk := src.read(_DOWNLOAD_CHUNK_BYTES):
                        dst.write(chunk)
                        digest.update(chunk)
            except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
                raise ToolArchiveError(
                    f"failed to extract {wanted.binary_name} from {archive_path.name}: {exc}"
                ) from exc
            extracted[wanted.binary_name] = (target, digest.hexdigest())
    return extracted


def _extract_from_tar_xz(
    archive_path: Path,
    members: Sequence[ArchiveMember],
    staging_dir: Path,
) -> dict[str, tuple[Path, str]]:
    extracted: dict[str, tuple[Path, str]] = {}
    try:
        with tarfile.open(archive_path, "r:xz") as archive:
            regular_entries: list[tuple[PurePosixPath, tarfile.TarInfo]] = []
            for info in archive.getmembers():
                if not info.isfile():
                    continue
                parsed = _normalize_member_path(info.name)
                regular_entries.append((parsed, info))

            for wanted in members:
                matches = [
                    info
                    for parsed, info in regular_entries
                    if _matches_suffix(parsed, wanted.member_suffix)
                ]
                if not matches:
                    raise ToolArchiveError(
                        f"archive {archive_path.name} is missing expected binary "
                        f"{wanted.member_suffix!r}"
                    )
                if len(matches) > 1:
                    raise ToolArchiveError(
                        f"archive {archive_path.name} has multiple entries matching "
                        f"{wanted.member_suffix!r}"
                    )
                target = staging_dir / wanted.binary_name
                digest = hashlib.sha256()
                src = archive.extractfile(matches[0])
                if src is None:
                    raise ToolArchiveError(
                        f"could not read {wanted.binary_name} from {archive_path.name}"
                    )
                with src, target.open("wb") as dst:
                    while chunk := src.read(_DOWNLOAD_CHUNK_BYTES):
                        dst.write(chunk)
                        digest.update(chunk)
                extracted[wanted.binary_name] = (target, digest.hexdigest())
    except ToolArchiveError:
        raise
    except (OSError, tarfile.TarError) as exc:
        raise ToolArchiveError(f"could not read tar.xz archive {archive_path.name}: {exc}") from exc
    return extracted


def _extract_artifact(
    artifact: ToolArtifact,
    archive_path: Path,
    staging_dir: Path,
) -> dict[str, tuple[Path, str]]:
    if artifact.archive_format == "zip":
        return _extract_from_zip(archive_path, artifact.members, staging_dir)
    return _extract_from_tar_xz(archive_path, artifact.members, staging_dir)


def _download_and_verify(
    spec: ToolSpec,
    artifact: ToolArtifact,
    *,
    tmp_dir: Path,
    client: httpx.Client,
    on_progress: ProgressCallback | None,
) -> Path:
    dest = tmp_dir / artifact.filename
    digest = hashlib.sha256()
    downloaded = 0
    total = artifact.size_bytes
    try:
        with client.stream("GET", artifact.url) as response:
            if response.status_code != 200:
                raise ToolDownloadError(
                    f"Download failed for {artifact.filename}: HTTP {response.status_code} from "
                    f"{spec.source_name}. Retry later or install offline with --from-dir."
                )
            header_len = response.headers.get("Content-Length")
            if header_len and header_len.isdigit():
                total = int(header_len)
            if on_progress is not None:
                on_progress(
                    InstallProgress(
                        tool=spec.tool,
                        label=spec.label,
                        stage="downloading",
                        message=f"Downloading {artifact.filename}...",
                        bytes_downloaded=0,
                        bytes_total=total,
                    )
                )
            with dest.open("wb") as handle:
                for chunk in response.iter_bytes(chunk_size=_DOWNLOAD_CHUNK_BYTES):
                    if not chunk:
                        continue
                    downloaded += len(chunk)
                    if downloaded > _MAX_ARCHIVE_BYTES:
                        raise ToolDownloadError(
                            f"Download for {artifact.filename} exceeded the size limit "
                            f"({_MAX_ARCHIVE_BYTES} bytes)."
                        )
                    handle.write(chunk)
                    digest.update(chunk)
                    if on_progress is not None:
                        on_progress(
                            InstallProgress(
                                tool=spec.tool,
                                label=spec.label,
                                stage="downloading",
                                message=f"Downloading {artifact.filename}...",
                                bytes_downloaded=downloaded,
                                bytes_total=total,
                            )
                        )
    except httpx.HTTPError as exc:
        dest.unlink(missing_ok=True)
        raise ToolDownloadError(
            f"Could not download {artifact.filename} from {spec.source_name}: {exc}. "
            "Check your network connection or install offline with --from-dir."
        ) from exc
    except BaseException:
        dest.unlink(missing_ok=True)
        raise

    if on_progress is not None:
        on_progress(
            InstallProgress(
                tool=spec.tool,
                label=spec.label,
                stage="verifying",
                message=f"Verifying SHA-256 for {artifact.filename}...",
                bytes_downloaded=downloaded,
                bytes_total=downloaded,
            )
        )
    actual_sha = digest.hexdigest()
    if actual_sha != artifact.sha256.lower():
        dest.unlink(missing_ok=True)
        raise ChecksumMismatchError(
            artifact.filename, expected=artifact.sha256.lower(), actual=actual_sha
        )
    return dest


def _verify_offline_archive(
    spec: ToolSpec,
    artifact: ToolArtifact,
    *,
    archive_dir: Path,
    on_progress: ProgressCallback | None,
) -> Path:
    candidate = archive_dir / artifact.filename
    if not candidate.is_file():
        raise ToolDownloadError(
            f"Offline archive not found: {candidate}. Download {artifact.url} and save it as "
            f"{artifact.filename} in {archive_dir} (run `autoclipper setup-tools --show-urls` "
            "to see all required files)."
        )
    if on_progress is not None:
        on_progress(
            InstallProgress(
                tool=spec.tool,
                label=spec.label,
                stage="verifying",
                message=f"Verifying offline archive {artifact.filename}...",
            )
        )
    actual_sha = _sha256_file(candidate)
    if actual_sha != artifact.sha256.lower():
        raise ChecksumMismatchError(
            artifact.filename, expected=artifact.sha256.lower(), actual=actual_sha
        )
    return candidate


def install_tool(
    spec: ToolSpec,
    *,
    tools_dir: Path,
    force: bool = False,
    archive_dir: Path | None = None,
    client: httpx.Client | None = None,
    on_progress: ProgressCallback | None = None,
) -> ToolInstallOutcome:
    """Install one pinned tool group into ``tools_dir``, skipping if already verified.

    Args:
        spec: Pinned release specification from `get_tool_spec`.
        tools_dir: Root managed tools directory (binaries go in ``<tools_dir>/bin/``).
        force: Re-download and reinstall even when the manifest and binary hashes already match.
        archive_dir: Optional directory containing pre-downloaded archives for offline install
            (SHA-256 checksums are still verified; no network calls are made).
        client: Optional `httpx.Client` for downloads (tests inject a mock/local client).
        on_progress: Optional callback invoked with `InstallProgress` updates.

    Returns:
        A `ToolInstallOutcome` indicating whether the tool was installed or already up to date.
    """
    if on_progress is not None:
        on_progress(
            InstallProgress(
                tool=spec.tool,
                label=spec.label,
                stage="checking",
                message=f"Checking {spec.label} ({spec.version})...",
            )
        )

    manifest = _load_manifest(tools_dir)
    if not force:
        existing = _is_already_installed(spec, tools_dir, manifest)
        if existing is not None:
            if on_progress is not None:
                on_progress(
                    InstallProgress(
                        tool=spec.tool,
                        label=spec.label,
                        stage="skipped",
                        message=f"{spec.label} {spec.version} is already installed.",
                    )
                )
            return ToolInstallOutcome(
                tool=spec.tool,
                label=spec.label,
                version=spec.version,
                status="already_installed",
                source_name=spec.source_name,
                license=spec.license,
                binaries=existing,
            )

    bin_dir = managed_bin_dir(tools_dir)
    tmp_root = tools_dir / ".tmp"
    bin_dir.mkdir(parents=True, exist_ok=True)
    tmp_root.mkdir(parents=True, exist_ok=True)

    owns_client = client is None and archive_dir is None
    active_client = (
        httpx.Client(follow_redirects=True, timeout=_HTTP_TIMEOUT) if owns_client else client
    )
    work_dir = Path(tempfile.mkdtemp(prefix=f"{spec.tool}-", dir=tmp_root))
    try:
        staged: dict[str, tuple[Path, str]] = {}
        for artifact in spec.artifacts:
            if archive_dir is not None:
                archive_file = _verify_offline_archive(
                    spec, artifact, archive_dir=archive_dir, on_progress=on_progress
                )
            else:
                assert active_client is not None
                archive_file = _download_and_verify(
                    spec,
                    artifact,
                    tmp_dir=work_dir,
                    client=active_client,
                    on_progress=on_progress,
                )
            if on_progress is not None:
                on_progress(
                    InstallProgress(
                        tool=spec.tool,
                        label=spec.label,
                        stage="extracting",
                        message=f"Extracting {artifact.filename}...",
                    )
                )
            extracted = _extract_artifact(artifact, archive_file, work_dir)
            staged.update(extracted)

        installed_paths: list[Path] = []
        binary_hashes: dict[str, str] = {}
        for name in spec.binary_names:
            src_path, bin_sha = staged[name]
            if spec.platform.os != "windows":
                src_path.chmod(0o755)
            dest_path = bin_dir / name
            try:
                src_path.replace(dest_path)
            except OSError as exc:
                raise ToolInstallError(
                    f"Could not write {dest_path}: {exc}. Close any running AutoClipper processes "
                    "using this binary and try again."
                ) from exc
            installed_paths.append(dest_path)
            binary_hashes[name] = bin_sha

        manifest[spec.tool] = {
            "version": spec.version,
            "platform": spec.platform.key,
            "source": spec.source_name,
            "source_url": spec.source_url,
            "license": spec.license,
            "artifact_sha256s": [art.sha256.lower() for art in spec.artifacts],
            "binaries": binary_hashes,
            "installed_at": datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        _save_manifest(tools_dir, manifest)

        log.info(
            "installed managed tool tool=%s version=%s platform=%s binaries=%d",
            spec.tool,
            spec.version,
            spec.platform.key,
            len(installed_paths),
        )
        if on_progress is not None:
            on_progress(
                InstallProgress(
                    tool=spec.tool,
                    label=spec.label,
                    stage="done",
                    message=f"Installed {spec.label} {spec.version}.",
                )
            )
        return ToolInstallOutcome(
            tool=spec.tool,
            label=spec.label,
            version=spec.version,
            status="installed",
            source_name=spec.source_name,
            license=spec.license,
            binaries=tuple(installed_paths),
        )
    except Exception as exc:
        if on_progress is not None:
            on_progress(
                InstallProgress(
                    tool=spec.tool,
                    label=spec.label,
                    stage="failed",
                    message=str(exc),
                )
            )
        raise
    finally:
        if owns_client and active_client is not None:
            active_client.close()
        shutil.rmtree(work_dir, ignore_errors=True)
        # Clean up .tmp if empty so the managed folder stays tidy.
        with contextlib.suppress(OSError):
            tmp_root.rmdir()


def install_tools(
    tools: Sequence[ToolGroup] = ALL_TOOL_GROUPS,
    *,
    tools_dir: Path,
    platform: PlatformKey | None = None,
    force: bool = False,
    archive_dir: Path | None = None,
    client: httpx.Client | None = None,
    on_progress: ProgressCallback | None = None,
) -> tuple[ToolInstallOutcome, ...]:
    """Install one or more pinned tool groups for ``platform`` into ``tools_dir``.

    Validates platform support for every requested tool up front before starting any downloads.
    """
    unique_tools: list[ToolGroup] = []
    for tool in tools:
        if tool not in unique_tools:
            unique_tools.append(tool)
    if not unique_tools:
        raise ValueError("at least one tool must be selected")

    target_platform = detect_platform() if platform is None else platform
    specs = [get_tool_spec(tool, target_platform) for tool in unique_tools]

    outcomes: list[ToolInstallOutcome] = []
    for spec in specs:
        outcomes.append(
            install_tool(
                spec,
                tools_dir=tools_dir,
                force=force,
                archive_dir=archive_dir,
                client=client,
                on_progress=on_progress,
            )
        )
    return tuple(outcomes)
