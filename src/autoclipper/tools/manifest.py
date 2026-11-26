"""Pinned external tool release catalog and platform detection.

AutoClipper does not bundle third-party binaries in git or its Python package. Instead,
``autoclipper setup-tools`` (and the API tool setup endpoint) downloads pinned, SHA-256-verified
archives directly from each tool's upstream release host into the user's managed tools folder:

- **FFmpeg & FFprobe** (GPL-3.0 static builds with ``libx264`` and ``libass``, no nonfree codecs):
  - Windows x86_64: Gyan.dev ``codexffmpeg`` release ``9.0.2`` (listed on ffmpeg.org)
  - Windows ARM64, Linux x86_64, Linux ARM64: BtbN ``FFmpeg-Builds`` end-of-month build
    ``autobuild-2026-10-31-12-59`` (FFmpeg ``9.0.2`` GPL static, listed on ffmpeg.org)
  - macOS ARM64 & x86_64: Martin Riedl's signed static release ``9.1`` builds
- **TwitchDownloaderCLI** (MIT license):
  - ``lay295/TwitchDownloader`` GitHub release ``1.56.5`` (the pinned chat/video release)
"""

import platform as stdlib_platform
import sys
from dataclasses import dataclass
from typing import Final, Literal

type ToolGroup = Literal["ffmpeg", "twitch_downloader"]
type PlatformOS = Literal["windows", "macos", "linux"]
type PlatformArch = Literal["x86_64", "arm64"]
type ArchiveFormat = Literal["zip", "tar.xz"]

ALL_TOOL_GROUPS: Final[tuple[ToolGroup, ...]] = ("ffmpeg", "twitch_downloader")

_OS_ALIASES: Final[dict[str, PlatformOS]] = {
    "win32": "windows",
    "windows": "windows",
    "darwin": "macos",
    "macos": "macos",
    "linux": "linux",
}

_ARCH_ALIASES: Final[dict[str, PlatformArch]] = {
    "x86_64": "x86_64",
    "amd64": "x86_64",
    "x64": "x86_64",
    "arm64": "arm64",
    "aarch64": "arm64",
}


class UnsupportedPlatformError(ValueError):
    """The current OS/CPU combination has no pinned download build for the requested tool."""

    def __init__(
        self,
        *,
        os_name: str,
        arch_name: str,
        tool: ToolGroup | None = None,
        supported: tuple[str, ...] = (),
        detail: str | None = None,
    ) -> None:
        self.os_name = os_name
        self.arch_name = arch_name
        self.tool = tool
        self.supported = supported
        target = f"{os_name}-{arch_name}"
        if detail is not None:
            message = detail
        elif tool is None:
            message = (
                f"Unsupported platform {target!r} for automated tool setup. "
                "Install FFmpeg and TwitchDownloaderCLI manually and add them to PATH or set "
                "AUTOCLIPPER_FFMPEG_PATH, AUTOCLIPPER_FFPROBE_PATH and "
                "AUTOCLIPPER_TWITCH_DOWNLOADER_PATH."
            )
        else:
            supported_text = ", ".join(supported) if supported else "none"
            other_hint = (
                " You can still run `autoclipper setup-tools --tool ffmpeg` for FFmpeg."
                if tool == "twitch_downloader" and os_name == "windows" and arch_name == "arm64"
                else ""
            )
            message = (
                f"No pinned {tool} build is available for {target}. "
                f"Supported platforms: {supported_text}. Install it manually on PATH or set "
                f"its AUTOCLIPPER_*_PATH variable.{other_hint}"
            )
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class PlatformKey:
    """Normalized ``(os, arch)`` pair used to select pinned tool builds."""

    os: PlatformOS
    arch: PlatformArch

    @property
    def key(self) -> str:
        """Stable hyphenated identifier, e.g. ``'windows-x86_64'``."""
        return f"{self.os}-{self.arch}"

    @property
    def sys_platform(self) -> str:
        """Corresponding ``sys.platform`` string for executable naming."""
        if self.os == "windows":
            return "win32"
        if self.os == "macos":
            return "darwin"
        return "linux"


@dataclass(frozen=True, slots=True)
class ArchiveMember:
    """One executable extracted from a downloaded archive into ``<tools_dir>/bin/``.

    Attributes:
        binary_name: Target filename inside ``<tools_dir>/bin/`` (e.g. ``"ffmpeg.exe"``).
        member_suffix: Expected POSIX-style relative path suffix inside the archive (e.g.
            ``"bin/ffmpeg.exe"`` or ``"ffmpeg"``).
    """

    binary_name: str
    member_suffix: str


