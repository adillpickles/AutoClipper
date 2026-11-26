"""Tests for `autoclipper.tools.manifest`: platform detection and pinned release metadata."""

import re

import pytest

from autoclipper.tools.manifest import (
    ALL_TOOL_GROUPS,
    PINNED_TOOLS,
    PlatformKey,
    UnsupportedPlatformError,
    detect_platform,
    get_tool_spec,
    supported_platforms,
)

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


@pytest.mark.parametrize(
    ("sys_platform", "machine", "expected"),
    [
        ("win32", "AMD64", PlatformKey("windows", "x86_64")),
        ("win32", "x86_64", PlatformKey("windows", "x86_64")),
        ("win32", "ARM64", PlatformKey("windows", "arm64")),
        ("darwin", "arm64", PlatformKey("macos", "arm64")),
        ("darwin", "x86_64", PlatformKey("macos", "x86_64")),
        ("linux", "x86_64", PlatformKey("linux", "x86_64")),
        ("linux", "aarch64", PlatformKey("linux", "arm64")),
        ("linux2", "amd64", PlatformKey("linux", "x86_64")),
    ],
)
def test_detect_platform_normalizes_os_and_cpu(
    sys_platform: str, machine: str, expected: PlatformKey
) -> None:
    assert detect_platform(sys_platform=sys_platform, machine=machine) == expected


@pytest.mark.parametrize(
    ("sys_platform", "machine"),
    [
        ("freebsd14", "amd64"),
        ("linux", "riscv64"),
        ("win32", "x86"),
        ("sunos5", "sparc"),
    ],
)
def test_detect_platform_rejects_unsupported_targets(sys_platform: str, machine: str) -> None:
    with pytest.raises(UnsupportedPlatformError, match="Unsupported platform"):
        detect_platform(sys_platform=sys_platform, machine=machine)


def test_every_pinned_entry_has_valid_https_url_and_sha256() -> None:
    assert PINNED_TOOLS
    for (tool, plat), spec in PINNED_TOOLS.items():
        assert spec.tool == tool
        assert spec.platform == plat
        assert spec.license in {"GPL-3.0", "MIT"}
        assert spec.artifacts
        for artifact in spec.artifacts:
            assert artifact.url.startswith("https://")
            assert SHA256_RE.fullmatch(artifact.sha256), artifact.sha256
            assert artifact.members


def test_windows_arm64_has_ffmpeg_but_clear_error_for_twitch_downloader() -> None:
    plat = PlatformKey("windows", "arm64")
    ffmpeg_spec = get_tool_spec("ffmpeg", plat)
    assert ffmpeg_spec.binary_names == ("ffmpeg.exe", "ffprobe.exe")

    with pytest.raises(UnsupportedPlatformError) as excinfo:
        get_tool_spec("twitch_downloader", plat)
    message = str(excinfo.value)
    assert "windows-arm64" in message
    assert "--tool ffmpeg" in message
    assert "windows-x86_64" in message


@pytest.mark.parametrize("tool", ALL_TOOL_GROUPS)
def test_supported_platforms_lists_pinned_targets(tool: str) -> None:
    platforms = supported_platforms(tool)  # type: ignore[arg-type]
    assert "windows-x86_64" in platforms
    assert "macos-arm64" in platforms
    assert "linux-x86_64" in platforms
