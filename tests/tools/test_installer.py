"""Tests for `autoclipper.tools.installer` and `autoclipper setup-tools` CLI.

All unit tests run against fake in-memory transports or a local loopback HTTP server, never real
remote hosts. A single `live` test verifies an upstream release URL is reachable and is deselected
by default.
"""

import hashlib
import io
import json
import sys
import tarfile
import threading
import zipfile
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from autoclipper.cli import ExitCode, app
from autoclipper.tools.discovery import resolve_tool
from autoclipper.tools.installer import (
    ChecksumMismatchError,
    InstallProgress,
    ToolArchiveError,
    ToolDownloadError,
    ToolInstallError,
    install_tool,
    install_tools,
    manifest_path,
)
from autoclipper.tools.manifest import (
    ArchiveFormat,
    ArchiveMember,
    PlatformKey,
    ToolArtifact,
    ToolSpec,
    UnsupportedPlatformError,
)

cli_runner = CliRunner()


def _make_zip_bytes(entries: dict[str, bytes], *, symlink_name: str | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
        if symlink_name is not None:
            info = zipfile.ZipInfo(symlink_name)
            info.external_attr = 0o120777 << 16
            zf.writestr(info, b"/etc/passwd")
    return buf.getvalue()


def _make_tar_xz_bytes(entries: dict[str, bytes], *, symlink_name: str | None = None) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:xz") as tf:
        for name, data in entries.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
        if symlink_name is not None:
            sym = tarfile.TarInfo(name=symlink_name)
            sym.type = tarfile.SYMTYPE
            sym.linkname = "/etc/passwd"
            tf.addfile(sym)
    return buf.getvalue()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _spec_for(
    *,
    tool: str = "ffmpeg",
    platform: PlatformKey | None = None,
    artifacts: tuple[ToolArtifact, ...],
) -> ToolSpec:
    plat = platform or PlatformKey("windows", "x86_64")
    return ToolSpec(
        tool=tool,  # type: ignore[arg-type]
        label="FFmpeg & FFprobe" if tool == "ffmpeg" else "TwitchDownloaderCLI",
        version="9.0.2" if tool == "ffmpeg" else "1.56.5",
        platform=plat,
        source_name="Fake Upstream",
        source_url="https://example.invalid/releases",
        license="GPL-3.0" if tool == "ffmpeg" else "MIT",
        artifacts=artifacts,
    )


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        del format, args


def _serve_directory(directory: Path) -> tuple[ThreadingHTTPServer, str]:
    handler = partial(_QuietHandler, directory=str(directory))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    return server, f"http://{host}:{port}"


def test_install_zip_from_local_http_server_is_verified_and_idempotent(tmp_path: Path) -> None:
    served_dir = tmp_path / "served"
    served_dir.mkdir()
    archive_bytes = _make_zip_bytes(
        {
            "ffmpeg-9.0.2-essentials_build/bin/ffmpeg.exe": b"fake-ffmpeg-binary",
            "ffmpeg-9.0.2-essentials_build/bin/ffprobe.exe": b"fake-ffprobe-binary",
            "ffmpeg-9.0.2-essentials_build/README.txt": b"docs",
        }
    )
    archive_name = "ffmpeg-9.0.2-essentials_build.zip"
    (served_dir / archive_name).write_bytes(archive_bytes)

    server, base_url = _serve_directory(served_dir)
    try:
        spec = _spec_for(
            platform=PlatformKey("windows", "x86_64"),
            artifacts=(
                ToolArtifact(
                    filename=archive_name,
                    url=f"{base_url}/{archive_name}",
                    sha256=_sha256(archive_bytes),
                    archive_format="zip",
                    size_bytes=len(archive_bytes),
                    members=(
                        ArchiveMember("ffmpeg.exe", "bin/ffmpeg.exe"),
                        ArchiveMember("ffprobe.exe", "bin/ffprobe.exe"),
                    ),
                ),
            ),
        )
        tools_dir = tmp_path / "tools"
        events: list[InstallProgress] = []

        first = install_tool(spec, tools_dir=tools_dir, on_progress=events.append)

        assert first.status == "installed"
        assert (tools_dir / "bin" / "ffmpeg.exe").read_bytes() == b"fake-ffmpeg-binary"
        assert (tools_dir / "bin" / "ffprobe.exe").read_bytes() == b"fake-ffprobe-binary"
        assert not (tools_dir / "bin" / "README.txt").exists()
        assert manifest_path(tools_dir).is_file()
        stages = [e.stage for e in events]
        assert stages[0] == "checking"
        assert "downloading" in stages
        assert "verifying" in stages
        assert "extracting" in stages
        assert stages[-1] == "done"

        # Shut down server: second run must skip download completely (idempotent).
        server.shutdown()
        second = install_tool(spec, tools_dir=tools_dir)
        assert second.status == "already_installed"
        assert second.binaries == first.binaries
    finally:
        server.shutdown()
        server.server_close()


def test_install_tar_xz_and_posix_executable_bits(tmp_path: Path) -> None:
    archive_bytes = _make_tar_xz_bytes(
        {
            "ffmpeg-n9.0.2-linux64/bin/ffmpeg": b"#!/bin/sh\necho ffmpeg\n",
            "ffmpeg-n9.0.2-linux64/bin/ffprobe": b"#!/bin/sh\necho ffprobe\n",
        }
    )
    url = "https://example.invalid/ffmpeg-linux64.tar.xz"
    spec = _spec_for(
        platform=PlatformKey("linux", "x86_64"),
        artifacts=(
            ToolArtifact(
                filename="ffmpeg-linux64.tar.xz",
                url=url,
                sha256=_sha256(archive_bytes),
                archive_format="tar.xz",
                members=(
                    ArchiveMember("ffmpeg", "bin/ffmpeg"),
                    ArchiveMember("ffprobe", "bin/ffprobe"),
                ),
            ),
        ),
    )

    transport = httpx.MockTransport(lambda req: httpx.Response(200, content=archive_bytes))
    tools_dir = tmp_path / "tools"
    with httpx.Client(transport=transport) as client:
        outcome = install_tool(spec, tools_dir=tools_dir, client=client)

    assert outcome.status == "installed"
    ffmpeg_bin = tools_dir / "bin" / "ffmpeg"
    ffprobe_bin = tools_dir / "bin" / "ffprobe"
    assert ffmpeg_bin.read_bytes() == b"#!/bin/sh\necho ffmpeg\n"
    assert ffprobe_bin.read_bytes() == b"#!/bin/sh\necho ffprobe\n"
    if sys.platform != "win32":
        assert ffmpeg_bin.stat().st_mode & 0o111
        assert ffprobe_bin.stat().st_mode & 0o111


def test_multi_artifact_install_is_atomic_when_second_artifact_fails(tmp_path: Path) -> None:
    ffmpeg_zip = _make_zip_bytes({"ffmpeg": b"mac-ffmpeg"})
    ffprobe_zip = _make_zip_bytes({"ffprobe": b"mac-ffprobe"})
    spec = _spec_for(
        platform=PlatformKey("macos", "arm64"),
        artifacts=(
            ToolArtifact(
                filename="ffmpeg.zip",
                url="https://example.invalid/ffmpeg.zip",
                sha256=_sha256(ffmpeg_zip),
                archive_format="zip",
                members=(ArchiveMember("ffmpeg", "ffmpeg"),),
            ),
            ToolArtifact(
                filename="ffprobe.zip",
                url="https://example.invalid/ffprobe.zip",
                sha256=_sha256(ffprobe_zip),
                archive_format="zip",
                members=(ArchiveMember("ffprobe", "ffprobe"),),
            ),
        ),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("ffmpeg.zip"):
            return httpx.Response(200, content=ffmpeg_zip)
        return httpx.Response(503)

    tools_dir = tmp_path / "tools"
    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(ToolDownloadError, match="HTTP 503"),
    ):
        install_tool(spec, tools_dir=tools_dir, client=client)

    # Neither binary nor manifest should be committed when the second artifact failed.
    assert not (tools_dir / "bin" / "ffmpeg").exists()
    assert not (tools_dir / "bin" / "ffprobe").exists()
    assert not manifest_path(tools_dir).exists()


def test_checksum_mismatch_discards_download_and_fails(tmp_path: Path) -> None:
    archive_bytes = _make_zip_bytes({"TwitchDownloaderCLI.exe": b"tampered"})
    spec = _spec_for(
        tool="twitch_downloader",
        platform=PlatformKey("windows", "x86_64"),
        artifacts=(
            ToolArtifact(
                filename="td.zip",
                url="https://example.invalid/td.zip",
                sha256="0" * 64,
                archive_format="zip",
                members=(ArchiveMember("TwitchDownloaderCLI.exe", "TwitchDownloaderCLI.exe"),),
            ),
        ),
    )

    tools_dir = tmp_path / "tools"
    with (
        httpx.Client(
            transport=httpx.MockTransport(lambda _req: httpx.Response(200, content=archive_bytes))
        ) as client,
        pytest.raises(ChecksumMismatchError, match="SHA-256 mismatch"),
    ):
        install_tool(spec, tools_dir=tools_dir, client=client)

    assert not (tools_dir / "bin" / "TwitchDownloaderCLI.exe").exists()
    assert not (tools_dir / ".tmp").exists()


@pytest.mark.parametrize(
    ("archive_format", "payload", "expected_error"),
    [
        ("zip", b"not a zip", "could not open zip"),
        (
            "zip",
            _make_zip_bytes({"../escape/ffmpeg.exe": b"evil"}),
            "path traversal",
        ),
        (
            "zip",
            _make_zip_bytes({"other.txt": b"hi"}, symlink_name="bin/ffmpeg.exe"),
            "missing expected binary",
        ),
        (
            "tar.xz",
            _make_tar_xz_bytes({"../escape/ffmpeg": b"evil"}),
            "path traversal",
        ),
        (
            "tar.xz",
            _make_tar_xz_bytes({"other.txt": b"hi"}, symlink_name="bin/ffmpeg"),
            "missing expected binary",
        ),
    ],
)
def test_unsafe_or_corrupt_archives_are_rejected(
    tmp_path: Path,
    archive_format: ArchiveFormat,
    payload: bytes,
    expected_error: str,
) -> None:
    is_win = archive_format == "zip"
    member = (
        ArchiveMember("ffmpeg.exe", "bin/ffmpeg.exe")
        if is_win
        else ArchiveMember("ffmpeg", "bin/ffmpeg")
    )
    spec = _spec_for(
        platform=PlatformKey("windows" if is_win else "linux", "x86_64"),
        artifacts=(
            ToolArtifact(
                filename="bad.archive",
                url="https://example.invalid/bad.archive",
                sha256=_sha256(payload),
                archive_format=archive_format,
                members=(member,),
            ),
        ),
    )

    with (
        httpx.Client(
            transport=httpx.MockTransport(lambda _req: httpx.Response(200, content=payload))
        ) as client,
        pytest.raises(ToolArchiveError, match=expected_error),
    ):
        install_tool(spec, tools_dir=tmp_path / "tools", client=client)


def test_corrupted_installed_binary_or_force_triggers_reinstall(tmp_path: Path) -> None:
    archive_bytes = _make_zip_bytes({"TwitchDownloaderCLI.exe": b"good-binary"})
    spec = _spec_for(
        tool="twitch_downloader",
        platform=PlatformKey("windows", "x86_64"),
        artifacts=(
            ToolArtifact(
                filename="td.zip",
                url="https://example.invalid/td.zip",
                sha256=_sha256(archive_bytes),
                archive_format="zip",
                members=(ArchiveMember("TwitchDownloaderCLI.exe", "TwitchDownloaderCLI.exe"),),
            ),
        ),
    )
    calls = 0

    def handler(_req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, content=archive_bytes)

    tools_dir = tmp_path / "tools"
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert install_tool(spec, tools_dir=tools_dir, client=client).status == "installed"
        assert calls == 1

        # Clean re-run skips.
        assert install_tool(spec, tools_dir=tools_dir, client=client).status == "already_installed"
        assert calls == 1

        # Corrupted binary on disk is detected and reinstalled.
        (tools_dir / "bin" / "TwitchDownloaderCLI.exe").write_bytes(b"corrupted")
        assert install_tool(spec, tools_dir=tools_dir, client=client).status == "installed"
        assert calls == 2
        assert (tools_dir / "bin" / "TwitchDownloaderCLI.exe").read_bytes() == b"good-binary"

        # --force always reinstalls.
        assert (
            install_tool(spec, tools_dir=tools_dir, force=True, client=client).status == "installed"
        )
        assert calls == 3


def test_offline_archive_dir_installs_without_network(tmp_path: Path) -> None:
    archive_dir = tmp_path / "offline"
    archive_dir.mkdir()
    archive_bytes = _make_zip_bytes({"TwitchDownloaderCLI.exe": b"offline-td"})
    (archive_dir / "td.zip").write_bytes(archive_bytes)

    spec = _spec_for(
        tool="twitch_downloader",
        platform=PlatformKey("windows", "x86_64"),
        artifacts=(
            ToolArtifact(
                filename="td.zip",
                url="https://example.invalid/td.zip",
                sha256=_sha256(archive_bytes),
                archive_format="zip",
                members=(ArchiveMember("TwitchDownloaderCLI.exe", "TwitchDownloaderCLI.exe"),),
            ),
        ),
    )

    tools_dir = tmp_path / "tools"
    outcome = install_tool(spec, tools_dir=tools_dir, archive_dir=archive_dir)
    assert outcome.status == "installed"
    assert (tools_dir / "bin" / "TwitchDownloaderCLI.exe").read_bytes() == b"offline-td"
    # Offline archive is preserved untouched.
    assert (archive_dir / "td.zip").is_file()

    # Discovery now finds the installed managed binary.
    resolved = resolve_tool("twitch_downloader", tools_dir=tools_dir, platform="win32")
    assert resolved is not None
    assert resolved.source == "managed"
    assert resolved.path == tools_dir / "bin" / "TwitchDownloaderCLI.exe"


def test_offline_archive_missing_or_bad_checksum_raises(tmp_path: Path) -> None:
    archive_dir = tmp_path / "offline"
    archive_dir.mkdir()
    spec = _spec_for(
        tool="twitch_downloader",
        platform=PlatformKey("windows", "x86_64"),
        artifacts=(
            ToolArtifact(
                filename="missing.zip",
                url="https://example.invalid/missing.zip",
                sha256="a" * 64,
                archive_format="zip",
                members=(ArchiveMember("TwitchDownloaderCLI.exe", "TwitchDownloaderCLI.exe"),),
            ),
        ),
    )
    with pytest.raises(ToolDownloadError, match="Offline archive not found"):
        install_tool(spec, tools_dir=tmp_path / "tools", archive_dir=archive_dir)

    (archive_dir / "missing.zip").write_bytes(b"wrong-bytes")
    with pytest.raises(ChecksumMismatchError, match="SHA-256 mismatch"):
        install_tool(spec, tools_dir=tmp_path / "tools", archive_dir=archive_dir)


def test_install_tools_validates_unsupported_platform_up_front(tmp_path: Path) -> None:
    with pytest.raises(UnsupportedPlatformError, match="windows-arm64"):
        install_tools(
            ("ffmpeg", "twitch_downloader"),
            tools_dir=tmp_path / "tools",
            platform=PlatformKey("windows", "arm64"),
        )
    assert not (tmp_path / "tools").exists()


# --- CLI: autoclipper setup-tools -------------------------------------------------------------


def test_cli_setup_tools_show_urls_prints_pinned_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "autoclipper.tools.manifest.detect_platform",
        lambda: PlatformKey("linux", "x86_64"),
    )

    result = cli_runner.invoke(app, ["setup-tools", "--show-urls"])

    assert result.exit_code == ExitCode.SUCCESS, result.output
    assert "linux-x86_64" in result.output
    assert "FFmpeg & FFprobe" in result.output
    assert "TwitchDownloaderCLI" in result.output
    assert "sha256:" in result.output
    assert "GPL-3.0" in result.output
    assert "MIT" in result.output


