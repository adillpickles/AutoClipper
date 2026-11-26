"""Startup and CLI preflight checks for local AutoClipper environments.

Checks the external tools, optional extras and secrets that ``autoclipper serve`` and the
pipeline commands need, without making any network calls, loading ML models or revealing secret
values or local filesystem paths.
"""

import importlib
from typing import Final, Literal

from autoclipper.config import MissingSettingError, Settings
from autoclipper.ingest.chat import (
    PINNED_TOOL_VERSION,
    TOOL_NAME,
)
from autoclipper.schemas import PreflightCheck, PreflightReport, PreflightStatus
from autoclipper.tools.discovery import (
    TOOL_COMMANDS,
    ConfiguredToolError,
    ResolvedTool,
    ToolName,
    ToolSource,
    effective_tools_dir,
    resolve_all_tools,
    resolve_tool,
)

type RunnerMode = Literal["mock", "pipeline"]

_CAPTIONS_MODULE: Final = "faster_whisper"
_TWITCH_DOWNLOADER_RELEASE_URL: Final = (
    f"https://github.com/lay295/TwitchDownloader/releases/tag/{PINNED_TOOL_VERSION}"
)
_STATUS_TAGS: Final[dict[PreflightStatus, str]] = {
    "ok": "[OK]  ",
    "warn": "[WARN]",
    "fail": "[FAIL]",
}
_SOURCE_LABELS: Final[dict[ToolSource, str]] = {
    "config": "configured path",
    "managed": "managed tools folder",
    "path": "PATH",
}


def _describe_tool_source(resolved: ResolvedTool) -> str:
    if resolved.source == "config" and resolved.config_var:
        return f"configured via {resolved.config_var}"
    return _SOURCE_LABELS[resolved.source]


def check_ffmpeg(settings: Settings | None = None) -> PreflightCheck:
    """Check that both ``ffmpeg`` and ``ffprobe`` are discoverable and report which copy is used."""
    resolved_tools: dict[str, ResolvedTool] = {}
    config_errors: list[ConfiguredToolError] = []
    missing: list[str] = []

    ffmpeg_targets: tuple[tuple[ToolName, str], ...] = (
        ("ffmpeg", "ffmpeg"),
        ("ffprobe", "ffprobe"),
    )
    for tool_name, label in ffmpeg_targets:
        try:
            found = resolve_tool(tool_name, settings=settings)
        except ConfiguredToolError as exc:
            config_errors.append(exc)
        else:
            if found is None:
                missing.append(label)
            else:
                resolved_tools[tool_name] = found

    if config_errors:
        message_parts = [exc.safe_message for exc in config_errors]
        if missing:
            verb = "is" if len(missing) == 1 else "are"
            message_parts.append(
                f"{' and '.join(missing)} {verb} not found in the managed tools folder or on PATH."
            )
        vars_text = " and ".join(exc.config_var for exc in config_errors)
        return PreflightCheck(
            name="ffmpeg",
            label="FFmpeg & FFprobe",
            status="fail",
            message=" ".join(message_parts),
            fix=f"Update or unset {vars_text} in .env or the environment.",
            source="config",
        )

    if not missing:
        ffmpeg_tool = resolved_tools["ffmpeg"]
        ffprobe_tool = resolved_tools["ffprobe"]
        if ffmpeg_tool.source == ffprobe_tool.source:
            source = ffmpeg_tool.source
            if source == "config":
                where = f"configured via {ffmpeg_tool.config_var} and {ffprobe_tool.config_var}"
            else:
                where = _SOURCE_LABELS[source]
            message = f"FFmpeg and FFprobe are available ({where})."
        else:
            source = "mixed"
            message = (
                f"FFmpeg ({_describe_tool_source(ffmpeg_tool)}) and "
                f"FFprobe ({_describe_tool_source(ffprobe_tool)}) are available."
            )
        return PreflightCheck(
            name="ffmpeg",
            label="FFmpeg & FFprobe",
            status="ok",
            message=message,
            fix=None,
            source=source,
        )

    verb = "is" if len(missing) == 1 else "are"
    return PreflightCheck(
        name="ffmpeg",
        label="FFmpeg & FFprobe",
        status="fail",
        message=f"{' and '.join(missing)} {verb} not found in the managed tools folder or on PATH.",
        fix=(
            "Run `uv run autoclipper setup-tools --tool ffmpeg`, or install FFmpeg manually "
            "(Windows: winget install Gyan.FFmpeg, macOS: brew install ffmpeg, "
            "Linux: sudo apt install ffmpeg), or set AUTOCLIPPER_FFMPEG_PATH / "
            "AUTOCLIPPER_FFPROBE_PATH."
        ),
        source=None,
    )


