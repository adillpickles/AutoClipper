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
    "AppSettings",
    "ArchiveKind",
    "ArchiveOmission",
    "ArchiveStatus",
    "Candidate",
    "CandidateMedia",
    "ChannelInfo",
    "ChannelVod",
    "ChannelVodsResponse",
    "CreateExportRequest",
    "CreateHighlightsRequest",
    "CreateJobRequest",
    "DeleteJobResponse",
    "ErrorResponse",
    "Export",
    "ExportAvailability",
    "Health",
    "HighlightsSegment",
    "HighlightsVideo",
    "Job",
    "JobListResponse",
    "JobRenderSummary",
    "JobSummary",
    "OutputCopy",
    "PreflightCheck",
    "PreflightReport",
    "PreflightStatus",
    "PreviewMedia",
    "Progress",
    "ScorerInfo",
    "ToolSetupItem",
    "ToolSetupName",
    "ToolSetupRequest",
    "ToolSetupState",
    "ToolSetupStatus",
    "UpdateSettingsRequest",
    "VideoUpload",
    "VodSummary",
    "parse_channel",
    "parse_vod",
]

SCHEMA_VERSION: Final = "1.1"

type State = Literal["queued", "running", "done", "failed"]
type ArchiveKind = Literal["clips", "highlights"]
type Layout = Literal["center_crop", "configured_region", "stacked"]
type Order = Literal["chronological", "rank"]
type PreflightStatus = Literal["ok", "warn", "fail"]
type ToolSetupState = Literal["idle", "running", "done", "failed"]
type ToolSetupName = Literal["ffmpeg", "twitch_downloader"]

# api-contract.md § 3.1, case-insensitive; the ID is in group 1 or 2.
VOD_PATTERN: Final = re.compile(
    r"^(?:(?:https?://)?(?:www\.|m\.)?twitch\.tv/videos/([0-9]+)/?(?:[?#].*)?|([0-9]+))$",
    re.IGNORECASE,
)

# api-contract.md § 9.7: channel URL or bare login (optional leading @).
_CHANNEL_LOGIN_PATTERN: Final = re.compile(r"^@?([A-Za-z0-9][A-Za-z0-9_]{0,24})$")
_CHANNEL_URL_PATTERN: Final = re.compile(
    r"^(?:https?://)?(?:www\.|m\.)?twitch\.tv/([A-Za-z0-9][A-Za-z0-9_]{0,24})"
    r"(?:/(?:videos|home|about|schedule|clips))?/?(?:[?#].*)?$",
    re.IGNORECASE,
)
_TWITCH_RESERVED_PATHS: Final = frozenset(
    {
        "clips",
        "directory",
        "downloads",
        "drops",
        "inventory",
        "jobs",
        "login",
        "moderator",
        "p",
        "popout",
        "prime",
        "search",
        "settings",
        "signup",
        "subscriptions",
        "turbo",
        "u",
        "user",
        "videos",
        "wallet",
    }
)


def parse_vod(text: str) -> str | None:
    """The video ID in a VOD link or bare ID (§ 3.1), or None if it isn't one."""
    match = VOD_PATTERN.match(text.strip())
    if match is None:
        return None
    video_id = match.group(1) or match.group(2)
    # Only ASCII digits: `[0-9]` with IGNORECASE matches nothing else, but be explicit.
    return video_id if video_id.isascii() else None


def parse_channel(text: str) -> str | None:
    """The normalized lowercase channel login from a Twitch channel URL or bare login (§ 9.7).

    Rejects VOD links, clip links, non-Twitch URLs, bare numeric IDs, and reserved Twitch routes.
    """
    trimmed = text.strip()
    if not trimmed:
        return None
    url_match = _CHANNEL_URL_PATTERN.match(trimmed)
    if url_match is not None:
        login = url_match.group(1)
        if not login.isascii():
            return None
        lowered = login.lower()
        if lowered in _TWITCH_RESERVED_PATHS or lowered.isdigit():
            return None
        return lowered
    login_match = _CHANNEL_LOGIN_PATTERN.match(trimmed)
    if login_match is not None:
        login = login_match.group(1)
        if not login.isascii() or login.isdigit():
            return None
        return login.lower()
    return None


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


class VideoUpload(_Response):
    """Result of ``POST /api/v1/uploads`` (§ 3.1a)."""

    upload_id: str
    filename: str
    size_bytes: int = Field(ge=1)
    duration_s: FiniteFloat | None = None


class CreateJobRequest(_Request):
    """``POST /api/v1/jobs`` (§ 3.1)."""

    vod: str
    video_path: str | None = None
    upload_id: str | None = None


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


class JobRenderSummary(_Response):
    """Summary of one export or highlights render belonging to a job (§ 3.7)."""

    render_id: str
    kind: Literal["export", "highlights"]
    candidate_id: str | None
    state: State
    file_available: bool
    download_url: str | None
    duration_s: FiniteFloat | None
    created_at: UtcDatetime
    finished_at: UtcDatetime | None