def test_cli_setup_tools_offline_install_and_re_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    offline_dir = tmp_path / "pkg"
    offline_dir.mkdir()
    td_bytes = _make_zip_bytes({"TwitchDownloaderCLI.exe": b"cli-td"})
    (offline_dir / "td.zip").write_bytes(td_bytes)
    fake_spec = _spec_for(
        tool="twitch_downloader",
        platform=PlatformKey("windows", "x86_64"),
        artifacts=(
            ToolArtifact(
                filename="td.zip",
                url="https://example.invalid/td.zip",
                sha256=_sha256(td_bytes),
                archive_format="zip",
                members=(ArchiveMember("TwitchDownloaderCLI.exe", "TwitchDownloaderCLI.exe"),),
            ),
        ),
    )
    monkeypatch.setattr(
        "autoclipper.tools.installer.detect_platform",
        lambda: PlatformKey("windows", "x86_64"),
    )
    monkeypatch.setattr(
        "autoclipper.tools.installer.get_tool_spec",
        lambda tool, plat=None: fake_spec,
    )

    tools_dir = tmp_path / "managed"
    first = cli_runner.invoke(
        app,
        [
            "setup-tools",
            "--tool",
            "twitch-downloader",
            "--from-dir",
            str(offline_dir),
            "--tools-dir",
            str(tools_dir),
        ],
    )
    assert first.exit_code == ExitCode.SUCCESS, first.output
    assert "installed" in first.output
    manifest_data = json.loads(manifest_path(tools_dir).read_text(encoding="utf-8"))
    assert manifest_data["tools"]["twitch_downloader"]["version"] == "1.56.5"

    second = cli_runner.invoke(
        app,
        [
            "setup-tools",
            "--tool",
            "twitch-downloader",
            "--from-dir",
            str(offline_dir),
            "--tools-dir",
            str(tools_dir),
        ],
    )
    assert second.exit_code == ExitCode.SUCCESS, second.output
    assert "already installed" in second.output


