"""Managed external tool setup router and background worker (api-contract.md § 9.8).

Lives in its own module so ``POST /api/v1/tools/setup`` and ``GET /api/v1/tools/setup`` stay
independent of analysis/render job state in `autoclipper.api.service`. Responses never include
local filesystem paths.
"""

import logging
import threading
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from fastapi import APIRouter, Request, Response

from autoclipper.config import Settings, get_settings
from autoclipper.schemas import (
    ApiError,
    ErrorResponse,
    ToolSetupItem,
    ToolSetupName,
    ToolSetupRequest,
    ToolSetupStatus,
)
from autoclipper.tools.discovery import (
    ConfiguredToolError,
    effective_tools_dir,
    resolve_tool,
)
from autoclipper.tools.installer import (
    ChecksumMismatchError,
    InstallProgress,
    ProgressCallback,
    ToolArchiveError,
    ToolDownloadError,
    ToolInstallError,
    ToolInstallOutcome,
    install_tool,
)
from autoclipper.tools.manifest import (
    ToolGroup,
    UnsupportedPlatformError,
    detect_platform,
    get_tool_spec,
)

log = logging.getLogger(__name__)

_NO_STORE: Final = {"Cache-Control": "no-store"}
_JSON_TYPE: Final = "application/json; charset=utf-8"

_TOOL_LABELS: Final[dict[ToolSetupName, str]] = {
    "ffmpeg": "FFmpeg & FFprobe",
    "twitch_downloader": "TwitchDownloaderCLI",
}

type SingleToolInstaller = Callable[
    [ToolGroup, Path, bool, ProgressCallback | None],
    ToolInstallOutcome,
]


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


def _default_missing_tools(settings: Settings) -> tuple[ToolSetupName, ...]:
    """Return managed tool groups that are currently missing (ignoring explicit config errors)."""
    missing: list[ToolSetupName] = []

    ffmpeg_missing = False
    for name in ("ffmpeg", "ffprobe"):
        try:
            if resolve_tool(name, settings=settings) is None:
                ffmpeg_missing = True
        except ConfiguredToolError:
            # Explicit AUTOCLIPPER_FFMPEG_PATH / AUTOCLIPPER_FFPROBE_PATH overrides managed tools.
            ffmpeg_missing = False
            break
    if ffmpeg_missing:
        missing.append("ffmpeg")

    try:
        if resolve_tool("twitch_downloader", settings=settings) is None:
            missing.append("twitch_downloader")
    except ConfiguredToolError:
        pass

    return tuple(missing)


def _default_install_one(
    tool: ToolGroup,
    tools_dir: Path,
    force: bool,
    on_progress: ProgressCallback | None,
) -> ToolInstallOutcome:
    plat = detect_platform()
    spec = get_tool_spec(tool, plat)
    return install_tool(spec, tools_dir=tools_dir, force=force, on_progress=on_progress)


def _api_error_for(tool: ToolSetupName, exc: Exception) -> ApiError:
    """Turn an installer exception into a path-free `ApiError` safe for HTTP responses."""
    label = _TOOL_LABELS[tool]
    if isinstance(exc, UnsupportedPlatformError):
        return ApiError(code="unsupported_platform", message=str(exc))
    if isinstance(exc, ChecksumMismatchError):
        return ApiError(
            code="checksum_mismatch",
            message=(
                f"SHA-256 verification failed for {exc.filename}. The download was discarded; "
                "try again later or install manually."
            ),
        )
    if isinstance(exc, ToolDownloadError):
        return ApiError(
            code="download_failed",
            message=(
                f"Could not download {label}. Check your internet connection and try again, or "
                "install it manually."
            ),
        )
    if isinstance(exc, ToolArchiveError):
        return ApiError(
            code="archive_invalid",
            message=(
                f"The downloaded archive for {label} could not be extracted safely. "
                "Try again or install it manually."
            ),
        )
    if isinstance(exc, ToolInstallError):
        return ApiError(
            code="tool_install_failed",
            message=(
                f"Could not install {label} into the managed tools folder. Close any running "
                "processes using the binary and try again."
            ),
        )
    log.error("unexpected error installing managed tool=%s", tool, exc_info=exc)
    return ApiError(
        code="internal_error",
        message=f"Unexpected error while installing {label}. Check the server logs and try again.",
    )


class _SetupCancelled(Exception):
    """Raised inside the background worker when the API server is shutting down."""


