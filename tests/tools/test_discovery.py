"""Tests for `autoclipper.tools.discovery` and its integration with ffmpeg and chat."""

import shutil
import sys
from pathlib import Path

import pytest

from autoclipper.config import get_settings
from autoclipper.ffmpeg import FFMPEG, FFPROBE, FFmpegNotFoundError, find_program
from autoclipper.ingest.chat import TOOL_NAME, ChatDownloadError, find_twitchdownloader
from autoclipper.tools.discovery import (
    ConfiguredToolError,
    ToolName,
    executable_name,
    managed_binary_path,
    resolve_all_tools,
    resolve_tool,
)


def _make_fake_binary(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"#!/bin/sh\nexit 0\n")
    path.chmod(0o755)
    return path


def test_executable_name_matches_platform() -> None:
    assert executable_name("ffmpeg", platform="win32") == "ffmpeg.exe"
    assert executable_name("ffprobe", platform="win32") == "ffprobe.exe"
    assert executable_name("twitch_downloader", platform="win32") == "TwitchDownloaderCLI.exe"
    assert executable_name("ffmpeg", platform="darwin") == "ffmpeg"
    assert executable_name("twitch_downloader", platform="linux") == "TwitchDownloaderCLI"


@pytest.mark.parametrize("tool", ["ffmpeg", "ffprobe", "twitch_downloader"])
def test_discovery_precedence_config_beats_managed_beats_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tool: ToolName
) -> None:
    tools_dir = tmp_path / "tools"
    path_dir = tmp_path / "path_bin"
    config_bin = _make_fake_binary(tmp_path / "custom" / executable_name(tool))
    managed_bin = _make_fake_binary(managed_binary_path(tool, tools_dir))
    path_bin = _make_fake_binary(path_dir / executable_name(tool))

    monkeypatch.setattr(
        shutil,
        "which",
        lambda name: str(path_bin) if name in {FFMPEG, FFPROBE, TOOL_NAME} else None,
    )

    from_config = resolve_tool(tool, tools_dir=tools_dir, explicit_path=config_bin)
    assert from_config is not None
    assert from_config.source == "config"
    assert from_config.path == config_bin

    from_managed = resolve_tool(tool, tools_dir=tools_dir)
    assert from_managed is not None
    assert from_managed.source == "managed"
    assert from_managed.path == managed_bin

    managed_bin.unlink()
    from_path = resolve_tool(tool, tools_dir=tools_dir)
    assert from_path is not None
    assert from_path.source == "path"
    assert from_path.path == path_bin

    monkeypatch.setattr(shutil, "which", lambda _name: None)
    assert resolve_tool(tool, tools_dir=tools_dir) is None


def test_explicit_config_missing_raises_instead_of_falling_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tools_dir = tmp_path / "tools"
    _make_fake_binary(managed_binary_path("ffmpeg", tools_dir))
    monkeypatch.setattr(shutil, "which", lambda _name: "/fake/bin/ffmpeg")

    missing = tmp_path / "does-not-exist"
    with pytest.raises(ConfiguredToolError) as excinfo:
        resolve_tool("ffmpeg", tools_dir=tools_dir, explicit_path=missing)

    err = excinfo.value
    assert err.tool == "ffmpeg"
    assert err.config_var == "AUTOCLIPPER_FFMPEG_PATH"
    assert err.configured_path == missing
    assert "does not exist" in err.safe_message
    assert str(missing) not in err.safe_message


def test_explicit_config_directory_is_rejected(tmp_path: Path) -> None:
    folder = tmp_path / "not-a-file"
    folder.mkdir()

    with pytest.raises(ConfiguredToolError, match="is a directory"):
        resolve_tool("ffprobe", explicit_path=folder)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX execute bit check")
def test_non_executable_file_rejected_on_posix(tmp_path: Path) -> None:
    non_exec = tmp_path / "ffmpeg"
    non_exec.write_bytes(b"data")
    non_exec.chmod(0o644)

    with pytest.raises(ConfiguredToolError, match="not executable"):
        resolve_tool("ffmpeg", explicit_path=non_exec, platform="linux")


def test_windows_explicit_path_resolves_exe_suffix(tmp_path: Path) -> None:
    exe = _make_fake_binary(tmp_path / "ffmpeg.exe")
    bare = tmp_path / "ffmpeg"

    resolved = resolve_tool("ffmpeg", explicit_path=bare, platform="win32")

    assert resolved is not None
    assert resolved.source == "config"
    assert resolved.path == exe


def test_find_program_uses_managed_and_config_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tools_dir = tmp_path / "tools"
    managed_ffmpeg = _make_fake_binary(managed_binary_path("ffmpeg", tools_dir))
    monkeypatch.setenv("AUTOCLIPPER_TOOLS_DIR", str(tools_dir))
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    get_settings.cache_clear()

    assert find_program(FFMPEG) == managed_ffmpeg

    custom_ffprobe = _make_fake_binary(tmp_path / "custom-ffprobe.exe")
    monkeypatch.setenv("AUTOCLIPPER_FFPROBE_PATH", str(custom_ffprobe))
    get_settings.cache_clear()

    assert find_program(FFPROBE) == custom_ffprobe

    monkeypatch.setenv("AUTOCLIPPER_FFMPEG_PATH", str(tmp_path / "missing-ffmpeg"))
    get_settings.cache_clear()
    with pytest.raises(FFmpegNotFoundError, match="AUTOCLIPPER_FFMPEG_PATH"):
        find_program(FFMPEG)


def test_find_twitchdownloader_uses_managed_and_config_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tools_dir = tmp_path / "tools"
    managed_td = _make_fake_binary(managed_binary_path("twitch_downloader", tools_dir))
    monkeypatch.setenv("AUTOCLIPPER_TOOLS_DIR", str(tools_dir))
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    get_settings.cache_clear()

    assert find_twitchdownloader() == managed_td

    monkeypatch.setenv("AUTOCLIPPER_TWITCH_DOWNLOADER_PATH", str(tmp_path / "missing-td"))
    get_settings.cache_clear()
    with pytest.raises(ChatDownloadError, match="AUTOCLIPPER_TWITCH_DOWNLOADER_PATH"):
        find_twitchdownloader()


def test_resolve_all_tools_captures_mixed_outcomes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tools_dir = tmp_path / "tools"
    managed_ffmpeg = _make_fake_binary(managed_binary_path("ffmpeg", tools_dir))
    monkeypatch.setenv("AUTOCLIPPER_TOOLS_DIR", str(tools_dir))
    monkeypatch.setenv("AUTOCLIPPER_FFPROBE_PATH", str(tmp_path / "bad-ffprobe"))
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    get_settings.cache_clear()

    resolved = resolve_all_tools(get_settings())

    assert resolved["ffmpeg"] is not None
    assert not isinstance(resolved["ffmpeg"], ConfiguredToolError)
    assert resolved["ffmpeg"].path == managed_ffmpeg
    assert isinstance(resolved["ffprobe"], ConfiguredToolError)
    assert resolved["twitch_downloader"] is None