def test_cli_setup_tools_error_exit_codes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bad_dir = cli_runner.invoke(app, ["setup-tools", "--from-dir", str(tmp_path / "nope")])
    assert bad_dir.exit_code == ExitCode.USER_ERROR

    monkeypatch.setattr(
        "autoclipper.tools.installer.detect_platform",
        lambda: PlatformKey("windows", "arm64"),
    )
    unsupported = cli_runner.invoke(app, ["setup-tools", "--tool", "twitch-downloader"])
    assert unsupported.exit_code == ExitCode.USER_ERROR
    assert "windows-arm64" in unsupported.output

    existing_file = tmp_path / "not-a-dir"
    existing_file.write_text("file", encoding="utf-8")
    bad_tools_dir = cli_runner.invoke(
        app, ["setup-tools", "--tool", "ffmpeg", "--tools-dir", str(existing_file)]
    )
    assert bad_tools_dir.exit_code == ExitCode.USER_ERROR
    assert "must be a directory" in bad_tools_dir.output


def test_partial_replace_failure_rolls_back_previous_binaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tools_dir = tmp_path / "tools"
    bin_dir = tools_dir / "bin"
    bin_dir.mkdir(parents=True)
    old_ffmpeg = bin_dir / "ffmpeg.exe"
    old_ffprobe = bin_dir / "ffprobe.exe"
    old_ffmpeg.write_bytes(b"old-ffmpeg")
    old_ffprobe.write_bytes(b"old-ffprobe")

    archive_bytes = _make_zip_bytes(
        {
            "pkg/bin/ffmpeg.exe": b"new-ffmpeg",
            "pkg/bin/ffprobe.exe": b"new-ffprobe",
        }
    )
    spec = _spec_for(
        platform=PlatformKey("windows", "x86_64"),
        artifacts=(
            ToolArtifact(
                filename="ffmpeg.zip",
                url="https://example.invalid/ffmpeg.zip",
                sha256=_sha256(archive_bytes),
                archive_format="zip",
                members=(
                    ArchiveMember("ffmpeg.exe", "bin/ffmpeg.exe"),
                    ArchiveMember("ffprobe.exe", "bin/ffprobe.exe"),
                ),
            ),
        ),
    )

    real_replace = Path.replace

    def fail_on_ffprobe(self: Path, target: Path) -> Path:
        if target == old_ffprobe:
            raise PermissionError("locked by running process")
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", fail_on_ffprobe)
    with (
        httpx.Client(
            transport=httpx.MockTransport(lambda _req: httpx.Response(200, content=archive_bytes))
        ) as client,
        pytest.raises(ToolInstallError, match="locked by running process"),
    ):
        install_tool(spec, tools_dir=tools_dir, client=client)

    assert old_ffmpeg.read_bytes() == b"old-ffmpeg"
    assert old_ffprobe.read_bytes() == b"old-ffprobe"