class ToolSetupManager:
    """Runs at most one background managed-tool setup at a time and tracks live progress."""

    def __init__(
        self,
        *,
        settings_provider: Callable[[], Settings] = get_settings,
        missing_tools_fn: Callable[[Settings], Sequence[ToolSetupName]] | None = None,
        install_one_fn: SingleToolInstaller | None = None,
    ) -> None:
        self._settings_provider = settings_provider
        self._missing_tools_fn = (
            _default_missing_tools if missing_tools_fn is None else missing_tools_fn
        )
        self._install_one_fn = _default_install_one if install_one_fn is None else install_one_fn
        self._lock = threading.Lock()
        self._closed = threading.Event()
        self._worker: threading.Thread | None = None
        now = _utc_now()
        self._status = ToolSetupStatus(
            state="idle",
            items=(),
            error=None,
            started_at=None,
            updated_at=now,
            finished_at=None,
        )

    def status(self) -> ToolSetupStatus:
        """Return the latest tool-setup status snapshot."""
        with self._lock:
            return self._status

    def start(self, request: ToolSetupRequest | None = None) -> tuple[ToolSetupStatus, bool]:
        """Start installing requested (or missing) tools; return ``(status, started)``.

        If a setup run is already in progress, returns the current running status with
        ``started=False``. If no tools need installation (and ``force`` is False), returns a
        ``done`` status immediately with ``started=False``.
        """
        force = request.force if request is not None else False
        requested = request.tools if request is not None else None

        with self._lock:
            if self._closed.is_set() or self._status.state == "running":
                return self._status, False

            settings = self._settings_provider()
            if requested is not None:
                selected = tuple(dict.fromkeys(requested))
            else:
                selected = tuple(dict.fromkeys(self._missing_tools_fn(settings)))

            now = _utc_now()
            if not selected:
                self._status = ToolSetupStatus(
                    state="done",
                    items=(),
                    error=None,
                    started_at=now,
                    updated_at=now,
                    finished_at=now,
                )
                return self._status, False

            try:
                tools_dir = effective_tools_dir(settings)
            except RuntimeError:
                err = ApiError(
                    code="tool_install_failed",
                    message=(
                        "Could not determine the default managed tools folder. "
                        "Set AUTOCLIPPER_TOOLS_DIR in .env and restart the server."
                    ),
                )
                self._status = ToolSetupStatus(
                    state="failed",
                    items=(),
                    error=err,
                    started_at=now,
                    updated_at=now,
                    finished_at=now,
                )
                return self._status, False

            initial_items = tuple(
                ToolSetupItem(
                    tool=name,
                    label=_TOOL_LABELS[name],
                    stage="queued",
                    message=f"Queued {_TOOL_LABELS[name]}...",
                )
                for name in selected
            )
            self._status = ToolSetupStatus(
                state="running",
                items=initial_items,
                error=None,
                started_at=now,
                updated_at=now,
                finished_at=None,
            )
            worker = threading.Thread(
                target=self._run,
                args=(selected, tools_dir, force),
                name="autoclipper-tool-setup",
                daemon=True,
            )
            self._worker = worker
            worker.start()
            return self._status, True

    def _update_item(
        self, tool: ToolSetupName, updater: Callable[[ToolSetupItem], ToolSetupItem]
    ) -> None:
        with self._lock:
            updated_items = tuple(
                updater(item) if item.tool == tool else item for item in self._status.items
            )
            self._status = self._status.model_copy(
                update={"items": updated_items, "updated_at": _utc_now()}
            )

    def _run(
        self,
        selected: tuple[ToolSetupName, ...],
        tools_dir: Path,
        force: bool,
    ) -> None:
        first_error: ApiError | None = None
        succeeded = 0

        for tool in selected:
            if self._closed.is_set():
                first_error = first_error or ApiError(
                    code="setup_cancelled",
                    message="Tool setup was cancelled because the server is shutting down.",
                )
                break

            def _on_progress(event: InstallProgress, *, current: ToolSetupName = tool) -> None:
                if self._closed.is_set():
                    raise _SetupCancelled
                if event.stage == "failed":
                    # The exception handler below records a sanitized path-free error message.
                    return
                self._update_item(
                    current,
                    lambda item: ToolSetupItem(
                        tool=item.tool,
                        label=item.label,
                        stage=event.stage,
                        message=event.message,
                        bytes_downloaded=event.bytes_downloaded,
                        bytes_total=event.bytes_total,
                        fraction=event.fraction,
                        error=None,
                    ),
                )

            try:
                outcome = self._install_one_fn(tool, tools_dir, force, _on_progress)
            except _SetupCancelled:
                api_err = ApiError(
                    code="setup_cancelled",
                    message="Tool setup was cancelled because the server is shutting down.",
                )
                if first_error is None:
                    first_error = api_err
                self._update_item(
                    tool,
                    lambda item, err=api_err: ToolSetupItem(
                        tool=item.tool,
                        label=item.label,
                        stage="failed",
                        message=err.message,
                        bytes_downloaded=item.bytes_downloaded,
                        bytes_total=item.bytes_total,
                        fraction=None,
                        error=err,
                    ),
                )
                break
            except Exception as exc:
                api_err = _api_error_for(tool, exc)
                if first_error is None:
                    first_error = api_err
                self._update_item(
                    tool,
                    lambda item, err=api_err: ToolSetupItem(
                        tool=item.tool,
                        label=item.label,
                        stage="failed",
                        message=err.message,
                        bytes_downloaded=item.bytes_downloaded,
                        bytes_total=item.bytes_total,
                        fraction=None,
                        error=err,
                    ),
                )
            else:
                succeeded += 1
                final_stage = "skipped" if outcome.status == "already_installed" else "done"
                final_msg = (
                    f"{outcome.label} {outcome.version} is already installed."
                    if outcome.status == "already_installed"
                    else f"Installed {outcome.label} {outcome.version}."
                )
                self._update_item(
                    tool,
                    lambda item, stage=final_stage, msg=final_msg: ToolSetupItem(
                        tool=item.tool,
                        label=item.label,
                        stage=stage,
                        message=msg,
                        bytes_downloaded=item.bytes_downloaded,
                        bytes_total=item.bytes_total,
                        fraction=1.0 if stage == "done" else None,
                        error=None,
                    ),
                )

        finished = _utc_now()
        with self._lock:
            if first_error is None:
                self._status = self._status.model_copy(
                    update={
                        "state": "done",
                        "error": None,
                        "updated_at": finished,
                        "finished_at": finished,
                    }
                )
            else:
                summary_error = first_error
                if succeeded > 0:
                    summary_error = ApiError(
                        code=first_error.code,
                        message=(
                            f"Installed {succeeded} of {len(selected)} tool(s). "
                            f"{first_error.message}"
                        ),
                    )
                self._status = self._status.model_copy(
                    update={
                        "state": "failed",
                        "error": summary_error,
                        "updated_at": finished,
                        "finished_at": finished,
                    }
                )

    def close(self, *, timeout_s: float = 5.0) -> None:
        """Signal cancellation and wait for any in-flight tool setup worker to exit cleanly."""
        self._closed.set()
        with self._lock:
            worker = self._worker
        if worker is not None and worker.is_alive():
            worker.join(timeout=timeout_s)


