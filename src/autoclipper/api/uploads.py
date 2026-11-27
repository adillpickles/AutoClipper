"""Streaming local VOD video upload endpoint and shared video file validation (§ 3.1, § 3.1a).

Large VOD files (4-6 hours, 5-20 GB) stream directly from ``request.stream()`` into a
``.part`` file under ``<artifacts_dir>/uploads/`` without buffering the body in memory or spooling
to an OS temp directory. Partial files are deleted on client disconnect, truncation, or failed
FFprobe validation, and local filesystem paths are never included in HTTP error responses.
"""

import asyncio
import logging
import math
import re
import secrets
from collections.abc import Callable, Sequence
from contextlib import suppress
from pathlib import Path
from typing import Final, Protocol

from fastapi import APIRouter, Request, Response
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect

from autoclipper.ffmpeg import FFmpegError, probe
from autoclipper.schemas import ApiError, ErrorResponse, VideoUpload

log = logging.getLogger(__name__)

ALLOWED_VIDEO_EXTENSIONS: Final[frozenset[str]] = frozenset(
    {
        ".mp4",
        ".mkv",
        ".ts",
        ".mts",
        ".m2ts",
        ".mov",
        ".webm",
        ".flv",
        ".m4v",
        ".avi",
        ".mpg",
        ".mpeg",
        ".wmv",
        ".ogv",
    }
)
UPLOAD_ID_PATTERN: Final = re.compile(r"^upl_[0-9a-f]{16}$")

_NO_STORE: Final = {"Cache-Control": "no-store"}
_JSON_TYPE: Final = "application/json; charset=utf-8"

_SUPPORTED_EXTENSIONS_LIST: Final = ".mp4, .mkv, .ts, .mov, .webm, .flv, or .avi"
_UNSUPPORTED_EXTENSION_MESSAGE: Final = (
    f"Unsupported video file type. Use a video file such as {_SUPPORTED_EXTENSIONS_LIST}."
)
_EMPTY_UPLOAD_MESSAGE: Final = "The uploaded video file is empty."
_TRUNCATED_UPLOAD_MESSAGE: Final = (
    "The video upload stopped before finishing. Try uploading it again."
)
_INVALID_UPLOAD_VIDEO_MESSAGE: Final = (
    "That file isn't a valid or readable video. Check that it's a complete video file and try "
    "again."
)


class UploadHost(Protocol):
    """Subset of `MockService` needed by the upload router."""

    @property
    def uploads_dir(self) -> Path:
        """Directory under artifacts where uploaded VOD videos are stored."""
        ...

    @property
    def probe_video(self) -> Callable[[Path], bool] | None:
        """Optional test override for probing uploaded/local video files."""
        ...

    def register_upload(
        self,
        *,
        upload_id: str,
        filename: str,
        path: Path,
        size_bytes: int,
        duration_s: float | None,
    ) -> VideoUpload:
        """Persist upload metadata and return the `VideoUpload` response model."""
        ...


def has_supported_video_extension(name_or_path: str | Path) -> bool:
    """Return True when the filename or path has a supported video container extension."""
    return Path(name_or_path).suffix.lower() in ALLOWED_VIDEO_EXTENSIONS


def sanitize_upload_filename(raw: str | None) -> str | None:
    """Return a safe basename for an uploaded filename, or None if missing/invalid."""
    if raw is None:
        return None
    # Split on both separators so Windows client paths sent to a POSIX server keep only basename.
    name = raw.replace("\\", "/").split("/")[-1].strip()
    if not name or name in {".", ".."}:
        return None
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in name):
        return None
    return name


def validate_video_file(
    path: Path, *, probe_video: Callable[[Path], bool] | None = None
) -> tuple[bool, float | None]:
    """Verify `path` is an existing, readable, non-empty video file with a supported extension.

    Returns ``(is_valid, duration_s)``. ``duration_s`` is populated when FFprobe runs and None
    when a boolean ``probe_video`` test double is used.
    """
    if not has_supported_video_extension(path):
        return False, None
    if not path.is_file():
        return False, None
    try:
        with path.open("rb") as handle:
            if not handle.read(1):
                return False, None
    except OSError:
        return False, None
    if probe_video is not None:
        return bool(probe_video(path)), None
    try:
        info = probe(path)
    except FFmpegError:
        return False, None
    if not info.video_streams or not math.isfinite(info.duration_s) or info.duration_s <= 0:
        return False, None
    return True, info.duration_s


def _json_response(model: VideoUpload, status_code: int) -> Response:
    return Response(
        content=model.model_dump_json(),
        status_code=status_code,
        media_type=_JSON_TYPE,
        headers=_NO_STORE,
    )


def _error_response(
    status_code: int,
    code: str,
    message: str,
    details: dict[str, object] | None = None,
) -> Response:
    body = ErrorResponse(error=ApiError(code=code, message=message, details=details))
    return Response(
        content=body.model_dump_json(),
        status_code=status_code,
        media_type=_JSON_TYPE,
        headers=_NO_STORE,
    )


def _parse_content_length(raw: str | None) -> int | None:
    if raw is None:
        return None
    stripped = raw.strip()
    if not stripped or not stripped.isdigit():
        raise ValueError("invalid Content-Length")
    return int(stripped)