def test_overlapping_installs_preserve_both_manifest_entries(tmp_path: Path) -> None:
    tools_dir = tmp_path / "tools"
    ffmpeg_zip = _make_zip_bytes(
        {"pkg/bin/ffmpeg.exe": b"ff-bin", "pkg/bin/ffprobe.exe": b"fp-bin"}
    )
    td_zip = _make_zip_bytes({"TwitchDownloaderCLI.exe": b"td-bin"})
    ffmpeg_spec = _spec_for(
        tool="ffmpeg",
        platform=PlatformKey("windows", "x86_64"),
        artifacts=(
            ToolArtifact(
                filename="ffmpeg.zip",
                url="https://example.invalid/ffmpeg.zip",
                sha256=_sha256(ffmpeg_zip),
                archive_format="zip",
                members=(
                    ArchiveMember("ffmpeg.exe", "bin/ffmpeg.exe"),
                    ArchiveMember("ffprobe.exe", "bin/ffprobe.exe"),
                ),
            ),
        ),
    )
    td_spec = _spec_for(
        tool="twitch_downloader",
        platform=PlatformKey("windows", "x86_64"),
        artifacts=(
            ToolArtifact(
                filename="td.zip",
                url="https://example.invalid/td.zip",
                sha256=_sha256(td_zip),
                archive_format="zip",
                members=(ArchiveMember("TwitchDownloaderCLI.exe", "TwitchDownloaderCLI.exe"),),
            ),
        ),
    )

    # Simulate td installing after ffmpeg already loaded an empty manifest snapshot.
    td_installed = False

    def ffmpeg_handler(_req: httpx.Request) -> httpx.Response:
        nonlocal td_installed
        if not td_installed:
            td_installed = True
            with httpx.Client(
                transport=httpx.MockTransport(lambda _r: httpx.Response(200, content=td_zip))
            ) as td_client:
                install_tool(td_spec, tools_dir=tools_dir, client=td_client)
        return httpx.Response(200, content=ffmpeg_zip)

    with httpx.Client(transport=httpx.MockTransport(ffmpeg_handler)) as ff_client:
        install_tool(ffmpeg_spec, tools_dir=tools_dir, client=ff_client)

    manifest_data = json.loads(manifest_path(tools_dir).read_text(encoding="utf-8"))
    assert set(manifest_data["tools"]) == {"ffmpeg", "twitch_downloader"}


