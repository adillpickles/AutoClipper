"""Request and response models of the backend API (docs/architecture/api-contract.md).

FastAPI-free, so the CLI and tests can use them too. `AnalysisResult` and its parts live in
`autoclipper.analysis` and are re-exported here. Requests are strict (`ConfigDict(strict=True,
extra="forbid")`, enums as `Literal`); responses always serialize every field, ``null`` for an
empty optional value.
"""

import re
from typing import Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from autoclipper.analysis import (
    AnalysisResult,
    AnalysisWarning,
    Candidate,
    FiniteFloat,
    ScorerInfo,
    UtcDatetime,
    VodSummary,
)

__all__ = [
    "AnalysisResult",
    "AnalysisWarning",
    "ApiError",
    "Candidate",
    "CandidateMedia",
    "CreateExportRequest",
    "CreateHighlightsRequest",
    "CreateJobRequest",
    "ErrorResponse",
    "Export",
    "ExportAvailability",
    "Health",
    "HighlightsSegment",
    "HighlightsVideo",
    "Job",
    "PreviewMedia",
    "Progress",
    "ScorerInfo",
    "VodSummary",
    "parse_vod",
]

SCHEMA_VERSION: Final = "1.0"

type State = Literal["queued", "running", "done", "failed"]
type Layout = Literal["center_crop", "configured_region"]
type Order = Literal["chronological", "rank"]

# api-contract.md § 3.1, case-insensitive; the ID is in group 1 or 2.
VOD_PATTERN: Final = re.compile(
    r"^(?:(?:https?://)?(?:www\.|m\.)?twitch\.tv/videos/([0-9]+)/?(?:[?#].*)?|([0-9]+))$",
    re.IGNORECASE,
)


def parse_vod(text: str) -> str | None:
    """The video ID in a VOD link or bare ID (§ 3.1), or None if it isn't one."""
    match = VOD_PATTERN.match(text.strip())
    if match is None:
        return None
    video_id = match.group(1) or match.group(2)
    # Only ASCII digits: `[0-9]` with IGNORECASE matches nothing else, but be explicit.
    return video_id if video_id.isascii() else None


class _Response(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class _Request(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)


class ApiError(_Response):
    """The inner error object (§ 8)."""

    code: str
    message: str
    details: dict[str, object] | None = None


class ErrorResponse(_Response):
    """Every non-2xx body (§ 8)."""

    error: ApiError


class Progress(_Response):
    """Where running work is (§ 3.4)."""

    stage: str
    stage_index: int = Field(ge=1)
    stage_count: int = Field(ge=1)
    stage_fraction: Annotated[float, Field(ge=0, le=1)] | None = None
    message: str


class CreateJobRequest(_Request):
    """``POST /api/v1/jobs`` (§ 3.1)."""

    vod: str
    video_path: str | None = None


class Job(_Response):
    """An analysis job (§ 3.2)."""

    job_id: str
    video_id: str
    video_path: str | None
    state: State
    progress: Progress | None
    warnings: tuple[AnalysisWarning, ...]
    error: ApiError | None
    created_at: UtcDatetime
    started_at: UtcDatetime | None
    updated_at: UtcDatetime
    finished_at: UtcDatetime | None


class PreviewMedia(_Response):
    """Whether and how a candidate can be previewed (§ 5)."""

    status: Literal["available", "preparing", "unavailable"]
    kind: Literal["video_file"] | None
    url: str | None
    media_start_s: FiniteFloat | None
    media_end_s: FiniteFloat | None
    reason: str | None
    message: str | None


class ExportAvailability(_Response):
    """Whether a candidate can be exported now, and within which bounds (§ 5)."""

    status: Literal["available", "unavailable"]
    reason: str | None
    message: str | None
    min_start_s: FiniteFloat | None
    max_end_s: FiniteFloat | None
    max_duration_s: FiniteFloat | None
    layouts: tuple[str, ...]


class CandidateMedia(_Response):
    """``GET /jobs/{job_id}/candidates/{candidate_id}/media`` (§ 5)."""

    job_id: str
    candidate_id: str
    video_id: str
    preview: PreviewMedia
    export: ExportAvailability


class CreateExportRequest(_Request):
    """``POST /api/v1/exports`` (§ 6.1)."""

    job_id: str
    candidate_id: str
    start_s: float
    end_s: float
    layout: Layout
    captions: bool

    @field_validator("start_s", "end_s")
    @classmethod
    def _finite(cls, value: float) -> float:
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError("must be a finite number")
        return value


class Export(_Response):
    """A clip export (§ 6.2)."""

    export_id: str
    job_id: str
    candidate_id: str
    video_id: str
    start_s: float
    end_s: float
    layout: str
    captions: bool
    state: State
    progress: Progress | None
    error: ApiError | None
    download_url: str | None
    created_at: UtcDatetime
    started_at: UtcDatetime | None
    updated_at: UtcDatetime
    finished_at: UtcDatetime | None


class HighlightsSegment(BaseModel):
    """One moment of a highlights video (§ 7.1). Strict in requests, echoed in responses."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    candidate_id: str
    start_s: float
    end_s: float

    @field_validator("start_s", "end_s")
    @classmethod
    def _finite(cls, value: float) -> float:
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError("must be a finite number")
        return value


class CreateHighlightsRequest(_Request):
    """``POST /api/v1/highlights`` (§ 7.1).

    `segments` is a list, not a tuple: strict mode validates the parsed JSON body, where a
    tuple field would reject the JSON array.
    """

    job_id: str
    segments: Annotated[list[HighlightsSegment], Field(min_length=1, max_length=20)] | None = None
    count: Annotated[int, Field(ge=1, le=20)] = 10
    order: Order = "chronological"
    captions: bool = False


class HighlightsVideo(_Response):
    """A highlights video (§ 7.2)."""

    highlights_id: str
    job_id: str
    video_id: str
    segments: tuple[HighlightsSegment, ...]
    order: Order
    captions: bool
    state: State
    progress: Progress | None
    error: ApiError | None
    download_url: str | None
    duration_s: float | None
    created_at: UtcDatetime
    started_at: UtcDatetime | None
    updated_at: UtcDatetime
    finished_at: UtcDatetime | None


class Health(_Response):
    """``GET /api/v1/health`` (§ 9.5)."""

    status: Literal["ok"] = "ok"
    schema_version: str = SCHEMA_VERSION
    job_runner: Literal["mock", "pipeline"]
