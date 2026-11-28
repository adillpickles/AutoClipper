"""The FastAPI app behind ``autoclipper serve`` (docs/architecture/api-contract.md).

`create_app` wires the routes of § 2 to a `MockService`, replaces FastAPI's default error
bodies with the contract's envelope (§ 8), allows the configured browser origins (§ 9.4) and
marks status responses ``Cache-Control: no-store`` (§ 9.1).
"""

import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Generator, Sequence
from contextlib import asynccontextmanager
from typing import Annotated, Final
from urllib.parse import quote

from fastapi import FastAPI, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import Receive, Scope, Send

from autoclipper.analysis import AnalysisResult
from autoclipper.api.channels import HelixFactory, register_channel_routes
from autoclipper.api.origins import OriginCheck, origin_forbidden
from autoclipper.api.service import ApiException, MockService
from autoclipper.api.tools import ToolSetupManager, create_tools_router
from autoclipper.api.uploads import create_uploads_router
from autoclipper.config import get_settings
from autoclipper.export.archive import stream_archive
from autoclipper.preflight import run_preflight
from autoclipper.schemas import (
    ApiError,
    AppSettings,
    ArchiveKind,
    ArchiveStatus,
    CandidateMedia,
    CreateExportRequest,
    CreateHighlightsRequest,
    CreateJobRequest,
    DeleteJobResponse,
    ErrorResponse,
    Export,
    Health,
    HighlightsVideo,
    Job,
    JobListResponse,
    JobStatusRequest,
    JobSummary,
    PreflightReport,
    State,
    UpdateSettingsRequest,
)

log = logging.getLogger(__name__)

API_PREFIX: Final = "/api/v1"
DEFAULT_CORS_ORIGINS: Final = ("http://localhost:3000", "http://127.0.0.1:3000")
_NO_STORE: Final = {"Cache-Control": "no-store"}
_INTERNAL: Final = "Something went wrong on the server. Try again."
_JSON_TYPE: Final = "application/json; charset=utf-8"
DEFAULT_JOBS_LIMIT: Final = 20
MAX_JOBS_LIMIT: Final = 100


class _ListJobsQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    limit: int = Field(default=DEFAULT_JOBS_LIMIT, ge=1, le=MAX_JOBS_LIMIT)
    offset: int = Field(default=0, ge=0)
    before: str | None = Field(default=None, max_length=128)
    q: str | None = None
    state: State | None = None


class _DeleteJobQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    delete_exports: bool = False


def _error(status: int, error: ApiError, headers: dict[str, str] | None = None) -> JSONResponse:
    body = ErrorResponse(error=error).model_dump(mode="json")
    return JSONResponse(body, status_code=status, headers=headers, media_type=_JSON_TYPE)


def _attachment(filename: str) -> str:
    """A ``Content-Disposition`` value in the same form FileResponse uses for downloads."""
    quoted = quote(filename)
    if quoted != filename:
        return f"attachment; filename*=utf-8''{quoted}"
    return f'attachment; filename="{filename}"'


class _ClosingStreamingResponse(StreamingResponse):
    """Streams a sync generator and always closes it when the response ends.

    Starlette stops pulling from the generator when the client disconnects but never closes
    it, and on ASGI 2.4 servers it skips background tasks then too. Closing it here runs the
    generator's cleanup right away, so the files it holds open are released (on Windows an
    open file can't be deleted).
    """

    def __init__(
        self, content: Generator[bytes], *, media_type: str, headers: dict[str, str]
    ) -> None:
        super().__init__(content, media_type=media_type, headers=headers)
        self._source = content

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            await run_in_threadpool(self._close_source)

    def _close_source(self) -> None:
        try:
            self._source.close()
        except ValueError:  # still running in a worker thread; its file stays open until it ends
            log.warning("could not close a streamed download: its generator is still running")


def _json(model: BaseModel, status: int = 200, headers: dict[str, str] | None = None) -> Response:
    return Response(
        content=model.model_dump_json(),
        status_code=status,
        media_type=_JSON_TYPE,
        headers=headers,
    )


def _invalid_request_message(errors: Sequence[object]) -> tuple[str, list[str]]:
    fields: list[str] = []
    first = ""
    for error in errors:
        if not isinstance(error, dict):
            continue
        kind = str(error.get("type", ""))
        # A JSON syntax error's location is a byte offset, not a field.
        loc = (
            [] if kind == "json_invalid" else [str(p) for p in error.get("loc", ()) if p != "body"]
        )
        name = ".".join(loc)
        if name and name not in fields:
            fields.append(name)
        if not first:
            if kind == "json_invalid":
                first = "the body isn't valid JSON"
            elif kind == "missing" and not name:
                first = "the body is missing"
            elif kind == "missing":
                first = f"{name} is required"
            elif kind == "extra_forbidden":
                first = f"{name} isn't a known field"
            elif kind.startswith("bool"):
                first = f"{name} must be true or false"
            elif name:
                first = f"{name} has an invalid value"
    return f"The request is invalid: {first or 'check the fields'}.", fields