def create_uploads_router(
    service: UploadHost,
    *,
    prefix: str = "/api/v1",
    allowed_origins: Sequence[str] = (),
) -> APIRouter:
    """Create the FastAPI router exposing ``POST /api/v1/uploads``."""
    normalized_origins = frozenset(o.strip().rstrip("/").lower() for o in allowed_origins if o)
    router = APIRouter(prefix=prefix)

    @router.post("/uploads", response_model=VideoUpload, status_code=201)
    async def upload_video(request: Request, filename: str | None = None) -> Response:
        origin = request.headers.get("origin")
        if (
            origin is not None
            and normalized_origins
            and origin.strip().rstrip("/").lower() not in normalized_origins
        ):
            return _error_response(
                403,
                "origin_not_allowed",
                "Video upload requests from this browser origin are not allowed.",
            )

        if filename is None or not filename.strip():
            return _error_response(
                422,
                "invalid_request",
                "The request is invalid: filename is required.",
                {"fields": ["filename"]},
            )

        unknown_fields = sorted(request.query_params.keys() - {"filename"})
        if unknown_fields:
            return _error_response(
                422,
                "invalid_request",
                "The request is invalid: unexpected upload query parameters.",
                {"fields": unknown_fields},
            )

        clean_name = sanitize_upload_filename(filename)
        if clean_name is None:
            return _error_response(
                422,
                "invalid_video_upload",
                "That filename isn't valid. Choose a video file and try again.",
            )

        if not has_supported_video_extension(clean_name):
            return _error_response(422, "invalid_video_upload", _UNSUPPORTED_EXTENSION_MESSAGE)

        try:
            expected_length = _parse_content_length(request.headers.get("content-length"))
        except ValueError:
            return _error_response(
                422,
                "invalid_request",
                "The request is invalid: Content-Length must be a non-negative integer.",
                {"fields": ["content-length"]},
            )

        if expected_length == 0:
            return _error_response(422, "invalid_video_upload", _EMPTY_UPLOAD_MESSAGE)

        uploads_dir = service.uploads_dir
        upload_id = f"upl_{secrets.token_hex(8)}"
        ext = Path(clean_name).suffix.lower()
        final_path = uploads_dir / f"{upload_id}{ext}"
        part_path = uploads_dir / f".{upload_id}.{secrets.token_hex(4)}.part{ext}"

        bytes_written = 0
        registered = False
        try:
            uploads_dir.mkdir(parents=True, exist_ok=True)
            handle = part_path.open("xb")
            try:
                async for chunk in request.stream():
                    if not chunk:
                        continue
                    await run_in_threadpool(handle.write, chunk)
                    bytes_written += len(chunk)
                await run_in_threadpool(handle.close)
            finally:
                if not handle.closed:
                    try:
                        handle.close()
                    except OSError:
                        log.exception(
                            "could not close partial video upload upload_id=%s", upload_id
                        )

            if bytes_written == 0:
                return _error_response(422, "invalid_video_upload", _EMPTY_UPLOAD_MESSAGE)

            if expected_length is not None and bytes_written != expected_length:
                log.info(
                    "video upload truncated upload_id=%s expected=%d received=%d",
                    upload_id,
                    expected_length,
                    bytes_written,
                )
                return _error_response(422, "invalid_video_upload", _TRUNCATED_UPLOAD_MESSAGE)

            # After the body is consumed, the next ASGI event can only be a disconnect.
            # Listen while FFprobe is running so middleware has time to deliver that event.
            disconnect = asyncio.ensure_future(request.receive())
            try:
                valid, duration_s = await run_in_threadpool(
                    validate_video_file, part_path, probe_video=service.probe_video
                )
                # Let a disconnect already passing through middleware reach the waiter.
                done, _ = await asyncio.wait((disconnect,), timeout=0.01)
                if done and disconnect.result()["type"] == "http.disconnect":
                    raise ClientDisconnect
            finally:
                if not disconnect.done():
                    disconnect.cancel()
                    with suppress(asyncio.CancelledError):
                        await disconnect
            if not valid:
                log.warning(
                    "uploaded video failed validation upload_id=%s path=%s size_bytes=%d",
                    upload_id,
                    part_path,
                    bytes_written,
                )
                return _error_response(422, "invalid_video_upload", _INVALID_UPLOAD_VIDEO_MESSAGE)

            await run_in_threadpool(part_path.replace, final_path)
            uploaded = await run_in_threadpool(
                service.register_upload,
                upload_id=upload_id,
                filename=clean_name,
                path=final_path,
                size_bytes=bytes_written,
                duration_s=duration_s,
            )
            registered = True
            log.info(
                "video uploaded upload_id=%s size_bytes=%d duration_s=%s",
                upload_id,
                bytes_written,
                duration_s,
            )
            return _json_response(uploaded, 201)
        except ClientDisconnect:
            log.info("video upload cancelled upload_id=%s bytes=%d", upload_id, bytes_written)
            return Response(status_code=499)
        except OSError as exc:
            log.warning("video upload I/O error upload_id=%s: %s", upload_id, exc)
            return _error_response(
                500,
                "upload_failed",
                "Could not save the uploaded video file. Check available disk space and try again.",
            )
        finally:
            for path in (part_path,) if registered else (part_path, final_path):
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    log.exception(
                        "could not remove upload file upload_id=%s path=%s", upload_id, path
                    )

    return router