def check_twitch_downloader(settings: Settings | None = None) -> PreflightCheck:
    """Check that ``TwitchDownloaderCLI`` is discoverable and report which copy is used."""
    try:
        found = resolve_tool("twitch_downloader", settings=settings)
    except ConfiguredToolError as exc:
        return PreflightCheck(
            name="twitch_downloader",
            label="TwitchDownloaderCLI",
            status="warn",
            message=(
                f"{exc.safe_message} Chat replay and automatic video segment downloads are "
                "unavailable (local --video files still work)."
            ),
            fix=f"Update or unset {exc.config_var} in .env or the environment.",
            source="config",
        )
    if found is None:
        return PreflightCheck(
            name="twitch_downloader",
            label="TwitchDownloaderCLI",
            status="warn",
            message=(
                f"{TOOL_NAME} is not found in the managed tools folder or on PATH; chat replay "
                "and automatic video segment downloads are unavailable (local --video files "
                "still work)."
            ),
            fix=(
                "Run `uv run autoclipper setup-tools --tool twitch-downloader`, or download "
                f"{TOOL_NAME} {PINNED_TOOL_VERSION} from {_TWITCH_DOWNLOADER_RELEASE_URL} "
                "and add it to PATH or set AUTOCLIPPER_TWITCH_DOWNLOADER_PATH."
            ),
            source=None,
        )
    return PreflightCheck(
        name="twitch_downloader",
        label="TwitchDownloaderCLI",
        status="ok",
        message=f"{TOOL_NAME} is available ({_describe_tool_source(found)}).",
        fix=None,
        source=found.source,
    )


def check_chat_hash_salt(
    settings: Settings, *, job_runner: RunnerMode = "pipeline"
) -> PreflightCheck:
    """Check that ``AUTOCLIPPER_CHAT_HASH_SALT`` is configured (presence only)."""
    try:
        salt = settings.require_chat_hash_salt()
    except MissingSettingError:
        salt = None
    if salt is not None and salt.get_secret_value().strip():
        return PreflightCheck(
            name="chat_hash_salt",
            label="Chat hash salt",
            status="ok",
            message="AUTOCLIPPER_CHAT_HASH_SALT is configured.",
            fix=None,
        )
    if job_runner == "mock":
        message = (
            "AUTOCLIPPER_CHAT_HASH_SALT is not set (not needed in mock mode, required for "
            "chat and viewer-clip pseudonymization on real VODs)."
        )
    else:
        message = (
            "AUTOCLIPPER_CHAT_HASH_SALT is not set; chat and viewer clips will be unavailable "
            "(analysis degrades to audio only)."
        )
    return PreflightCheck(
        name="chat_hash_salt",
        label="Chat hash salt",
        status="warn",
        message=message,
        fix=(
            "Set AUTOCLIPPER_CHAT_HASH_SALT in .env (generate one with: "
            'uv run python -c "import secrets; print(secrets.token_hex(32))").'
        ),
    )


def check_captions() -> PreflightCheck:
    """Check that the optional ``captions`` extra (``faster-whisper``) is importable.

    Imports the package without instantiating ``WhisperModel``, so no model weights are
    downloaded or loaded.
    """
    try:
        importlib.import_module(_CAPTIONS_MODULE)
    except Exception:
        return PreflightCheck(
            name="captions",
            label="Captions extra",
            status="warn",
            message=(
                "faster-whisper is not installed or failed to import; exports and highlights "
                "work without captions, but burned-in captions are unavailable."
            ),
            fix="Install the captions extra with: uv sync --extra captions",
        )
    return PreflightCheck(
        name="captions",
        label="Captions extra",
        status="ok",
        message="faster-whisper is installed for burned-in captions.",
        fix=None,
    )