@dataclass(frozen=True, slots=True)
class ToolArtifact:
    """One pinned downloadable archive with its expected SHA-256 digest and extracted members."""

    filename: str
    url: str
    sha256: str
    archive_format: ArchiveFormat
    members: tuple[ArchiveMember, ...]
    size_bytes: int | None = None


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """Pinned upstream release specification for one tool group on one platform."""

    tool: ToolGroup
    label: str
    version: str
    platform: PlatformKey
    source_name: str
    source_url: str
    license: str
    artifacts: tuple[ToolArtifact, ...]

    @property
    def binary_names(self) -> tuple[str, ...]:
        """All executable filenames installed into ``<tools_dir>/bin/`` by this spec."""
        return tuple(member.binary_name for art in self.artifacts for member in art.members)


_MIN_LINUX_GLIBC: Final = (2, 28)


def _parse_glibc_version(version_text: str) -> tuple[int, int] | None:
    parts = version_text.strip().split(".")
    if len(parts) < 2 or not (parts[0].isdigit() and parts[1].isdigit()):
        return None
    return (int(parts[0]), int(parts[1]))


def detect_platform(
    *,
    sys_platform: str | None = None,
    machine: str | None = None,
    libc_ver: tuple[str, str] | None = None,
) -> PlatformKey:
    """Detect and normalize the host OS and CPU architecture.

    On Linux hosts, also verifies that glibc >= 2.28 is present (required by the pinned
    BtbN FFmpeg and glibc TwitchDownloaderCLI builds; musl/Alpine hosts are rejected with an
    actionable error).

    Raises:
        UnsupportedPlatformError: The OS, CPU architecture, or Linux C library is unsupported.
    """
    raw_os = (sys.platform if sys_platform is None else sys_platform).strip().lower()
    raw_arch = (stdlib_platform.machine() if machine is None else machine).strip().lower()

    os_key = _OS_ALIASES.get(raw_os)
    if os_key is None and raw_os.startswith("linux"):
        os_key = "linux"
    arch_key = _ARCH_ALIASES.get(raw_arch)

    if os_key is None or arch_key is None:
        raise UnsupportedPlatformError(
            os_name=os_key or raw_os or "unknown",
            arch_name=arch_key or raw_arch or "unknown",
        )

    if os_key == "linux" and (sys_platform is None or libc_ver is not None):
        libc_name, libc_version = stdlib_platform.libc_ver() if libc_ver is None else libc_ver
        parsed = _parse_glibc_version(libc_version) if libc_name == "glibc" else None
        if parsed is None or parsed < _MIN_LINUX_GLIBC:
            found = f"{libc_name} {libc_version}".strip() or "non-glibc (e.g. musl/Alpine)"
            raise UnsupportedPlatformError(
                os_name=os_key,
                arch_name=arch_key,
                detail=(
                    f"Unsupported Linux C library ({found}) for pinned downloads; "
                    f"pinned Linux builds require glibc >= {_MIN_LINUX_GLIBC[0]}."
                    f"{_MIN_LINUX_GLIBC[1]}. Install FFmpeg and TwitchDownloaderCLI via your "
                    "system package manager or set AUTOCLIPPER_FFMPEG_PATH, "
                    "AUTOCLIPPER_FFPROBE_PATH and AUTOCLIPPER_TWITCH_DOWNLOADER_PATH."
                ),
            )

    return PlatformKey(os=os_key, arch=arch_key)


_BTBN_TAG: Final = "autobuild-2026-10-31-12-59"
_BTBN_BASE: Final = f"https://github.com/FFmpeg-Builds/FFmpeg-Builds/releases/download/{_BTBN_TAG}"
_BTBN_PAGE: Final = f"https://github.com/FFmpeg-Builds/FFmpeg-Builds/releases/tag/{_BTBN_TAG}"

_GYAN_VERSION: Final = "9.0.2"
_GYAN_PAGE: Final = f"https://github.com/GyanD/codexffmpeg/releases/tag/{_GYAN_VERSION}"
_GYAN_URL: Final = (
    f"https://github.com/GyanD/codexffmpeg/releases/download/{_GYAN_VERSION}/"
    f"ffmpeg-{_GYAN_VERSION}-essentials_build.zip"
)

_MR_VERSION: Final = "9.1"
_MR_PAGE: Final = "https://ffmpeg.martin-riedl.de/"