def create_app(
    service: MockService,
    *,
    cors_origins: Sequence[str] = DEFAULT_CORS_ORIGINS,
    preflight: Callable[[], PreflightReport] | None = None,
    helix_factory: HelixFactory | None = None,
    tool_setup: ToolSetupManager | None = None,
) -> FastAPI:
    """Build the API around `service`; closing the app stops its workers."""
    close_channels: Callable[[], None] | None = None
    active_tool_setup = ToolSetupManager() if tool_setup is None else tool_setup

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        if close_channels is not None:
            close_channels()
        active_tool_setup.close()
        service.close()

    app = FastAPI(
        title="AutoClipper API",
        version="1.0",
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url=None,
        # A trailing slash gets the 404 envelope, not a redirect without one (§ 8).
        redirect_slashes=False,
    )

    # Registered before CORSMiddleware, so it runs inside it: a 500 keeps its CORS headers and
    # the browser can read the envelope. (Starlette's own exception handler for `Exception` runs
    # outside every middleware.)
    @app.middleware("http")
    async def _catch_all(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        try:
            return await call_next(request)
        except Exception as exc:
            log.error("unhandled API error: %r", exc, exc_info=exc)
            return _error(500, ApiError(code="internal_error", message=_INTERNAL))

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(cors_origins),
        allow_methods=["GET", "POST", "PUT", "DELETE"],
        allow_headers=["Content-Type"],
        allow_credentials=False,
        expose_headers=["Location", "Content-Disposition"],
    )

    @app.exception_handler(ApiException)
    async def _api_error(_: Request, exc: ApiException) -> JSONResponse:
        return _error(exc.status, exc.error)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        message, fields = _invalid_request_message(list(exc.errors()))
        return _error(
            422, ApiError(code="invalid_request", message=message, details={"fields": fields})
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        if exc.status_code == 405:
            error = ApiError(code="method_not_allowed", message="This action isn't supported.")
        elif exc.status_code == 404:
            error = ApiError(code="not_found", message="There's nothing at this address.")
        else:
            error = ApiError(code="internal_error", message=_INTERNAL)
        return _error(exc.status_code, error, headers=getattr(exc, "headers", None))

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        log.error("unhandled API error: %r", exc, exc_info=exc)
        return _error(500, ApiError(code="internal_error", message=_INTERNAL))

    @app.get(f"{API_PREFIX}/health", response_model=Health)
    def health() -> Response:
        return _json(
            Health(
                job_runner=service.job_runner,
                captions_available=service.captions_available,
            )
        )

    @app.get(f"{API_PREFIX}/preflight", response_model=PreflightReport)
    def get_preflight() -> Response:
        report = (
            preflight()
            if preflight is not None
            else run_preflight(get_settings(), job_runner=service.job_runner)
        )
        return _json(report, headers=_NO_STORE)

    app.include_router(
        create_tools_router(active_tool_setup, prefix=API_PREFIX, allowed_origins=cors_origins)
    )
    app.include_router(
        create_uploads_router(service, prefix=API_PREFIX, allowed_origins=cors_origins)
    )

    settings_origins = OriginCheck(cors_origins)

    @app.get(f"{API_PREFIX}/settings", response_model=AppSettings)
    def get_app_settings() -> Response:
        return _json(service.get_settings(), headers=_NO_STORE)

    @app.put(f"{API_PREFIX}/settings", response_model=AppSettings)
    def update_app_settings(http_request: Request, request: UpdateSettingsRequest) -> Response:
        # A page on another origin must not point exports at a folder of its choosing.
        if not settings_origins.allows(http_request):
            return origin_forbidden("Settings changes from this browser origin are not allowed.")
        return _json(service.update_settings(request), headers=_NO_STORE)

    @app.get(f"{API_PREFIX}/jobs", response_model=JobListResponse)
    def list_jobs(query: Annotated[_ListJobsQuery, Query()]) -> Response:
        return _json(
            service.list_jobs(
                limit=query.limit,
                offset=query.offset,
                before=query.before,
                q=query.q,
                state=query.state,
            ),
            headers=_NO_STORE,
        )

    @app.post(f"{API_PREFIX}/jobs/status", response_model=tuple[JobSummary, ...])
    def job_status(request: JobStatusRequest) -> Response:
        return JSONResponse(
            [job.model_dump(mode="json") for job in service.job_status(request.job_ids)],
            headers=_NO_STORE,
            media_type=_JSON_TYPE,
        )

    @app.post(f"{API_PREFIX}/jobs", response_model=Job, status_code=201)
    def create_job(request: CreateJobRequest) -> Response:
        job, created = service.create_job(request)
        if not created:
            return _json(job, 200, _NO_STORE)
        return _json(job, 201, {"Location": f"{API_PREFIX}/jobs/{job.job_id}", **_NO_STORE})

    @app.get(f"{API_PREFIX}/jobs/{{job_id}}", response_model=Job)
    def get_job(job_id: str) -> Response:
        return _json(service.get_job(job_id), headers=_NO_STORE)

    @app.delete(f"{API_PREFIX}/jobs/{{job_id}}", response_model=DeleteJobResponse)
    def delete_job(job_id: str, query: Annotated[_DeleteJobQuery, Query()]) -> Response:
        return _json(
            service.delete_job(job_id, delete_exports=query.delete_exports),
            headers=_NO_STORE,
        )

    @app.get(f"{API_PREFIX}/jobs/{{job_id}}/result", response_model=AnalysisResult)
    def get_result(job_id: str) -> Response:
        return _json(service.get_result(job_id))

    @app.get(f"{API_PREFIX}/jobs/{{job_id}}/archives/{{kind}}", response_model=ArchiveStatus)
    def get_archive(job_id: str, kind: ArchiveKind) -> Response:
        plan = service.export_archive(job_id, kind)
        status = ArchiveStatus(
            kind=kind,
            ready_count=len(plan.members),
            omitted=plan.omitted,
            filename=plan.filename,
            download_url=(
                f"{API_PREFIX}/jobs/{quote(job_id, safe='')}/archives/{kind}/file"
                if plan.members
                else None
            ),
        )
        return _json(status, headers=_NO_STORE)

    @app.get(f"{API_PREFIX}/jobs/{{job_id}}/archives/{{kind}}/file")
    def archive_file(job_id: str, kind: ArchiveKind) -> Response:
        plan = service.export_archive(job_id, kind)
        if not plan.members:
            raise ApiException(
                409,
                "archive_empty",
                "None of these renders have finished yet, so there's nothing to download.",
            )
        # A sync iterator: Starlette pulls each chunk in its thread pool, off the event loop.
        return _ClosingStreamingResponse(
            stream_archive(plan),
            media_type="application/zip",
            headers={"Content-Disposition": _attachment(plan.filename), **_NO_STORE},
        )

    @app.get(
        f"{API_PREFIX}/jobs/{{job_id}}/candidates/{{candidate_id}}/media",
        response_model=CandidateMedia,
    )
    def get_media(job_id: str, candidate_id: str) -> Response:
        return _json(service.candidate_media(job_id, candidate_id), headers=_NO_STORE)

    @app.get(f"{API_PREFIX}/media/{{media_id}}")
    def media_file(media_id: str) -> FileResponse:
        # FileResponse answers Range requests with 206, 416 and 400 itself (§ 5.1).
        return FileResponse(service.media_path(media_id), media_type="video/mp4")

    @app.post(f"{API_PREFIX}/exports", response_model=Export, status_code=201)
    def create_export(request: CreateExportRequest) -> Response:
        export, created = service.create_export(request)
        if not created:
            return _json(export, 200, _NO_STORE)
        headers = {"Location": f"{API_PREFIX}/exports/{export.export_id}", **_NO_STORE}
        return _json(export, 201, headers)

    @app.get(f"{API_PREFIX}/exports/{{export_id}}", response_model=Export)
    def get_export(export_id: str) -> Response:
        return _json(service.get_export(export_id), headers=_NO_STORE)

    @app.get(f"{API_PREFIX}/exports/{{export_id}}/file")
    def export_file(export_id: str) -> FileResponse:
        path, name = service.export_file(export_id)
        return FileResponse(path, media_type="video/mp4", filename=name)

    @app.post(f"{API_PREFIX}/highlights", response_model=HighlightsVideo, status_code=201)
    def create_highlights(request: CreateHighlightsRequest) -> Response:
        video, created = service.create_highlights(request)
        if not created:
            return _json(video, 200, _NO_STORE)
        headers = {"Location": f"{API_PREFIX}/highlights/{video.highlights_id}", **_NO_STORE}
        return _json(video, 201, headers)

    @app.get(f"{API_PREFIX}/highlights/{{highlights_id}}", response_model=HighlightsVideo)
    def get_highlights(highlights_id: str) -> Response:
        return _json(service.get_highlights(highlights_id), headers=_NO_STORE)

    @app.get(f"{API_PREFIX}/highlights/{{highlights_id}}/file")
    def highlights_file(highlights_id: str) -> FileResponse:
        path, name = service.highlights_file(highlights_id)
        return FileResponse(path, media_type="video/mp4", filename=name)

    close_channels = register_channel_routes(
        app, service, api_prefix=API_PREFIX, helix_factory=helix_factory
    )

    return app