def check_twitch_credentials(
    settings: Settings, *, job_runner: RunnerMode = "pipeline"
) -> PreflightCheck:
    """Check that Twitch Helix credentials are configured (presence only, no network call)."""
    try:
        creds = settings.require_twitch_credentials()
    except MissingSettingError as exc:
        missing = " and ".join(exc.names)
        verb = "is" if len(exc.names) == 1 else "are"
        if job_runner == "mock":
            return PreflightCheck(
                name="twitch_credentials",
                label="Twitch API credentials",
                status="warn",
                message=(
                    f"{missing} {verb} not set (not needed in mock mode, required for real VOD "
                    "analysis)."
                ),
                fix=f"Set {missing} in .env (copy .env.example) or in the environment.",
            )
        return PreflightCheck(
            name="twitch_credentials",
            label="Twitch API credentials",
            status="fail",
            message=(
                f"{missing} {verb} not set; real VOD analysis requires Twitch Helix credentials."
            ),
            fix=(
                f"Set {missing} in .env (copy .env.example) or in the environment, or pass "
                "--jobs mock for built-in scenarios."
            ),
        )
    # Guard against directly constructed Settings with whitespace-only SecretStr values.
    missing_blank = [
        name
        for name, value in (
            ("TWITCH_CLIENT_ID", creds.client_id),
            ("TWITCH_CLIENT_SECRET", creds.client_secret),
        )
        if not value.get_secret_value().strip()
    ]
    if missing_blank:
        missing = " and ".join(missing_blank)
        verb = "is" if len(missing_blank) == 1 else "are"
        status: PreflightStatus = "warn" if job_runner == "mock" else "fail"
        return PreflightCheck(
            name="twitch_credentials",
            label="Twitch API credentials",
            status=status,
            message=f"{missing} {verb} blank; real VOD analysis requires Twitch Helix credentials.",
            fix=f"Set {missing} in .env (copy .env.example) or in the environment.",
        )
    return PreflightCheck(
        name="twitch_credentials",
        label="Twitch API credentials",
        status="ok",
        message="TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET are configured.",
        fix=None,
    )


def _worst_status(checks: tuple[PreflightCheck, ...]) -> PreflightStatus:
    if any(check.status == "fail" for check in checks):
        return "fail"
    if any(check.status == "warn" for check in checks):
        return "warn"
    return "ok"


def run_preflight(settings: Settings, *, job_runner: RunnerMode = "pipeline") -> PreflightReport:
    """Run all local readiness checks and summarize their worst status."""
    checks = (
        check_ffmpeg(settings),
        check_twitch_downloader(settings),
        check_chat_hash_salt(settings, job_runner=job_runner),
        check_captions(),
        check_twitch_credentials(settings, job_runner=job_runner),
    )
    return PreflightReport(status=_worst_status(checks), checks=checks)


def format_preflight_summary(report: PreflightReport) -> str:
    """Format a `PreflightReport` as ASCII-safe lines for CLI output."""
    lines = ["Preflight checks:"]
    for check in report.checks:
        tag = _STATUS_TAGS[check.status]
        lines.append(f"  {tag} {check.label}: {check.message}")
        if check.fix is not None:
            lines.append(f"         Fix: {check.fix}")
    return "\n".join(lines)


def format_tool_locations(settings: Settings) -> str:
    """Format local tool paths for CLI ``doctor`` output (never sent over HTTP)."""
    try:
        tools_dir_label = str(effective_tools_dir(settings))
    except RuntimeError:
        tools_dir_label = "<unavailable>"
    resolved = resolve_all_tools(settings)
    lines = [f"Tool locations (managed folder: {tools_dir_label}):"]
    for tool_name in ("ffmpeg", "ffprobe", "twitch_downloader"):
        label = TOOL_COMMANDS[tool_name]
        entry = resolved[tool_name]
        if isinstance(entry, ResolvedTool):
            lines.append(f"  {label:<19} [{entry.source}] {entry.path}")
        elif isinstance(entry, ConfiguredToolError):
            lines.append(f"  {label:<19} [config error] {entry.configured_path} ({entry.reason})")
        else:
            lines.append(f"  {label:<19} [missing]")
    return "\n".join(lines)