_TD_VERSION: Final = "1.56.5"
_TD_BASE: Final = f"https://github.com/lay295/TwitchDownloader/releases/download/{_TD_VERSION}"
_TD_PAGE: Final = f"https://github.com/lay295/TwitchDownloader/releases/tag/{_TD_VERSION}"


PINNED_TOOLS: Final[dict[tuple[ToolGroup, PlatformKey], ToolSpec]] = {
    # --- FFmpeg & FFprobe --------------------------------------------------------------------
    ("ffmpeg", PlatformKey("windows", "x86_64")): ToolSpec(
        tool="ffmpeg",
        label="FFmpeg & FFprobe",
        version=_GYAN_VERSION,
        platform=PlatformKey("windows", "x86_64"),
        source_name="Gyan.dev (codexffmpeg essentials)",
        source_url=_GYAN_PAGE,
        license="GPL-3.0",
        artifacts=(
            ToolArtifact(
                filename=f"ffmpeg-{_GYAN_VERSION}-essentials_build.zip",
                url=_GYAN_URL,
                sha256="60f467265b1e312373dbcd92200c2618a74850f98d3d078e94296bb3fa2047ba",
                archive_format="zip",
                size_bytes=114_768_076,
                members=(
                    ArchiveMember("ffmpeg.exe", "bin/ffmpeg.exe"),
                    ArchiveMember("ffprobe.exe", "bin/ffprobe.exe"),
                ),
            ),
        ),
    ),
    ("ffmpeg", PlatformKey("windows", "arm64")): ToolSpec(
        tool="ffmpeg",
        label="FFmpeg & FFprobe",
        version="9.0.2",
        platform=PlatformKey("windows", "arm64"),
        source_name="BtbN/FFmpeg-Builds (winarm64-gpl-9.0)",
        source_url=_BTBN_PAGE,
        license="GPL-3.0",
        artifacts=(
            ToolArtifact(
                filename="ffmpeg-n9.0.2-39-g7adfbe40dc-winarm64-gpl-9.0.zip",
                url=f"{_BTBN_BASE}/ffmpeg-n9.0.2-39-g7adfbe40dc-winarm64-gpl-9.0.zip",
                sha256="8b3c394762f26716ccef03ac1b7ac76430759080e1255aa9bc01d74db590bcb5",
                archive_format="zip",
                size_bytes=136_603_040,
                members=(
                    ArchiveMember("ffmpeg.exe", "bin/ffmpeg.exe"),
                    ArchiveMember("ffprobe.exe", "bin/ffprobe.exe"),
                ),
            ),
        ),
    ),
    ("ffmpeg", PlatformKey("linux", "x86_64")): ToolSpec(
        tool="ffmpeg",
        label="FFmpeg & FFprobe",
        version="9.0.2",
        platform=PlatformKey("linux", "x86_64"),
        source_name="BtbN/FFmpeg-Builds (linux64-gpl-9.0)",
        source_url=_BTBN_PAGE,
        license="GPL-3.0",
        artifacts=(
            ToolArtifact(
                filename="ffmpeg-n9.0.2-39-g7adfbe40dc-linux64-gpl-9.0.tar.xz",
                url=f"{_BTBN_BASE}/ffmpeg-n9.0.2-39-g7adfbe40dc-linux64-gpl-9.0.tar.xz",
                sha256="73e29a56974ae5a651133a04584c7483b21afe941e17dc91d79814a758b608d3",
                archive_format="tar.xz",
                size_bytes=153_175_100,
                members=(
                    ArchiveMember("ffmpeg", "bin/ffmpeg"),
                    ArchiveMember("ffprobe", "bin/ffprobe"),
                ),
            ),
        ),
    ),
    ("ffmpeg", PlatformKey("linux", "arm64")): ToolSpec(
        tool="ffmpeg",
        label="FFmpeg & FFprobe",
        version="9.0.2",
        platform=PlatformKey("linux", "arm64"),
        source_name="BtbN/FFmpeg-Builds (linuxarm64-gpl-9.0)",
        source_url=_BTBN_PAGE,
        license="GPL-3.0",
        artifacts=(
            ToolArtifact(
                filename="ffmpeg-n9.0.2-39-g7adfbe40dc-linuxarm64-gpl-9.0.tar.xz",
                url=f"{_BTBN_BASE}/ffmpeg-n9.0.2-39-g7adfbe40dc-linuxarm64-gpl-9.0.tar.xz",
                sha256="6cad88ae5bcec356cfc5d7979a036cc4007128201928a0cf243df3db483276cc",
                archive_format="tar.xz",
                size_bytes=128_664_064,
                members=(
                    ArchiveMember("ffmpeg", "bin/ffmpeg"),
                    ArchiveMember("ffprobe", "bin/ffprobe"),
                ),
            ),
        ),
    ),
    ("ffmpeg", PlatformKey("macos", "arm64")): ToolSpec(
        tool="ffmpeg",
        label="FFmpeg & FFprobe",
        version=_MR_VERSION,
        platform=PlatformKey("macos", "arm64"),
        source_name="Martin Riedl FFmpeg Build Server (macOS arm64)",
        source_url=_MR_PAGE,
        license="GPL-3.0",
        artifacts=(
            ToolArtifact(
                filename="ffmpeg-9.1-macos-arm64.zip",
                url="https://ffmpeg.martin-riedl.de/download/macos/arm64/1795207104_9.1/ffmpeg.zip",
                sha256="1f66e9a690a7f1c9e60b012379f8869c11818d1797f45b21dbb2f2564ba61ae1",
                archive_format="zip",
                members=(ArchiveMember("ffmpeg", "ffmpeg"),),
            ),
            ToolArtifact(
                filename="ffprobe-9.1-macos-arm64.zip",
                url="https://ffmpeg.martin-riedl.de/download/macos/arm64/1795207104_9.1/ffprobe.zip",
                sha256="6ef2df5f575042923a28537ab37adf936f28bf3e7b85de23a1447ed447ec1dbd",
                archive_format="zip",
                members=(ArchiveMember("ffprobe", "ffprobe"),),
            ),
        ),
    ),
    ("ffmpeg", PlatformKey("macos", "x86_64")): ToolSpec(
        tool="ffmpeg",
        label="FFmpeg & FFprobe",
        version=_MR_VERSION,
        platform=PlatformKey("macos", "x86_64"),
        source_name="Martin Riedl FFmpeg Build Server (macOS amd64)",
        source_url=_MR_PAGE,
        license="GPL-3.0",
        artifacts=(
            ToolArtifact(
                filename="ffmpeg-9.1-macos-x86_64.zip",
                url="https://ffmpeg.martin-riedl.de/download/macos/amd64/1795212433_9.1/ffmpeg.zip",
                sha256="35b3d82cccc2c1d644d49d15f5e57eb913a08ef68f798042c90eecec62c51f1e",
                archive_format="zip",
                members=(ArchiveMember("ffmpeg", "ffmpeg"),),
            ),
            ToolArtifact(
                filename="ffprobe-9.1-macos-x86_64.zip",
                url="https://ffmpeg.martin-riedl.de/download/macos/amd64/1795212433_9.1/ffprobe.zip",
                sha256="41ca7b7cba7992e33f1da3f1bf54db8ec103a4d387b1bfac3162062e8b1074ad",
                archive_format="zip",
                members=(ArchiveMember("ffprobe", "ffprobe"),),
            ),
        ),
    ),
    # --- TwitchDownloaderCLI -----------------------------------------------------------------
    ("twitch_downloader", PlatformKey("windows", "x86_64")): ToolSpec(
        tool="twitch_downloader",
        label="TwitchDownloaderCLI",
        version=_TD_VERSION,
        platform=PlatformKey("windows", "x86_64"),
        source_name="lay295/TwitchDownloader (GitHub release)",
        source_url=_TD_PAGE,
        license="MIT",
        artifacts=(
            ToolArtifact(
                filename=f"TwitchDownloaderCLI-{_TD_VERSION}-Windows-x64.zip",
                url=f"{_TD_BASE}/TwitchDownloaderCLI-{_TD_VERSION}-Windows-x64.zip",
                sha256="8b1b0695f2b1b6bf0d2535fab4b84032951cded8cf4078dfdf4d58e391c813a0",
                archive_format="zip",
                size_bytes=52_226_305,
                members=(ArchiveMember("TwitchDownloaderCLI.exe", "TwitchDownloaderCLI.exe"),),
            ),
        ),
    ),
    ("twitch_downloader", PlatformKey("macos", "arm64")): ToolSpec(
        tool="twitch_downloader",
        label="TwitchDownloaderCLI",
        version=_TD_VERSION,
        platform=PlatformKey("macos", "arm64"),
        source_name="lay295/TwitchDownloader (GitHub release)",
        source_url=_TD_PAGE,
        license="MIT",
        artifacts=(
            ToolArtifact(
                filename=f"TwitchDownloaderCLI-{_TD_VERSION}-MacOSArm64.zip",
                url=f"{_TD_BASE}/TwitchDownloaderCLI-{_TD_VERSION}-MacOSArm64.zip",
                sha256="17a0182f83689a0ae429f48cf03c26f1701eed7edcd787fa3d261898a6023d76",
                archive_format="zip",
                size_bytes=49_601_971,
                members=(ArchiveMember("TwitchDownloaderCLI", "TwitchDownloaderCLI"),),
            ),
        ),
    ),
    ("twitch_downloader", PlatformKey("macos", "x86_64")): ToolSpec(
        tool="twitch_downloader",
        label="TwitchDownloaderCLI",
        version=_TD_VERSION,
        platform=PlatformKey("macos", "x86_64"),
        source_name="lay295/TwitchDownloader (GitHub release)",
        source_url=_TD_PAGE,
        license="MIT",
        artifacts=(
            ToolArtifact(
                filename=f"TwitchDownloaderCLI-{_TD_VERSION}-MacOS-x64.zip",
                url=f"{_TD_BASE}/TwitchDownloaderCLI-{_TD_VERSION}-MacOS-x64.zip",
                sha256="efaa61d6109dfbce4ddedc28da55e23dc1de30caa2f6a35dc611f9b3bb48b724",
                archive_format="zip",
                size_bytes=50_052_910,
                members=(ArchiveMember("TwitchDownloaderCLI", "TwitchDownloaderCLI"),),
            ),
        ),
    ),
    ("twitch_downloader", PlatformKey("linux", "x86_64")): ToolSpec(
        tool="twitch_downloader",
        label="TwitchDownloaderCLI",
        version=_TD_VERSION,
        platform=PlatformKey("linux", "x86_64"),
        source_name="lay295/TwitchDownloader (GitHub release)",
        source_url=_TD_PAGE,
        license="MIT",
        artifacts=(
            ToolArtifact(
                filename=f"TwitchDownloaderCLI-{_TD_VERSION}-Linux-x64.zip",
                url=f"{_TD_BASE}/TwitchDownloaderCLI-{_TD_VERSION}-Linux-x64.zip",
                sha256="f2bcee00d0e6da759d97c23767345634cb9cdb10cdff96cfa96f97983d51bf55",
                archive_format="zip",
                size_bytes=46_731_535,
                members=(ArchiveMember("TwitchDownloaderCLI", "TwitchDownloaderCLI"),),
            ),
        ),
    ),
    ("twitch_downloader", PlatformKey("linux", "arm64")): ToolSpec(
        tool="twitch_downloader",
        label="TwitchDownloaderCLI",
        version=_TD_VERSION,
        platform=PlatformKey("linux", "arm64"),
        source_name="lay295/TwitchDownloader (GitHub release)",
        source_url=_TD_PAGE,
        license="MIT",
        artifacts=(
            ToolArtifact(
                filename=f"TwitchDownloaderCLI-{_TD_VERSION}-LinuxArm64.zip",
                url=f"{_TD_BASE}/TwitchDownloaderCLI-{_TD_VERSION}-LinuxArm64.zip",
                sha256="2fde836f07c3ebfcf61cace23742a2c43543fc30fda96071eb96ab8e10c24f7f",
                archive_format="zip",
                size_bytes=46_109_413,
                members=(ArchiveMember("TwitchDownloaderCLI", "TwitchDownloaderCLI"),),
            ),
        ),
    ),
}


def supported_platforms(tool: ToolGroup) -> tuple[str, ...]:
    """Return sorted ``os-arch`` platform identifiers that have a pinned build for ``tool``."""
    return tuple(sorted(plat.key for (name, plat) in PINNED_TOOLS if name == tool))


def get_tool_spec(tool: ToolGroup, platform: PlatformKey | None = None) -> ToolSpec:
    """Return the pinned `ToolSpec` for ``tool`` on ``platform`` (defaults to current host).

    Raises:
        UnsupportedPlatformError: No pinned build exists for ``(tool, platform)``.
    """
    resolved_platform = detect_platform() if platform is None else platform
    spec = PINNED_TOOLS.get((tool, resolved_platform))
    if spec is None:
        raise UnsupportedPlatformError(
            os_name=resolved_platform.os,
            arch_name=resolved_platform.arch,
            tool=tool,
            supported=supported_platforms(tool),
        )
    return spec