def test_offline_archive_extracts_verified_staged_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive_dir = tmp_path / "shared"
    archive_dir.mkdir()
    good_zip = _make_zip_bytes({"TwitchDownloaderCLI.exe": b"verified-bytes"})
    source_archive = archive_dir / "td.zip"
    source_archive.write_bytes(good_zip)
    spec = _spec_for(
        tool="twitch_downloader",
        platform=PlatformKey("windows", "x86_64"),
        artifacts=(
            ToolArtifact(
                filename="td.zip",
                url="https://example.invalid/td.zip",
                sha256=_sha256(good_zip),
                archive_format="zip",
                members=(ArchiveMember("TwitchDownloaderCLI.exe", "TwitchDownloaderCLI.exe"),),
            ),
        ),
    )

    # Mutate the source file right before extraction starts; staging must isolate the bytes.
    def tamper_on_extract(event: InstallProgress) -> None:
        if event.stage == "extracting":
            source_archive.write_bytes(_make_zip_bytes({"TwitchDownloaderCLI.exe": b"tampered"}))

    tools_dir = tmp_path / "tools"
    outcome = install_tool(
        spec, tools_dir=tools_dir, archive_dir=archive_dir, on_progress=tamper_on_extract
    )
    assert outcome.status == "installed"
    assert (tools_dir / "bin" / "TwitchDownloaderCLI.exe").read_bytes() == b"verified-bytes"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX execute bit check")