class ArchiveOmission(_Response):
    """A requested render left out of a job's ZIP download (§ 7.6)."""

    render_id: str
    candidate_id: str | None
    rank: int | None
    reason: Literal["queued", "running", "failed", "file_missing"]


class ArchiveStatus(_Response):
    """What a job's ZIP download would contain right now (``GET /jobs/{id}/archives/{kind}``)."""

    kind: ArchiveKind
    ready_count: int
    omitted: tuple[ArchiveOmission, ...]
    filename: str
    download_url: str | None


class JobSummary(_Response):
    """One analysis job in the persistent library index (``GET /api/v1/jobs``, § 3.7)."""

    job_id: str
    video_id: str
    video_path: str | None
    state: State
    progress: Progress | None = None
    title: str | None
    broadcaster_id: str | None
    broadcaster_display_name: str | None
    duration_s: FiniteFloat | None
    thumbnail_url: str | None
    candidate_count: int | None
    result_available: bool
    export_count: int = Field(ge=0)
    missing_export_count: int = Field(ge=0)
    highlights_count: int = Field(ge=0)
    missing_highlights_count: int = Field(ge=0)
    exports: tuple[JobRenderSummary, ...]
    highlights: tuple[JobRenderSummary, ...]
    warning_count: int = Field(ge=0)
    error: ApiError | None
    created_at: UtcDatetime
    started_at: UtcDatetime | None
    updated_at: UtcDatetime
    finished_at: UtcDatetime | None


class JobListResponse(_Response):
    """Paginated job listing returned by ``GET /api/v1/jobs`` (§ 3.7)."""

    items: tuple[JobSummary, ...]
    total: int = Field(ge=0)
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)
    has_more: bool
    next_cursor: str | None


class JobStatusRequest(_Request):
    """Jobs to look up in a single library polling request (§ 3.7)."""

    job_ids: list[Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")]]


class DeleteJobResponse(_Response):
    """Result of ``DELETE /api/v1/jobs/{job_id}`` (§ 3.8)."""

    job_id: str
    deleted: Literal[True] = True
    deleted_files: int = Field(ge=0)


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
    filename: str | None = None
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
    filename: str | None = None
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
    captions_available: bool = False


class PreflightCheck(_Response):
    """One local-environment readiness check (§ 9.6)."""

    name: str
    label: str
    status: PreflightStatus
    message: str
    fix: str | None
    source: str | None = None


class PreflightReport(_Response):
    """``GET /api/v1/preflight`` (§ 9.6)."""

    status: PreflightStatus
    checks: tuple[PreflightCheck, ...]


class ChannelInfo(_Response):
    """A resolved Twitch channel (§ 9.7)."""

    broadcaster_id: str
    login: str
    display_name: str
    profile_image_url: str | None


class ChannelVod(_Response):
    """One recent archive VOD of a channel (§ 9.7)."""

    video_id: str
    title: str
    created_at: UtcDatetime
    duration_s: FiniteFloat
    thumbnail_url: str | None
    url: str
    view_count: int = Field(ge=0)
    analyzed: bool
    last_job_id: str | None


class ChannelVodsResponse(_Response):
    """``GET /api/v1/channels/vods`` (§ 9.7)."""

    channel: ChannelInfo
    vods: tuple[ChannelVod, ...]
    truncated: bool


class ToolSetupRequest(_Request):
    """``POST /api/v1/tools/setup`` (§ 9.8)."""

    tools: Annotated[list[ToolSetupName], Field(min_length=1, max_length=2)] | None = None
    force: bool = False


class OutputCopy(_Response):
    """The latest copy of a finished render into the output folder (§ 9.9)."""

    render_id: str
    kind: Literal["export", "highlights"]
    state: Literal["copied", "failed"]
    filename: str | None
    message: str | None
    finished_at: UtcDatetime


class AppSettings(_Response):
    """``GET``/``PUT /api/v1/settings`` (§ 9.9)."""

    output_folder: str | None
    last_copy: OutputCopy | None


class UpdateSettingsRequest(_Request):
    """``PUT /api/v1/settings``: ``output_folder`` null or blank turns copying off."""

    output_folder: Annotated[str, Field(max_length=4096)] | None


class ToolSetupItem(_Response):
    """Status and progress of one managed tool group during setup (§ 9.8)."""

    tool: ToolSetupName
    label: str
    stage: str
    message: str
    bytes_downloaded: int | None = None
    bytes_total: int | None = None
    fraction: Annotated[float, Field(ge=0, le=1)] | None = None
    error: ApiError | None = None


class ToolSetupStatus(_Response):
    """``GET /api/v1/tools/setup`` and ``POST /api/v1/tools/setup`` (§ 9.8)."""

    state: ToolSetupState
    items: tuple[ToolSetupItem, ...]
    error: ApiError | None
    started_at: UtcDatetime | None
    updated_at: UtcDatetime
    finished_at: UtcDatetime | None
