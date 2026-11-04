"""The FastAPI app behind ``autoclipper serve`` (docs/architecture/api-contract.md).

`create_app` wires the routes of § 2 to a `MockService`, replaces FastAPI's default error
bodies with the contract's envelope (§ 8), allows the configured browser origins (§ 9.4) and
marks status responses ``Cache-Control: no-store`` (§ 9.1).
"""

import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from typing import Final

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from autoclipper.analysis import AnalysisResult
from autoclipper.api.service import ApiException, MockService
from autoclipper.schemas import (
    ApiError,
    CandidateMedia,
    CreateExportRequest,
    CreateHighlightsRequest,
    CreateJobRequest,
    ErrorResponse,
    Export,
    Health,
    HighlightsVideo,
    Job,
)

log = logging.getLogger(__name__)

API_PREFIX: Final = "/api/v1"
DEFAULT_CORS_ORIGINS: Final = ("http://localhost:3000", "http://127.0.0.1:3000")
_NO_STORE: Final = {"Cache-Control": "no-store"}
_INTERNAL: Final = "Something went wrong on the server. Try again."
_JSON_TYPE: Final = "application/json; charset=utf-8"


def _error(status: int, error: ApiError, headers: dict[str, str] | None = None) -> JSONResponse:
    body = ErrorResponse(error=error).model_dump(mode="json")
    return JSONResponse(body, status_code=status, headers=headers, media_type=_JSON_TYPE)


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
    service: MockService, *, cors_origins: Sequence[str] = DEFAULT_CORS_ORIGINS
) -> FastAPI:
    """Build the API around `service`; closing the app stops its workers."""

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
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
        allow_methods=["GET", "POST"],
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
        return _json(Health(job_runner="mock"))

    @app.post(f"{API_PREFIX}/jobs", response_model=Job, status_code=201)
    def create_job(request: CreateJobRequest) -> Response:
        job, created = service.create_job(request)
        if not created:
            return _json(job, 200, _NO_STORE)
        return _json(job, 201, {"Location": f"{API_PREFIX}/jobs/{job.job_id}", **_NO_STORE})

    @app.get(f"{API_PREFIX}/jobs/{{job_id}}", response_model=Job)
    def get_job(job_id: str) -> Response:
        return _json(service.get_job(job_id), headers=_NO_STORE)

    @app.get(f"{API_PREFIX}/jobs/{{job_id}}/result", response_model=AnalysisResult)
    def get_result(job_id: str) -> Response:
        return _json(service.get_result(job_id))

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

    return app
