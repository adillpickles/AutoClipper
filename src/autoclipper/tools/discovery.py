"""External binary discovery for FFmpeg, FFprobe, and TwitchDownloaderCLI.

Every managed tool is resolved in the same precedence order:

1. Explicit configuration path (``AUTOCLIPPER_FFMPEG_PATH``, ``AUTOCLIPPER_FFPROBE_PATH``, or
   ``AUTOCLIPPER_TWITCH_DOWNLOADER_PATH``). When set, an invalid or missing path raises
   `ConfiguredToolError` instead of silently falling back.
2. AutoClipper's per-user managed tools folder (``<tools_dir>/bin/<executable>``).
3. ``PATH`` (via `shutil.which`).
"""

import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from autoclipper.config import Settings, default_tools_dir, get_settings

type ToolName = Literal["ffmpeg", "ffprobe", "twitch_downloader"]
type ToolSource = Literal["config", "managed", "path"]

TOOL_COMMANDS: Final[dict[ToolName, str]] = {
    "ffmpeg": "ffmpeg",
    "ffprobe": "ffprobe",
    "twitch_downloader": "TwitchDownloaderCLI",
}
"""Bare executable stem for each managed tool."""

TOOL_CONFIG_VARS: Final[dict[ToolName, str]] = {
    "ffmpeg": "AUTOCLIPPER_FFMPEG_PATH",
    "ffprobe": "AUTOCLIPPER_FFPROBE_PATH",
    "twitch_downloader": "AUTOCLIPPER_TWITCH_DOWNLOADER_PATH",
}
"""Environment / ``.env`` variable that explicitly overrides each tool's path."""


class ToolDiscoveryError(Exception):
    """Base error for external tool discovery failures."""


class ConfiguredToolError(ToolDiscoveryError):
    """An explicit ``AUTOCLIPPER_*_PATH`` setting does not point to a usable executable.

    Attributes:
        tool: Logical tool identifier (``"ffmpeg"``, ``"ffprobe"``, or ``"twitch_downloader"``).
        config_var: Environment variable name, e.g. ``"AUTOCLIPPER_FFMPEG_PATH"``.
        configured_path: The path configured by the user.
        reason: Short path-free description of why the path is unusable.
    """

    tool: ToolName
    config_var: str
    configured_path: Path
    reason: str

    def __init__(
        self, tool: ToolName, config_var: str, configured_path: Path, *, reason: str
    ) -> None:
        self.tool = tool
        self.config_var = config_var
        self.configured_path = configured_path
        self.reason = reason
        super().__init__(
            f"{config_var} ({configured_path}) {reason}. Update or unset {config_var}."
        )

    @property
    def safe_message(self) -> str:
        """Path-free description safe for ``GET /api/v1/preflight`` responses."""
        return f"{self.config_var} {self.reason}."


@dataclass(frozen=True, slots=True)
class ResolvedTool:
    """A discovered external tool executable and where it came from.

    Attributes:
        name: Logical tool identifier.
        path: Resolved executable path.
        source: Where the tool was found: ``"config"``, ``"managed"``, or ``"path"``.
        config_var: The ``AUTOCLIPPER_*_PATH`` variable name when ``source == "config"``.
    """

    name: ToolName
    path: Path
    source: ToolSource
    config_var: str | None = None


def _default_tools_dir() -> Path:
    """Indirection to `default_tools_dir` so unit tests stay isolated from user profiles."""
    return default_tools_dir()


def effective_tools_dir(settings: Settings | None = None) -> Path:
    """Return the managed tools directory for ``settings``, or the OS default when unset."""
    if settings is not None and settings.tools_dir is not None:
        return settings.tools_dir
    return _default_tools_dir()


def executable_name(tool: ToolName, *, platform: str | None = None) -> str:
    """Return the platform filename for ``tool`` (with ``.exe`` on Windows)."""
    plat = sys.platform if platform is None else platform
    base = TOOL_COMMANDS[tool]
    return f"{base}.exe" if plat == "win32" else base