def _json_response(model: ToolSetupStatus, status_code: int) -> Response:
    return Response(
        content=model.model_dump_json(),
        status_code=status_code,
        media_type=_JSON_TYPE,
        headers=_NO_STORE,
    )


def _origin_forbidden() -> Response:
    body = ErrorResponse(
        error=ApiError(
            code="origin_not_allowed",
            message="Tool setup requests from this browser origin are not allowed.",
        )
    )
    return Response(
        content=body.model_dump_json(),
        status_code=403,
        media_type=_JSON_TYPE,
        headers=_NO_STORE,
    )


def create_tools_router(
    manager: ToolSetupManager,
    *,
    prefix: str = "/api/v1",
    allowed_origins: Sequence[str] = (),
) -> APIRouter:
    """Create the FastAPI router exposing ``GET`` and ``POST /api/v1/tools/setup``.

    When ``allowed_origins`` is non-empty, ``POST /tools/setup`` rejects browser requests whose
    ``Origin`` header is not in the allowlist (preventing unpreflighted cross-origin POSTs from
    triggering downloads). Requests without an ``Origin`` header (CLI/local tools) are allowed.
    """
    normalized_origins = frozenset(o.strip().rstrip("/").lower() for o in allowed_origins if o)
    router = APIRouter(prefix=prefix)

    @router.get("/tools/setup", response_model=ToolSetupStatus)
    def get_tool_setup() -> Response:
        return _json_response(manager.status(), 200)

    @router.post("/tools/setup", response_model=ToolSetupStatus, status_code=202)
    def start_tool_setup(
        http_request: Request,
        request: ToolSetupRequest | None = None,
    ) -> Response:
        origin = http_request.headers.get("origin")
        if (
            origin is not None
            and normalized_origins
            and origin.strip().rstrip("/").lower() not in normalized_origins
        ):
            return _origin_forbidden()
        status, started = manager.start(request)
        return _json_response(status, 202 if started else 200)

    return router