def test_non_executable_managed_binary_is_repaired_on_rerun(tmp_path: Path) -> None:
    archive_bytes = _make_tar_xz_bytes({"pkg/bin/ffmpeg": b"#!/bin/sh\necho ok\n"})
    spec = _spec_for(
        platform=PlatformKey("linux", "x86_64"),
        artifacts=(
            ToolArtifact(
                filename="ffmpeg.tar.xz",
                url="https://example.invalid/ffmpeg.tar.xz",
                sha256=_sha256(archive_bytes),
                archive_format="tar.xz",
                members=(ArchiveMember("ffmpeg", "bin/ffmpeg"),),
            ),
        ),
    )
    tools_dir = tmp_path / "tools"
    with httpx.Client(
        transport=httpx.MockTransport(lambda _req: httpx.Response(200, content=archive_bytes))
    ) as client:
        install_tool(spec, tools_dir=tools_dir, client=client)
        binary = tools_dir / "bin" / "ffmpeg"
        binary.chmod(0o644)
        repaired = install_tool(spec, tools_dir=tools_dir, client=client)
        assert repaired.status == "installed"
        assert binary.stat().st_mode & 0o111


@pytest.mark.live
def test_live_pinned_twitch_downloader_release_is_reachable() -> None:
    from autoclipper.tools.manifest import get_tool_spec

    spec = get_tool_spec("twitch_downloader")
    with httpx.Client(follow_redirects=True, timeout=30.0) as client:
        response = client.head(spec.artifacts[0].url)
    assert response.status_code == 200