def managed_bin_dir(tools_dir: Path) -> Path:
    """Return the ``bin`` subdirectory inside a managed tools folder."""
    return tools_dir / "bin"


def managed_binary_path(tool: ToolName, tools_dir: Path, *, platform: str | None = None) -> Path:
    """Return the expected location of a managed executable inside ``tools_dir``."""
    return managed_bin_dir(tools_dir) / executable_name(tool, platform=platform)


def _settings_explicit_path(settings: Settings, tool: ToolName) -> Path | None:
    if tool == "ffmpeg":
        return settings.ffmpeg_path
    if tool == "ffprobe":
        return settings.ffprobe_path
    return settings.twitch_downloader_path


def _check_executable(path: Path, *, platform: str) -> tuple[Path | None, str]:
    """Check whether ``path`` is a usable executable; return ``(resolved, failure_reason)``."""
    candidate = path
    if platform == "win32" and not candidate.exists() and candidate.suffix == "":
        with_exe = candidate.with_suffix(".exe")
        if with_exe.exists():
            candidate = with_exe

    if not candidate.exists():
        return None, "does not exist"
    if candidate.is_dir():
        return None, "is a directory, not an executable file"
    if not candidate.is_file():
        return None, "is not a regular file"
    if platform != "win32" and not os.access(candidate, os.X_OK):
        return None, "is not executable"
    return candidate, ""


def resolve_tool(
    tool: ToolName,
    *,
    settings: Settings | None = None,
    tools_dir: Path | None = None,
    explicit_path: Path | None = None,
    platform: str | None = None,
) -> ResolvedTool | None:
    """Locate ``tool`` in precedence order: explicit config path -> managed folder -> ``PATH``.

    When ``settings``, ``tools_dir``, and ``explicit_path`` are all omitted, loads settings via
    `get_settings()`. Passing ``settings`` avoids reading global cached settings.

    Args:
        tool: Which managed tool to locate.
        settings: Loaded `Settings` instance, if already available.
        tools_dir: Explicit managed tools folder override (wins over ``settings.tools_dir``).
        explicit_path: Explicit executable path override (wins over ``settings.*_path``).
        platform: Optional ``sys.platform`` override for cross-platform tests.

    Returns:
        A `ResolvedTool` if found, or ``None`` if neither configured, managed, nor on ``PATH``.

    Raises:
        ConfiguredToolError: An explicit config path was provided (or set in ``settings``) but
            does not point to an existing executable file.
    """
    plat = sys.platform if platform is None else platform
    config_var = TOOL_CONFIG_VARS[tool]

    if settings is None and tools_dir is None and explicit_path is None:
        settings = get_settings()

    configured = (
        explicit_path
        if explicit_path is not None
        else (_settings_explicit_path(settings, tool) if settings is not None else None)
    )
    if configured is not None:
        resolved, reason = _check_executable(configured, platform=plat)
        if resolved is None:
            raise ConfiguredToolError(tool, config_var, configured, reason=reason)
        return ResolvedTool(name=tool, path=resolved, source="config", config_var=config_var)

    active_tools_dir = tools_dir if tools_dir is not None else effective_tools_dir(settings)
    managed = managed_binary_path(tool, active_tools_dir, platform=plat)
    managed_resolved, _ = _check_executable(managed, platform=plat)
    if managed_resolved is not None:
        return ResolvedTool(name=tool, path=managed_resolved, source="managed")

    on_path = shutil.which(TOOL_COMMANDS[tool])
    if on_path is not None:
        return ResolvedTool(name=tool, path=Path(on_path), source="path")

    return None


def resolve_all_tools(
    settings: Settings | None = None,
) -> dict[ToolName, ResolvedTool | ConfiguredToolError | None]:
    """Resolve every managed tool, capturing any `ConfiguredToolError` per tool."""
    results: dict[ToolName, ResolvedTool | ConfiguredToolError | None] = {}
    for tool in ("ffmpeg", "ffprobe", "twitch_downloader"):
        name: ToolName = tool
        try:
            results[name] = resolve_tool(name, settings=settings)
        except ConfiguredToolError as exc:
            results[name] = exc
    return results
