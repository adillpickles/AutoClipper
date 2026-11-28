"""Backend state, persistence and background work (docs/architecture/api-contract.md §§ 3, 5-7, 9).

`MockService` persists job, export and highlights records in a SQLite store under `output_dir`
(`autoclipper.api.store`, § 9.3), validates requests in the contract's order and raises
`ApiException` with the contract's codes. Two worker threads run the work: one for analysis jobs,
one shared by exports and highlights videos (one render at a time), each first in, first out.

Jobs run one of two runners (`job_runner`, reported by the health endpoint): ``mock`` walks
through the six analysis stages and finishes with the § 11 scenario keyed by the VOD ID, for
frontend work without credentials; ``pipeline`` (#45) calls the real analysis
(`autoclipper.analyze.analyze_vod`) with the job's optional ``video_path``, reporting real
progress as each stage begins and the warnings found so far, and fails the job with
`AnalyzeError`'s § 3.5 code when the analysis can't produce a result.

Exports are real: the render worker cuts the requested interval from the job's ``video_path``,
or from the sample preview video when the job has none, and renders it with
`autoclipper.export.clip.render_clip` (exact interval, layout, optional captions), one file per
export. Highlights videos are real too (#72): the worker joins the job's resolved segments with
`autoclipper.export.highlights.render_highlights` (exact cuts, the source's own framing at
1920x1080, fades, optional captions) from the same source, one file per highlights video.
"""

import contextlib
import json
import logging
import queue
import re
import secrets
import sqlite3
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Literal, Protocol, TypeVar

from autoclipper.analysis import AnalysisResult, AnalysisWarning, Candidate
from autoclipper.analyze import DEFAULT_SCORER, AnalyzeError, analyze_vod
from autoclipper.api import mock
from autoclipper.api.store import (
    DEFAULT_DB_NAME,
    DEFAULT_MOCK_DB_NAME,
    JobStore,
    StoredJobRecord,
    StoredRenderRecord,
    read_result_json,
    write_result_json,
)
from autoclipper.api.uploads import UPLOAD_ID_PATTERN, validate_video_file
from autoclipper.export.archive import ArchiveMember, ArchivePlan
from autoclipper.export.captions import CaptioningError, Stage, Transcriber, captions_available
from autoclipper.export.clip import (
    MAX_CLIP_DURATION_S,
    ClipSpec,
    IntervalOutsideMediaError,
    SourceMissingError,
    available_layouts,
    render_clip,
)
from autoclipper.export.cut import (
    DURATION_EPSILON_S,
    MIN_CLIP_DURATION_S,
    Interval,
    IntervalTooShortError,
)
from autoclipper.export.encode import EncodeSettings, encode_timeout_s
from autoclipper.export.errors import ExportError
from autoclipper.export.facecam import DETECTOR_VERSION
from autoclipper.export.filenames import (
    build_archive_filename,
    build_clip_filename,
    build_highlights_filename,
    collision_key,
    deduplicate_filename,
)
from autoclipper.export.highlights import (
    HighlightsRequest,
    HighlightsResult,
    Segment,
    render_highlights,
)
from autoclipper.export.highlights import (
    Stage as HighlightsStage,
)
from autoclipper.export.output_folder import (
    CopyCancelled,
    OutputFolderError,
    copy_into_folder,
    remove_stale_partials,
    validate_output_folder,
)
from autoclipper.export.reframe import StackedLayout, stacked_layout_from_region
from autoclipper.export.seek import (
    covered_span,
    probe_media_and_times,
    probe_stream_ends,
    probe_stream_times,
)
from autoclipper.features.chat import Lexicon
from autoclipper.ffmpeg import (
    TIMELINE_AUDIO_FILTER,
    TIMELINE_OPTIONS,
    FFmpegError,
    FFmpegNotFoundError,
    ffmpeg_command,
    probe,
    run_command,
)
from autoclipper.ingest.chat import (
    PINNED_TOOL_VERSION as TWITCHDOWNLOADER_VERSION,
)
from autoclipper.ingest.chat import (
    TOOL_NAME as TWITCHDOWNLOADER_TOOL_NAME,
)
from autoclipper.ingest.media import (
    DownloadIntegrityError,
    MissingDownloadToolError,
    VideoSegment,
    VodAccessRestrictedError,
    VodUnavailableError,
)
from autoclipper.ingest.vod import IngestTools
from autoclipper.schemas import (
    ApiError,
    AppSettings,
    ArchiveKind,
    ArchiveOmission,
    CandidateMedia,
    CreateExportRequest,
    CreateHighlightsRequest,
    CreateJobRequest,
    DeleteJobResponse,
    Export,
    ExportAvailability,
    HighlightsSegment,
    HighlightsVideo,
    Job,
    JobListResponse,
    JobRenderSummary,
    JobSummary,
    OutputCopy,
    PreviewMedia,
    Progress,
    State,
    UpdateSettingsRequest,
    VideoUpload,
    parse_vod,
)

log = logging.getLogger(__name__)

_W = TypeVar("_W")
"""A work item's type on one of the service's queues (a job or render ID, or a fetch key)."""

MAX_EXPORT_DURATION_S: Final = MAX_CLIP_DURATION_S  # api-contract.md § 5, labeling.md § 2
MAX_HIGHLIGHTS_DURATION_S: Final = 900.0  # § 7.1
# How much more than a candidate (or trim) is fetched for preview and cuts (#46): room to move
# a trim without another download, kept well under the 90 s export bound.
SEGMENT_MARGIN_S: Final = 30.0
# Same one-output-frame slack that `render_clip` and `render_highlights` allow at segment ends.
_RENDER_END_TOLERANCE_S: Final = 1.0 / EncodeSettings().frame_rate
# How ``missing.txt`` in a ZIP download describes each omission reason.
_OMISSION_TEXT: Final[dict[str, str]] = {
    "queued": "waiting to render",
    "running": "still rendering",
    "failed": "render failed",
    "file_missing": "file no longer on disk",
}
_OUTPUT_FOLDER_KEY: Final = "output_folder"
_COPY_FOLDER_GONE: Final = (
    "The output folder is missing. Reconnect its drive or choose another folder in Settings."
)
_COPY_FAILED: Final = (
    "Couldn't copy this file into the output folder. Check that the drive has free space."
)
_COPY_SOURCE_GONE: Final = (
    "This file is no longer on disk, so it couldn't be copied. Export it again to save a copy."
)
_PREPARING_MESSAGE: Final = "Fetching the video for this moment…"
_PREPARING_LOCAL_MESSAGE: Final = "Preparing the video for playback…"
_FETCH_FAILED_PREVIEW: Final = (
    "Preview isn't available: downloading the video failed. Trying again later may work."
)
_FETCH_FAILED_EXPORT: Final = (
    "Export isn't available: downloading the video failed. Trying again later may work."
)
_MISSING_TWITCHDOWNLOADER_PREVIEW: Final = (
    f"Preview isn't available: {TWITCHDOWNLOADER_TOOL_NAME} isn't installed on PATH. "
    f"Install release {TWITCHDOWNLOADER_VERSION} (see docs/development/setup.md), or analyze "
    "the VOD with a local video file."
)
_MISSING_TWITCHDOWNLOADER_EXPORT: Final = (
    f"Export isn't available: {TWITCHDOWNLOADER_TOOL_NAME} isn't installed on PATH. "
    f"Install release {TWITCHDOWNLOADER_VERSION} (see docs/development/setup.md), or analyze "
    "the VOD with a local video file."
)
_MISSING_FFMPEG_PREVIEW: Final = (
    "Preview isn't available: FFmpeg or FFprobe isn't installed on PATH. "
    "Install FFmpeg (see docs/development/setup.md), then try again."
)
_MISSING_FFMPEG_EXPORT: Final = (
    "Export isn't available: FFmpeg or FFprobe isn't installed on PATH. "
    "Install FFmpeg (see docs/development/setup.md), then try again."
)
_SEGMENT_PREVIEW_FAILED: Final = (
    "Preview isn't available: this moment's video couldn't be prepared for playback. "
    "Exporting it still works."
)
_SEGMENT_PREVIEW_UNCOVERED: Final = (
    "Preview isn't available: the downloaded video doesn't cover this whole moment. "
    "Exports still cut from the downloaded file."
)
_LOCAL_PREVIEW_FAILED: Final = (
    "Preview isn't available: this file couldn't be prepared for playback. "
    "Exports still cut it directly."
)
_LOCAL_PREVIEW_UNCOVERED: Final = (
    "Preview isn't available: the playable copy of this file doesn't cover this moment. "
    "Exports still cut it directly."
)
OUTPUT_PLAYABLE_VIDEO: Final = "h264"
OUTPUT_PLAYABLE_AUDIO: Final = "aac"
"""What a browser's ``<video>`` element reliably plays (§ 5.1: the preview must be playable)."""
OUTPUT_PLAYABLE_VIDEO_LIB: Final = "libx264"
OUTPUT_PLAYABLE_PIX_FMTS: Final = frozenset({"yuv420p", "yuvj420p"})
"""8-bit 4:2:0 H.264: browsers decode it everywhere; 4:4:4 and 10-bit may not (#46)."""
RENDER_STAGES: Final = ("obtaining_media", "transcribing", "rendering")
_RENDER_MESSAGES: Final = {
    "obtaining_media": "Getting the video",
    "transcribing": "Generating captions",
}
_STAGE_INDEX: Final = {name: index for index, (name, _) in enumerate(mock.JOB_STAGES, start=1)}
"""A job stage name's 1-based position (§ 3.4); the pipeline reports the same stages."""

type Clock = Callable[[], datetime]


class ClipRenderer(Protocol):
    """Renders one export; `render_clip` is the real one, tests pass a fake."""

    def __call__(
        self,
        spec: ClipSpec,
        /,
        *,
        region: tuple[float, float, float, float] | None,
        transcriber: Transcriber | None,
        on_stage: Callable[[Stage], None],
    ) -> object:
        """Write `spec.output` or raise."""
        ...


class HighlightsRenderer(Protocol):
    """Renders one highlights video; `render_highlights` is the real one, tests pass a fake."""

    def __call__(
        self,
        request: HighlightsRequest,
        /,
        *,
        transcriber: Transcriber | None,
        on_stage: Callable[[HighlightsStage], None],
    ) -> HighlightsResult:
        """Write `request.output` and return the result, or raise."""
        ...


class SegmentFetcher(Protocol):
    """Fetches one video segment of a VOD (#46); `download_video_segment` is the real one.

    A fetcher raises the download errors of `autoclipper.ingest.media` (`VodUnavailableError`,
    `VodAccessRestrictedError`, `DownloadNetworkError`, ...); tests pass a fake.
    """

    def __call__(self, video_id: str, begin_s: float, end_s: float, /) -> VideoSegment:
        """Return the complete segment, covering ``[begin_s, end_s)`` of VOD time."""
        ...


class AnalyzeRunner(Protocol):
    """Analyzes one VOD; `analyze_vod` is the real one, tests pass a fake."""

    def __call__(
        self,
        video_id: str,
        *,
        data_dir: Path,
        artifacts_dir: Path,
        tools: IngestTools,
        lexicon: Lexicon,
        video_file: Path | None,
        baseline: str,
        clock: Clock,
        on_stage: Callable[[str], None],
        on_warnings: Callable[[Sequence[AnalysisWarning]], None],
    ) -> AnalysisResult:
        """Return the analysis or raise."""
        ...


class ToolsFactory(Protocol):
    """Builds the ingest tools for one pipeline job; the CLI builds them from settings."""

    def __call__(self) -> IngestTools:
        """Return the tools; the caller closes the Helix client when the job ends."""
        ...


class ApiException(Exception):
    """A request failure with its HTTP status and contract error (§ 8)."""

    def __init__(
        self, status: int, code: str, message: str, details: dict[str, object] | None = None
    ) -> None:
        super().__init__(message)
        self.status = status
        self.error = ApiError(code=code, message=message, details=details)


def new_id(prefix: str) -> str:
    """A random, never reused ID: ``prefix`` + 64 random bits as hex (§ 1)."""
    return f"{prefix}_{secrets.token_hex(8)}"


# --- Sample media ------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SampleMedia:
    """The mock's pre-generated files (§ 5, § 11)."""

    preview: Path


PREVIEW_DURATION_S: Final = 360.0  # the longest mock VOD
_SAMPLE_TIMEOUT_S: Final = 600.0


def ensure_sample_media(
    directory: Path, *, preview_duration_s: float = PREVIEW_DURATION_S
) -> SampleMedia:
    """Generate the sample MP4 into `directory` unless it is already there and readable.

    The preview is a 640x360 test pattern with a tone, `preview_duration_s` long; exports and
    highlights videos of a job without a ``video_path`` are cut from it. It goes through the
    shared FFmpeg runner.

    Raises:
        FFmpegError: FFmpeg is missing or failed.
    """
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "preview.mp4"
    if path.is_file():
        try:
            info = probe(path)
            if info.duration_s >= preview_duration_s - 0.5 and info.video_streams:
                return SampleMedia(path)
        except FFmpegError:
            pass  # damaged: generate it again
    partial = path.with_name(f"{path.stem}.part.mp4")
    command = ffmpeg_command(
        "-y",
        *(
            "-f",
            "lavfi",
            "-i",
            f"testsrc2=size=640x360:rate=10:duration={preview_duration_s}",
        ),
        *(
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:sample_rate=48000:duration={preview_duration_s}",
        ),
        *("-c:v", "libx264", "-preset", "ultrafast", "-crf", "32", "-pix_fmt", "yuv420p"),
        *("-c:a", "aac", "-movflags", "+faststart", "-shortest", partial),
    )
    try:
        run_command(command, timeout_s=_SAMPLE_TIMEOUT_S)
        partial.replace(path)
    finally:
        partial.unlink(missing_ok=True)
    log.info("generated mock sample media path=%s", path)
    return SampleMedia(path)


# --- Records -----------------------------------------------------------------------------------


@dataclass(slots=True)
class _Timestamps:
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None


@dataclass(slots=True)
class _JobRecord:
    job_id: str
    video_id: str
    video_path: str | None
    times: _Timestamps
    seq: int = 0
    runner: Literal["mock", "pipeline"] = "mock"
    upload_id: str | None = None
    state: State = "queued"
    progress: Progress | None = None
    warnings: tuple[AnalysisWarning, ...] = ()
    error: ApiError | None = None
    result: AnalysisResult | None = None
    title: str | None = None
    broadcaster_id: str | None = None
    broadcaster_display_name: str | None = None
    duration_s: float | None = None
    thumbnail_url: str | None = None
    candidate_count: int | None = None
    result_path: Path | None = None

    def model(self) -> Job:
        return Job(
            job_id=self.job_id,
            video_id=self.video_id,
            video_path=self.video_path,
            state=self.state,
            progress=self.progress,
            warnings=self.warnings,
            error=self.error,
            created_at=self.times.created_at,
            started_at=self.times.started_at,
            updated_at=self.times.updated_at,
            finished_at=self.times.finished_at,
        )

    def to_stored(self) -> StoredJobRecord:
        return StoredJobRecord(
            job_id=self.job_id,
            seq=self.seq,
            runner=self.runner,
            video_id=self.video_id,
            video_path=self.video_path,
            upload_id=self.upload_id,
            state=self.state,
            title=self.title,
            broadcaster_id=self.broadcaster_id,
            broadcaster_display_name=self.broadcaster_display_name,
            duration_s=self.duration_s,
            thumbnail_url=self.thumbnail_url,
            candidate_count=self.candidate_count,
            result_path=self.result_path,
            progress=self.progress,
            warnings=self.warnings,
            error=self.error,
            created_at=self.times.created_at,
            started_at=self.times.started_at,
            updated_at=self.times.updated_at,
            finished_at=self.times.finished_at,
        )


@dataclass(slots=True)
class _RenderRecord:
    kind: Literal["export", "highlights"]
    render_id: str
    job_id: str
    video_id: str
    captions: bool
    times: _Timestamps
    request: CreateExportRequest | None = None
    segments: tuple[HighlightsSegment, ...] = ()
    order: Literal["chronological", "rank"] = "chronological"
    state: State = "queued"
    progress: Progress | None = None
    error: ApiError | None = None
    file: Path | None = None
    duration_s: float | None = None
    filename: str | None = None
    # What the export's layout resolves from when it renders (`_layout_key`); None when unknown
    # (a render restored from the library database).
    layout_key: str | None = None

    def export_model(self, *, filename: str | None = None) -> Export:
        assert self.request is not None
        return Export(
            export_id=self.render_id,
            job_id=self.job_id,
            candidate_id=self.request.candidate_id,
            video_id=self.video_id,
            start_s=self.request.start_s,
            end_s=self.request.end_s,
            layout=self.request.layout,
            captions=self.captions,
            state=self.state,
            progress=self.progress,
            error=self.error,
            download_url=(
                f"/api/v1/exports/{self.render_id}/file" if self.state == "done" else None
            ),
            filename=filename if self.state == "done" else None,
            created_at=self.times.created_at,
            started_at=self.times.started_at,
            updated_at=self.times.updated_at,
            finished_at=self.times.finished_at,
        )

    def highlights_model(self, *, filename: str | None = None) -> HighlightsVideo:
        done = self.state == "done"
        return HighlightsVideo(
            highlights_id=self.render_id,
            job_id=self.job_id,
            video_id=self.video_id,
            segments=self.segments,
            order=self.order,
            captions=self.captions,
            state=self.state,
            progress=self.progress,
            error=self.error,
            download_url=f"/api/v1/highlights/{self.render_id}/file" if done else None,
            filename=filename if done else None,
            duration_s=self.duration_s if done else None,
            created_at=self.times.created_at,
            started_at=self.times.started_at,
            updated_at=self.times.updated_at,
            finished_at=self.times.finished_at,
        )

    def summary_model(self) -> JobRenderSummary:
        file_available = self.state == "done" and self.file is not None and self.file.is_file()
        if self.kind == "export":
            url = f"/api/v1/exports/{self.render_id}/file" if file_available else None
            candidate_id = self.request.candidate_id if self.request is not None else None
            duration_s = (
                self.request.end_s - self.request.start_s if self.request is not None else None
            )
        else:
            url = f"/api/v1/highlights/{self.render_id}/file" if file_available else None
            candidate_id = None
            duration_s = self.duration_s if self.state == "done" else None
        return JobRenderSummary(
            render_id=self.render_id,
            kind=self.kind,
            candidate_id=candidate_id,
            state=self.state,
            file_available=file_available,
            download_url=url,
            duration_s=duration_s,
            created_at=self.times.created_at,
            finished_at=self.times.finished_at,
        )

    def to_stored(self) -> StoredRenderRecord:
        return StoredRenderRecord(
            render_id=self.render_id,
            job_id=self.job_id,
            kind=self.kind,
            video_id=self.video_id,
            captions=self.captions,
            request=self.request,
            segments=self.segments,
            order=self.order,
            state=self.state,
            progress=self.progress,
            error=self.error,
            file=self.file,
            duration_s=self.duration_s,
            created_at=self.times.created_at,
            started_at=self.times.started_at,
            updated_at=self.times.updated_at,
            finished_at=self.times.finished_at,
            filename=self.filename,
        )


@dataclass(frozen=True, slots=True)
class _SegmentState:
    """One candidate's downloaded video segment (#46), or where its fetch stands.

    Frozen and replaced wholesale on every change, so a reader holding the lock always sees a
    consistent snapshot (`preparing`, `ready` or `failed`).
    """

    status: Literal["preparing", "ready", "failed"]
    planned_begin_s: float
    planned_end_s: float
    path: Path | None = None
    # The segment's probed coverage, in VOD time (the file's own time 0 is `begin_s`).
    begin_s: float | None = None
    end_s: float | None = None
    media_id: str | None = None
    preview_coverage: Interval | None = None
    # `source_unavailable` or `fetch_failed` (§ 5.2); the messages derive from it.
    reason: str | None = None
    # Custom message for `fetch_failed` (e.g. actionable missing-tool guidance); None uses default.
    message: str | None = None
    # When a segment downloaded fine (`status == "ready"`) but preview transcoding failed
    # transiently (`media_id is None`), report `fetch_failed` once and allow retrying preview
    # preparation on the next media request without losing the downloaded segment for exports.
    preview_preparing: bool = False
    preview_failed: bool = False


@dataclass(frozen=True, slots=True)
class _FileMediaState:
    """Prepared preview copy state for a job's local `video_path` (#46)."""

    status: Literal["preparing", "ready", "failed"]
    media_id: str | None = None
    coverage: Interval | None = None


type _FetchWork = (
    tuple[Literal["segment"], str, str]
    | tuple[Literal["segment_preview"], str, str]
    | tuple[Literal["file"], str]
)


class _MediaUnavailable(Exception):
    """A render's video segment couldn't be obtained (#46); the reason is the contract's § 5.2."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


def _classify_segment_error(exc: Exception) -> tuple[str, str, str]:
    """Map a segment-download exception to contract `(reason, preview_message, export_message)`."""
    if isinstance(exc, VodUnavailableError | VodAccessRestrictedError):
        return ("source_unavailable", _SOURCE_PREVIEW_UNAVAILABLE, _SOURCE_EXPORT_UNAVAILABLE)
    if isinstance(exc, MissingDownloadToolError):
        tool = (exc.tool or "").lower()
        if "ffmpeg" in tool or "ffprobe" in tool:
            return ("fetch_failed", _MISSING_FFMPEG_PREVIEW, _MISSING_FFMPEG_EXPORT)
        return ("fetch_failed", _MISSING_TWITCHDOWNLOADER_PREVIEW, _MISSING_TWITCHDOWNLOADER_EXPORT)
    if isinstance(exc, FFmpegNotFoundError):
        return ("fetch_failed", _MISSING_FFMPEG_PREVIEW, _MISSING_FFMPEG_EXPORT)
    return ("fetch_failed", _FETCH_FAILED_PREVIEW, _FETCH_FAILED_EXPORT)


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


@dataclass
class MockService:
    """The backend's state and background work.

    Args:
        samples: The sample MP4s (`ensure_sample_media`).
        output_dir: Where export and highlights files go (``exports/`` and ``highlights/``, one
            file per ID).
        step_s: Seconds each stage takes, so ``queued`` and ``running`` are visible; 0 in tests.
        probe_video: Checks a request's ``video_path``; returns True for a readable video with a
            known duration. Defaults to the shared FFmpeg probe.
        clock: The current time, timezone-aware.
        synchronous: Run each job or render inside the request that creates it, with no worker
            threads (tests). The response still shows the record as created (``queued``).
        export_region: ``AUTOCLIPPER_EXPORT_REGION``; ``configured_region`` is offered only
            when it is set.
        transcriber: For captions; None means faster-whisper with the configured model.
        render: Renders an export (default `render_clip`).
        job_runner: ``mock`` (the § 11 scenarios, for frontend work) or ``pipeline`` (the real
            analysis, #45); the health endpoint reports which is running.
        data_dir: Where the pipeline's ingest steps read and write. Required in pipeline mode.
        artifacts_dir: Where the pipeline writes analysis results. Required in pipeline mode.
        lexicon: For the pipeline's chat features. Required in pipeline mode.
        tools_for: Builds the pipeline's ingest tools per job (the CLI builds them from
            settings). Required in pipeline mode.
        analyze: The pipeline's analysis function; tests pass a fake.
        baseline: The scorer the pipeline scores with (§ 4.1 ``scorer``).
    """

    samples: SampleMedia
    output_dir: Path
    step_s: float = 0.5
    probe_video: Callable[[Path], bool] | None = None
    clock: Clock = _utc_now
    synchronous: bool = False
    export_region: tuple[float, float, float, float] | None = None
    export_facecam_region: tuple[float, float, float, float] | None = None
    channel_layouts: Mapping[str, StackedLayout] | None = None
    transcriber: Transcriber | None = None
    captions_supported: bool | None = None
    render: ClipRenderer = render_clip
    highlights_render: HighlightsRenderer = render_highlights
    job_runner: Literal["mock", "pipeline"] = "mock"
    data_dir: Path | None = None
    artifacts_dir: Path | None = None
    lexicon: Lexicon | None = None
    tools_for: ToolsFactory | None = None
    analyze: AnalyzeRunner = field(default=analyze_vod, kw_only=True)
    baseline: str = DEFAULT_SCORER
    segment_fetcher: SegmentFetcher | None = field(default=None, kw_only=True)
    store_path: Path | None = field(default=None, kw_only=True)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)
    _store: JobStore = field(init=False)
    _next_seq: int = field(default=0, init=False)
    _deleting: set[str] = field(default_factory=set, init=False)
    _jobs: dict[str, _JobRecord] = field(default_factory=dict, init=False)
    _renders: dict[str, _RenderRecord] = field(default_factory=dict, init=False)
    _uploads: dict[str, Path] = field(default_factory=dict, init=False)
    _media: dict[str, Path] = field(default_factory=dict, init=False)
    _preview_media_id: str = field(default="", init=False)
    _file_media: dict[str, _FileMediaState] = field(default_factory=dict, init=False)
    _segments: dict[tuple[str, str], _SegmentState] = field(default_factory=dict, init=False)
    _active_segment_paths: Counter[Path] = field(default_factory=Counter, init=False)
    _pending_invalidations: set[Path] = field(default_factory=set, init=False)
    _detached_segments: list[Path] = field(default_factory=list, init=False)
    _fetching: set[tuple[str, int, int]] = field(default_factory=set, init=False)
    _fetches_done: int = field(default=0, init=False)
    _fetch_done: threading.Condition = field(
        default_factory=lambda: threading.Condition(), init=False
    )
    _analysis_queue: "queue.Queue[str | None]" = field(default_factory=queue.Queue, init=False)
    _render_queue: "queue.Queue[str | None]" = field(default_factory=queue.Queue, init=False)
    _fetch_queue: "queue.Queue[_FetchWork | None]" = field(default_factory=queue.Queue, init=False)
    _copy_queue: "queue.Queue[tuple[str, Path] | None]" = field(
        default_factory=queue.Queue, init=False
    )
    _output_folder: Path | None = field(default=None, init=False)
    # Set by close(): a copy in progress stops at its next chunk and removes its partial file.
    _copy_stop: threading.Event = field(default_factory=threading.Event, init=False)
    _last_copy: OutputCopy | None = field(default=None, init=False)
    _threads: list[threading.Thread] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        if self.job_runner == "pipeline":
            missing = [
                name
                for name, value in (
                    ("data_dir", self.data_dir),
                    ("artifacts_dir", self.artifacts_dir),
                    ("lexicon", self.lexicon),
                    ("tools_for", self.tools_for),
                    ("segment_fetcher", self.segment_fetcher),
                )
                if value is None
            ]
            if missing:
                raise ValueError(f"pipeline jobs need {', '.join(missing)}")
        self.output_dir = self.output_dir.resolve()
        default_name = DEFAULT_DB_NAME if self.job_runner == "pipeline" else DEFAULT_MOCK_DB_NAME
        db_path = (
            self.store_path.resolve()
            if self.store_path is not None
            else self.output_dir / default_name
        )
        self._store = JobStore(db_path)
        self._load_persisted()
        saved_folder = self._store.get_setting(_OUTPUT_FOLDER_KEY)
        # Not re-validated here: a folder on an unplugged drive stays set, and copies report it.
        self._output_folder = Path(saved_folder) if isinstance(saved_folder, str) else None
        self._preview_media_id = new_id("med")
        self._media[self._preview_media_id] = self.samples.preview
        if self.synchronous:
            return
        workers = (
            ("analysis", self._run_jobs),
            ("render", self._run_renders),
            ("fetch", self._run_fetches),
            # Copies get their own worker so a long copy never holds up the next render.
            ("copy", self._run_copies),
        )
        for name, target in workers:
            thread = threading.Thread(target=target, name=f"autoclipper-{name}", daemon=True)
            thread.start()
            self._threads.append(thread)

    def _load_persisted(self) -> None:
        """Restore saved jobs and renders on startup, marking in-flight work as interrupted."""
        jobs, renders = self._store.load_all()
        now = self.clock()
        max_seq = 0
        for stored in jobs:
            max_seq = max(max_seq, stored.seq)
            times = _Timestamps(
                created_at=stored.created_at,
                updated_at=stored.updated_at,
                started_at=stored.started_at,
                finished_at=stored.finished_at,
            )
            record = _JobRecord(
                job_id=stored.job_id,
                video_id=stored.video_id,
                video_path=stored.video_path,
                times=times,
                seq=stored.seq,
                runner=stored.runner,
                upload_id=stored.upload_id,
                state=stored.state,
                progress=stored.progress,
                warnings=stored.warnings,
                error=stored.error,
                title=stored.title,
                broadcaster_id=stored.broadcaster_id,
                broadcaster_display_name=stored.broadcaster_display_name,
                duration_s=stored.duration_s,
                thumbnail_url=stored.thumbnail_url,
                candidate_count=stored.candidate_count,
                result_path=stored.result_path,
            )
            if record.state in ("queued", "running"):
                record.state = "failed"
                record.error = _JOB_INTERRUPTED
                record.times.updated_at = record.times.finished_at = now
                self._store.upsert_job(record.to_stored())
            elif record.state == "done" and record.result_path is not None:
                loaded = read_result_json(record.result_path)
                if loaded is not None:
                    record.result = loaded
            self._jobs[record.job_id] = record
            if record.upload_id is not None:
                self._resolve_upload_path(record.upload_id)  # warm the cache before workers start

        for stored_render in renders:
            if stored_render.job_id not in self._jobs:
                continue
            times = _Timestamps(
                created_at=stored_render.created_at,
                updated_at=stored_render.updated_at,
                started_at=stored_render.started_at,
                finished_at=stored_render.finished_at,
            )
            render = _RenderRecord(
                kind=stored_render.kind,
                render_id=stored_render.render_id,
                job_id=stored_render.job_id,
                video_id=stored_render.video_id,
                captions=stored_render.captions,
                times=times,
                request=stored_render.request,
                segments=stored_render.segments,
                order=stored_render.order,
                state=stored_render.state,
                progress=stored_render.progress,
                error=stored_render.error,
                file=stored_render.file,
                duration_s=stored_render.duration_s,
                filename=stored_render.filename,
            )
            if render.state in ("queued", "running"):
                render.state = "failed"
                render.error = (
                    _EXPORT_INTERRUPTED if render.kind == "export" else _HIGHLIGHTS_INTERRUPTED
                )
                render.times.updated_at = render.times.finished_at = now
                self._store.upsert_render(render.to_stored())
            self._renders[render.render_id] = render

        self._next_seq = max_seq

    def _persist_job(self, record: _JobRecord) -> None:
        """Persist `record` to the SQLite index (call with `self._lock` held)."""
        self._store.upsert_job(record.to_stored())

    def _persist_render(self, record: _RenderRecord) -> None:
        """Persist `record` to the SQLite index (call with `self._lock` held)."""
        self._store.upsert_render(record.to_stored())

    @property
    def captions_available(self) -> bool:
        """Whether this server can transcribe speech for burned-in captions (§ 9.5)."""
        if self.captions_supported is not None:
            return self.captions_supported
        return self.transcriber is not None or captions_available()

    def close(self) -> None:
        """Stop the workers: queued work is dropped, the work in hand finishes."""
        self._copy_stop.set()
        if not self.synchronous:
            queues = (self._analysis_queue, self._render_queue, self._fetch_queue, self._copy_queue)
            for work in queues:
                while True:  # queued work is dropped; on next startup it becomes interrupted
                    try:
                        work.get_nowait()
                    except queue.Empty:
                        break
                work.put(None)
            for thread in self._threads:
                thread.join(timeout=5)
        with self._lock:
            detached = list(self._detached_segments)
            self._detached_segments.clear()
        for path in detached:
            with contextlib.suppress(OSError):
                path.unlink(missing_ok=True)
        self._store.close()

    # --- Uploads -------------------------------------------------------------------------------

    @property
    def uploads_dir(self) -> Path:
        """Directory under artifacts where uploaded VOD videos are stored."""
        return self.output_dir / "uploads"

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
        if not UPLOAD_ID_PATTERN.fullmatch(upload_id):
            raise ValueError(f"invalid upload_id: {upload_id}")
        resolved = path.resolve()
        if not _is_within(resolved, self.uploads_dir):
            raise ValueError("uploaded file must live inside uploads_dir")
        meta_path = self.uploads_dir / f"{upload_id}.json"
        partial = meta_path.with_name(f".{upload_id}.{secrets.token_hex(4)}.json.part")
        payload = {
            "upload_id": upload_id,
            "filename": filename,
            "stored_name": resolved.name,
            "size_bytes": size_bytes,
            "duration_s": duration_s,
        }
        try:
            partial.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8", newline="\n")
            partial.replace(meta_path)
        finally:
            partial.unlink(missing_ok=True)
        with self._lock:
            self._uploads[upload_id] = resolved
        return VideoUpload(
            upload_id=upload_id,
            filename=filename,
            size_bytes=size_bytes,
            duration_s=duration_s,
        )

    def _read_upload_path(self, upload_id: str) -> Path | None:
        """Read an upload's sidecar and return its stored file, or None (no lock held)."""
        if not UPLOAD_ID_PATTERN.fullmatch(upload_id):
            return None
        meta_path = self.uploads_dir / f"{upload_id}.json"
        try:
            data: object = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        stored_name = data.get("stored_name")
        if (
            not isinstance(stored_name, str)
            or not stored_name
            or Path(stored_name).name != stored_name
        ):
            return None
        candidate = (self.uploads_dir / stored_name).resolve()
        # Cached even when the file is gone: every caller checks `is_file()`, so a file put
        # back while the server runs is found again without a restart.
        return candidate if _is_within(candidate, self.uploads_dir) else None

    def _resolve_upload_path(self, upload_id: str) -> Path | None:
        """Resolve an opaque `upload_id` to its stored file path, or None if missing/invalid.

        The sidecar is read outside `self._lock`; only the cache lookup and update hold it.
        """
        with self._lock:
            cached = self._uploads.get(upload_id)
        if cached is None:
            cached = self._read_upload_path(upload_id)
            if cached is None:
                return None
            with self._lock:
                cached = self._uploads.setdefault(upload_id, cached)
        return cached if cached.is_file() else None

    def _has_local_source(self, job: _JobRecord) -> bool:
        return job.video_path is not None or job.upload_id is not None

    def _source_video_path_locked(self, job: _JobRecord) -> Path | None:
        if job.video_path is not None:
            return Path(job.video_path)
        if job.upload_id is not None:
            # Cache only: uploads are cached when registered, attached (`create_job`) or loaded
            # at startup, so callers holding the lock never read sidecars. Callers check the
            # file still exists.
            return self._uploads.get(job.upload_id)
        return None

    def _source_video_path(self, job: _JobRecord) -> Path | None:
        if job.video_path is not None:
            return Path(job.video_path)
        if job.upload_id is not None:
            return self._resolve_upload_path(job.upload_id)
        return None

    # --- Settings ------------------------------------------------------------------------------

    def get_settings(self) -> AppSettings:
        """The app settings (§ 9.9)."""
        with self._lock:
            folder = self._output_folder
            return AppSettings(
                output_folder=str(folder) if folder is not None else None,
                last_copy=self._last_copy,
            )

    def update_settings(self, request: UpdateSettingsRequest) -> AppSettings:
        """Set or clear the output folder (`422 invalid_output_folder` if it can't be used)."""
        raw = (request.output_folder or "").strip()
        folder: Path | None = None
        if raw:
            try:
                folder = validate_output_folder(raw)
            except OutputFolderError as exc:
                # The message names no path (§ 1); the log keeps it for the user's own diagnosis.
                log.info("rejected output folder %r: %s", raw, exc.reason)
                raise ApiException(
                    422, "invalid_output_folder", exc.message, {"reason": exc.reason}
                ) from exc
        with self._lock:
            self._store.set_setting(_OUTPUT_FOLDER_KEY, str(folder) if folder else None)
            self._output_folder = folder
        log.info("output folder %s", f"set to {folder}" if folder else "cleared")
        return self.get_settings()

    # --- Jobs ----------------------------------------------------------------------------------

    def create_job(self, request: CreateJobRequest) -> tuple[Job, bool]:
        """Create a job, or return the queued/running one for the same input (`created` False)."""
        video_id = parse_vod(request.vod)
        if video_id is None:
            raise ApiException(
                422,
                "invalid_vod_link",
                "That isn't a Twitch VOD link. Paste a link like twitch.tv/videos/<id> or just the "
                "numeric ID.",
            )
        if request.video_path is not None and request.upload_id is not None:
            raise ApiException(
                422,
                "invalid_request",
                "The request is invalid: pass either video_path or upload_id, not both.",
                {"fields": ["video_path", "upload_id"]},
            )
        if request.video_path is not None and not self._valid_video(request.video_path):
            raise ApiException(
                422,
                "invalid_video_path",
                "That video file can't be opened. Check the path and that it's a complete video "
                "file.",
            )
        if request.upload_id is not None:
            upload_path = self._resolve_upload_path(request.upload_id)
            if (
                upload_path is None
                or not validate_video_file(upload_path, probe_video=self.probe_video)[0]
            ):
                raise ApiException(
                    422,
                    "invalid_video_upload",
                    "The uploaded video file is missing or can't be opened. Upload the video "
                    "again.",
                )
        with self._lock:
            key = (video_id, request.video_path, request.upload_id)
            reusable = self._reusable_job(key)
            if reusable is not None:
                return reusable.model(), False
            now = self.clock()
            next_seq = self._next_seq + 1
            record = _JobRecord(
                new_id("job"),
                video_id,
                request.video_path,
                _Timestamps(now, now),
                seq=next_seq,
                runner=self.job_runner,
                upload_id=request.upload_id,
            )
            self._persist_job(record)
            self._next_seq = next_seq
            self._jobs[record.job_id] = record
            model = record.model()
        self._enqueue(self._analysis_queue, record.job_id, self._run_job_safely)
        return model, True

    def _reusable_job(self, key: tuple[str, str | None, str | None]) -> _JobRecord | None:
        """The job a new request for `key` should reuse, with the service lock held (#186).

        A `done` job whose result is still on disk wins (newest first), then a `queued` or
        `running` one. Failed jobs and done jobs whose result file is gone are never reused, so
        posting again retries them.
        """
        done: list[_JobRecord] = []
        active: list[_JobRecord] = []
        for record in self._jobs.values():
            if _job_key(record) != key or record.job_id in self._deleting:
                continue
            if record.state == "done" and self._result_available(record):
                done.append(record)
            elif record.state in ("queued", "running"):
                active.append(record)
        if done or active:
            return max(done or active, key=_job_sort_key)
        return None

    def _library_rows(self, jobs: Iterable[_JobRecord]) -> list[_JobRecord]:
        """One job per VOD and video source for the Library, with the service lock held (#186).

        Older libraries can hold several analyses of the same VOD from before job creation reused
        finished jobs. They are collapsed here rather than deleted, so no result or render is lost:
        the row is the newest `done` job with its result on disk, else the newest job. Deleting
        that row reveals the next one.
        """
        groups: dict[tuple[str, str | None, str | None], list[_JobRecord]] = {}
        for record in jobs:
            groups.setdefault(_job_key(record), []).append(record)
        rows: list[_JobRecord] = []
        for members in groups.values():
            if len(members) == 1:
                rows.append(members[0])
                continue
            done = [rec for rec in members if rec.state == "done" and self._result_available(rec)]
            rows.append(max(done or members, key=_job_sort_key))
        return rows

    def get_job(self, job_id: str) -> Job:
        """The job (`404 job_not_found` if unknown)."""
        with self._lock:
            return self._job(job_id).model()

    def get_result(self, job_id: str) -> AnalysisResult:
        """Result of a done job (`409 job_not_done` before, `404 result_file_missing` if gone)."""
        with self._lock:
            record = self._done_job(job_id)
            return self._job_result(record)

    def list_jobs(
        self,
        *,
        limit: int = 20,
        offset: int = 0,
        before: str | None = None,
        q: str | None = None,
        state: State | None = None,
    ) -> JobListResponse:
        """Paginated list of jobs, newest first, with optional search and state filter."""
        boundary: tuple[datetime, int, str] | None = None
        if before is not None:
            try:
                timestamp, seq, job_id = before.split("|")
                created_at = datetime.fromisoformat(timestamp)
                if (
                    offset != 0
                    or created_at.utcoffset() != UTC.utcoffset(None)
                    or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", job_id)
                ):
                    raise ValueError
                boundary = (created_at, int(seq), job_id)
            except ValueError:
                raise ApiException(
                    422,
                    "invalid_request",
                    "Use a valid job cursor without a nonzero offset.",
                    {"fields": ["query.before"]},
                ) from None
        needle = q.strip().lower() if q is not None else ""

        sort_key = _job_sort_key

        with self._lock:
            matched: list[_JobRecord] = []
            for record in self._library_rows(self._jobs.values()):
                if state is not None and record.state != state:
                    continue
                if needle:
                    haystack = " ".join(
                        part
                        for part in (
                            record.title or "",
                            record.broadcaster_display_name or "",
                            record.video_id,
                        )
                        if part
                    ).lower()
                    if needle not in haystack:
                        continue
                matched.append(record)
            total = len(matched)
            if boundary is not None:
                matched = [rec for rec in matched if sort_key(rec) < boundary]
            matched.sort(key=sort_key, reverse=True)
            page = matched[offset : offset + limit]
            items = self._job_summaries(page)
            has_more = offset + len(page) < len(matched)
            next_cursor = None
            if has_more and page:
                created_at, seq, job_id = sort_key(page[-1])
                next_cursor = f"{created_at.isoformat()}|{seq}|{job_id}"
        return JobListResponse(
            items=items,
            total=total,
            limit=limit,
            offset=offset,
            has_more=has_more,
            next_cursor=next_cursor,
        )

    def job_status(self, job_ids: Sequence[str]) -> tuple[JobSummary, ...]:
        """Return current summaries of the requested jobs that still exist."""
        with self._lock:
            jobs = [self._jobs[job_id] for job_id in dict.fromkeys(job_ids) if job_id in self._jobs]
            return self._job_summaries(jobs)

    def _job_summaries(self, jobs: Sequence[_JobRecord]) -> tuple[JobSummary, ...]:
        """Summarize the selected jobs and their renders with the service lock held."""
        renders_by_job: dict[str, list[_RenderRecord]] = {}
        for render_id in self._store.render_ids_for_jobs([job.job_id for job in jobs]):
            render = self._renders.get(render_id)
            if render is not None:  # Invalid persisted rows are skipped on startup.
                renders_by_job.setdefault(render.job_id, []).append(render)
        return tuple(self._job_summary(job, renders_by_job.get(job.job_id, ())) for job in jobs)

    def delete_job(self, job_id: str, *, delete_exports: bool = False) -> DeleteJobResponse:
        """Delete a finished job from the index and optionally remove its rendered files."""
        with self._lock:
            record = self._job(job_id)
            child_renders = [r for r in self._renders.values() if r.job_id == job_id]
            if (
                job_id in self._deleting
                or record.state in ("queued", "running")
                or any(r.state in ("queued", "running") for r in child_renders)
            ):
                raise ApiException(
                    409,
                    "job_in_progress",
                    "This analysis or one of its renders is still running. Wait for it to finish "
                    "before deleting it.",
                )
            self._deleting.add(job_id)
            result_path = record.result_path
            render_files = [r.file for r in child_renders if r.file is not None]

        deleted_files = 0
        failed_paths: list[Path] = []
        try:
            if delete_exports:
                for file_path in render_files:
                    if not _is_within(file_path, self.output_dir):
                        continue
                    try:
                        file_path.unlink()
                    except FileNotFoundError:
                        continue
                    except OSError as exc:
                        log.warning("couldn't remove render file path=%s: %s", file_path, exc)
                        failed_paths.append(file_path)
                    else:
                        deleted_files += 1

            analysis_root = self.output_dir / "analysis"
            if (
                not failed_paths
                and result_path is not None
                and _is_within(result_path, analysis_root)
            ):
                try:
                    result_path.unlink()
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    log.warning("couldn't remove analysis result path=%s: %s", result_path, exc)
                    failed_paths.append(result_path)

            if failed_paths:
                raise ApiException(500, "delete_failed", _DELETE_FAILED)

            with self._lock:
                self._store.delete_job(job_id)
                self._jobs.pop(job_id, None)
                for render in child_renders:
                    self._renders.pop(render.render_id, None)
                self._file_media.pop(job_id, None)
                for seg_key in [key for key in self._segments if key[0] == job_id]:
                    self._segments.pop(seg_key, None)
        finally:
            with self._lock:
                self._deleting.discard(job_id)

        return DeleteJobResponse(job_id=job_id, deleted=True, deleted_files=deleted_files)

    def _job_summary(self, record: _JobRecord, renders: Sequence[_RenderRecord]) -> JobSummary:
        ordered = sorted(
            renders,
            key=lambda r: (r.times.created_at, r.render_id),
            reverse=True,
        )
        export_summaries: list[JobRenderSummary] = []
        highlights_summaries: list[JobRenderSummary] = []
        export_count = 0
        missing_export_count = 0
        highlights_count = 0
        missing_highlights_count = 0
        for render in ordered:
            summary = render.summary_model()
            if render.kind == "export":
                export_summaries.append(summary)
                if render.state == "done":
                    if summary.file_available:
                        export_count += 1
                    else:
                        missing_export_count += 1
            else:
                highlights_summaries.append(summary)
                if render.state == "done":
                    if summary.file_available:
                        highlights_count += 1
                    else:
                        missing_highlights_count += 1
        return JobSummary(
            job_id=record.job_id,
            video_id=record.video_id,
            video_path=record.video_path,
            state=record.state,
            progress=record.progress,
            title=record.title,
            broadcaster_id=record.broadcaster_id,
            broadcaster_display_name=record.broadcaster_display_name,
            duration_s=record.duration_s,
            thumbnail_url=record.thumbnail_url,
            candidate_count=record.candidate_count,
            result_available=self._result_available(record),
            export_count=export_count,
            missing_export_count=missing_export_count,
            highlights_count=highlights_count,
            missing_highlights_count=missing_highlights_count,
            exports=tuple(export_summaries),
            highlights=tuple(highlights_summaries),
            warning_count=len(record.warnings),
            error=record.error,
            created_at=record.times.created_at,
            started_at=record.times.started_at,
            updated_at=record.times.updated_at,
            finished_at=record.times.finished_at,
        )

    def _result_available(self, record: _JobRecord) -> bool:
        if record.state != "done":
            return False
        if record.result_path is not None:
            if not record.result_path.is_file():
                record.result = None
                return False
            if record.result is None:
                record.result = read_result_json(record.result_path)
            return record.result is not None
        return record.result is not None

    def _job_result(self, record: _JobRecord) -> AnalysisResult:
        if not self._result_available(record) or record.result is None:
            raise ApiException(404, "result_file_missing", _RESULT_FILE_MISSING)
        return record.result

    def done_jobs_by_video(self) -> dict[str, str]:
        """Map each video ID with a finished job whose result is available to its most recent ID."""
        with self._lock:
            latest: dict[str, tuple[datetime, str]] = {}
            for record in self._jobs.values():
                if not self._result_available(record):
                    continue
                finished = record.times.finished_at or record.times.updated_at
                prev = latest.get(record.video_id)
                if prev is None or finished >= prev[0]:
                    latest[record.video_id] = (finished, record.job_id)
            return {video_id: job_id for video_id, (_, job_id) in latest.items()}

    def candidate_media(self, job_id: str, candidate_id: str) -> CandidateMedia:
        """Preview and export availability of one candidate (§ 5).

        A pipeline job without a `video_path` gets its video by downloading a segment around
        the candidate (#46). A job with a `video_path` prepares its browser-playable preview
        copy on the same background worker so HTTP requests never block on FFmpeg. The first
        request starts that work and reports `preparing`; poll (§ 9.1) until it is `available`
        or `unavailable`. Transient `fetch_failed` outcomes are evicted once reported so a
        later request retries instead of caching the failure.
        """
        work: _FetchWork | None = None
        with self._lock:
            record = self._done_job(job_id)
            candidate = self._candidate(record, candidate_id)
            if self._has_local_source(record):
                source = self._source_video_path_locked(record)
                if source is not None and source.is_file() and job_id not in self._file_media:
                    self._file_media[job_id] = _FileMediaState("preparing")
                    work = ("file", job_id)
            elif self._needs_segment(record) and self.segment_fetcher is not None:
                seg_key = (job_id, candidate_id)
                existing = self._segments.get(seg_key)
                if existing is None:
                    span = self._segment_span(record, candidate.start_s, candidate.end_s)
                    self._segments[seg_key] = _SegmentState("preparing", *span)
                    work = ("segment", job_id, candidate_id)
                elif (
                    existing.status == "ready"
                    and existing.media_id is None
                    and not existing.preview_preparing
                    and not existing.preview_failed
                ):
                    self._segments[seg_key] = replace(existing, preview_preparing=True)
                    work = ("segment_preview", job_id, candidate_id)
        if work is not None:  # enqueued outside the lock, like every worker item
            self._enqueue(self._fetch_queue, work, self._run_fetch_item_safely)
        with self._lock:
            record = self._done_job(job_id)
            candidate = self._candidate(record, candidate_id)
            media = self._media_for(record, candidate)
            seg_key = (job_id, candidate_id)
            seg_state = self._segments.get(seg_key)
            if (
                seg_state is not None
                and seg_state.status == "failed"
                and seg_state.reason == "fetch_failed"
            ):
                self._segments.pop(seg_key, None)
            elif (
                seg_state is not None
                and seg_state.status == "ready"
                and seg_state.media_id is None
                and seg_state.preview_failed
            ):
                # Reported `fetch_failed` once for this segment's preview; clear the sticky
                # flag so the next media request retries preview preparation while keeping the
                # downloaded segment available for exports.
                self._segments[seg_key] = replace(seg_state, preview_failed=False)
            file_state = self._file_media.get(job_id)
            if file_state is not None and file_state.status == "failed":
                self._file_media.pop(job_id, None)
            return media

    def _prepare_file_media(self, job_id: str) -> None:
        """Prepare a browser-playable copy of the job's own video file in the background (#46)."""
        with self._lock:
            job = self._jobs.get(job_id)
            source = None if job is None else self._source_video_path_locked(job)
        if source is None or not source.is_file():
            with self._lock:
                self._file_media.pop(job_id, None)
            return  # _media_for reports local_file_missing
        self._sleep()
        media_id = new_id("med")
        target = self.output_dir / "media" / f"{media_id}.mp4"
        try:
            playable, coverage = _prepare_preview(source, target, origin_s=0.0)
        except Exception as exc:  # the preview can't be made; renders still cut the file
            log.warning("preview preparation failed job_id=%s: %s", job_id, exc)
            with self._lock:
                current = self._file_media.get(job_id)
                if current is None or current.status != "ready":
                    self._file_media[job_id] = _FileMediaState("failed")
            return
        with self._lock:
            self._media[media_id] = playable
            self._file_media[job_id] = _FileMediaState("ready", media_id, coverage)

    def _needs_segment(self, job: _JobRecord) -> bool:
        """Whether this job's media comes from a downloaded segment (#46)."""
        return job.runner == "pipeline" and not self._has_local_source(job)

    def _segment_span(
        self, job: _JobRecord, begin_needed_s: float, end_needed_s: float
    ) -> tuple[float, float]:
        """The VOD-time span to fetch around `[begin_needed_s, end_needed_s)` (#46)."""
        duration_s = self._job_result(job).vod.duration_s
        begin = max(0.0, begin_needed_s - SEGMENT_MARGIN_S)
        end = min(duration_s, end_needed_s + SEGMENT_MARGIN_S)
        if not begin < end:  # the candidate hugged the VOD's end so closely the margin ate it
            end = min(duration_s, begin + 1.0)
        return begin, end

    def media_path(self, media_id: str) -> Path:
        """The file behind a media ID (`404 media_not_found`)."""
        with self._lock:
            path = self._media.get(media_id)
        if path is None or not path.is_file():
            raise ApiException(
                404,
                "media_not_found",
                "This video is no longer available. Reload the moment to get a new link.",
            )
        return path

    # --- Exports -------------------------------------------------------------------------------

    def create_export(self, request: CreateExportRequest) -> tuple[Export, bool]:
        """Create an export, or return an identical queued/running one (`created` False)."""
        with self._lock:
            job = self._done_job(request.job_id)
            if job.job_id in self._deleting:
                raise ApiException(
                    409,
                    "job_in_progress",
                    "This analysis is being deleted. Try again once it finishes.",
                )
            candidate = self._candidate(job, request.candidate_id)
            export = self._media_for(job, candidate).export
            self._require_available(export)
            if request.layout not in export.layouts:
                raise ApiException(
                    422,
                    "invalid_request",
                    f"The request is invalid: layout {request.layout} isn't available.",
                    {"fields": ["layout"]},
                )
            if not _within(
                export,
                request.start_s,
                request.end_s,
                min_duration_s=MIN_CLIP_DURATION_S,
            ):
                raise ApiException(
                    422,
                    "invalid_export_bounds",
                    _bounds_message(
                        export,
                        request.start_s,
                        request.end_s,
                        "Clips",
                        min_duration_s=MIN_CLIP_DURATION_S,
                    ),
                    {
                        "min_start_s": export.min_start_s,
                        "max_end_s": export.max_end_s,
                        "max_duration_s": export.max_duration_s,
                    },
                )
            # Identical requests reuse the queued, running or finished render (#186), so a page
            # that asks for its top clips again doesn't render them twice. A failed render, or a
            # finished one whose file was removed, is retried with a new export, and so is one
            # whose layout region changed (#191).
            layout_key = self._layout_key(job, request.layout)
            for record in self._renders.values():
                if (
                    record.kind == "export"
                    and record.request == request
                    and _same_layout(record.layout_key, layout_key, request.layout)
                    and (
                        record.state in ("queued", "running")
                        or (
                            record.state == "done"
                            and record.file is not None
                            and record.file.is_file()
                        )
                    )
                ):
                    filename = self._render_filename(record) if record.state == "done" else None
                    return record.export_model(filename=filename), False
            now = self.clock()
            record = _RenderRecord(
                "export",
                new_id("exp"),
                job.job_id,
                job.video_id,
                request.captions,
                _Timestamps(now, now),
                request=request,
                layout_key=layout_key,
            )
            self._persist_render(record)
            self._renders[record.render_id] = record
            model = record.export_model()
        self._enqueue(self._render_queue, record.render_id, self._run_render_safely)
        return model, True

    def get_export(self, export_id: str) -> Export:
        """The export (`404 export_not_found` if unknown)."""
        with self._lock:
            record = self._render(export_id, "export")
            filename = self._render_filename(record) if record.state == "done" else None
            return record.export_model(filename=filename)

    def export_file(self, export_id: str) -> tuple[Path, str]:
        """The finished export's file and suggested download name."""
        with self._lock:
            record = self._render(export_id, "export")
            path = self._finished_file(record)
            return path, self._render_filename(record)

    # --- Highlights ----------------------------------------------------------------------------

    def create_highlights(self, request: CreateHighlightsRequest) -> tuple[HighlightsVideo, bool]:
        """Create a highlights video, or return an identical queued/running one."""
        with self._lock:
            job = self._done_job(request.job_id)
            if job.job_id in self._deleting:
                raise ApiException(
                    409,
                    "job_in_progress",
                    "This analysis is being deleted. Try again once it finishes.",
                )
            result = self._job_result(job)
            candidates = {c.candidate_id: c for c in result.candidates}
            if request.segments is None:
                top = result.candidates[: request.count]
                segments = [
                    HighlightsSegment(candidate_id=c.candidate_id, start_s=c.start_s, end_s=c.end_s)
                    for c in top
                ]
            else:
                segments = list(request.segments)
                for segment in segments:
                    self._candidate(job, segment.candidate_id)
            for segment in segments:
                self._require_available(
                    self._media_for(job, candidates[segment.candidate_id]).export
                )
            ordered = _ordered(segments, candidates, request.order)
            limits = {
                "max_duration_s": MAX_EXPORT_DURATION_S,
                "max_total_duration_s": MAX_HIGHLIGHTS_DURATION_S,
            }
            if not ordered:
                raise ApiException(
                    422,
                    "invalid_highlights_bounds",
                    "This analysis has no moments to put in a highlights video.",
                    {"segment_index": None, **limits},
                )
            for index, segment in enumerate(ordered):
                export = self._media_for(job, candidates[segment.candidate_id]).export
                if not _within(export, segment.start_s, segment.end_s):
                    raise ApiException(
                        422,
                        "invalid_highlights_bounds",
                        _bounds_message(export, segment.start_s, segment.end_s, "Each moment"),
                        {"segment_index": index, **limits},
                    )
            total = sum(s.end_s - s.start_s for s in ordered)
            if total > MAX_HIGHLIGHTS_DURATION_S:
                raise ApiException(
                    422,
                    "invalid_highlights_bounds",
                    "A highlights video can be at most 15 minutes long. Remove or shorten some "
                    "moments and try again.",
                    {"segment_index": None, **limits},
                )
            resolved = tuple(ordered)
            for record in self._renders.values():
                if (
                    record.kind == "highlights"
                    and record.job_id == job.job_id
                    and record.segments == resolved
                    and record.order == request.order
                    and record.captions == request.captions
                    and record.state in ("queued", "running")
                ):
                    return record.highlights_model(), False
            now = self.clock()
            record = _RenderRecord(
                "highlights",
                new_id("hl"),
                job.job_id,
                job.video_id,
                request.captions,
                _Timestamps(now, now),
                segments=resolved,
                order=request.order,
                duration_s=total,
            )
            self._persist_render(record)
            self._renders[record.render_id] = record
            model = record.highlights_model()
        self._enqueue(self._render_queue, record.render_id, self._run_render_safely)
        return model, True

    def get_highlights(self, highlights_id: str) -> HighlightsVideo:
        """The highlights video (`404 highlights_not_found` if unknown)."""
        with self._lock:
            record = self._render(highlights_id, "highlights")
            filename = self._render_filename(record) if record.state == "done" else None
            return record.highlights_model(filename=filename)

    def highlights_file(self, highlights_id: str) -> tuple[Path, str]:
        """The finished highlights file and suggested download name."""
        with self._lock:
            record = self._render(highlights_id, "highlights")
            return self._finished_file(record), self._render_filename(record)

    def export_archive(self, job_id: str, kind: ArchiveKind) -> ArchivePlan:
        """Snapshot the job's finished files for a ZIP download (§ 7.6).

        Clips take each candidate's newest export, the one the results screen shows; an older
        trim of the same moment is left out. Highlights take every reel requested for the job.
        Renders that aren't finished or whose file is gone are listed as omitted.
        """
        with self._lock:
            job = self._job(job_id)
            channel, created_at, candidates = self._naming_context(job, job.times.created_at)
            render_kind = "export" if kind == "clips" else "highlights"
            records = [
                r for r in self._renders.values() if r.job_id == job_id and r.kind == render_kind
            ]
            if kind == "clips":
                newest: dict[str, _RenderRecord] = {}
                for record in records:  # insertion order breaks timestamp ties
                    assert record.request is not None
                    current = newest.get(record.request.candidate_id)
                    if current is None or record.times.created_at >= current.times.created_at:
                        newest[record.request.candidate_id] = record
                rank_of = {cid: c.rank for cid, c in candidates.items()}
                records = sorted(
                    newest.values(),
                    key=lambda r: rank_of.get(r.request.candidate_id, 1 << 30) if r.request else 0,
                )
            else:
                records.sort(key=lambda r: r.times.created_at)

            members: list[ArchiveMember] = []
            omitted: list[ArchiveOmission] = []
            notes: list[str] = []
            for record in records:
                candidate_id = record.request.candidate_id if record.request else None
                candidate = candidates.get(candidate_id) if candidate_id else None
                rank = candidate.rank if candidate is not None else None
                label = (
                    f"Clip {rank}"
                    if rank is not None
                    else (f"Clip {candidate_id}" if candidate_id else "Highlights video")
                )
                note = f"{label} ({record.render_id}): {{}}"
                if record.state == "done" and record.file is not None and record.file.is_file():
                    filename = self._render_filename(record)
                    members.append(
                        ArchiveMember(
                            record.file, filename, note.format(_OMISSION_TEXT["file_missing"])
                        )
                    )
                    continue
                reason = "file_missing" if record.state == "done" else record.state
                omitted.append(
                    ArchiveOmission(
                        render_id=record.render_id,
                        candidate_id=candidate_id,
                        rank=rank,
                        reason=reason,
                    )
                )
                notes.append(note.format(_OMISSION_TEXT[reason]))
            filename = build_archive_filename(
                channel=channel, created_at=created_at, fallback_id=job.video_id, kind=kind
            )
            return ArchivePlan(filename, tuple(members), tuple(omitted), tuple(notes))

    def _naming_context(
        self, job: _JobRecord | None, fallback_created_at: datetime
    ) -> tuple[str | None, datetime, dict[str, Candidate]]:
        """Channel, VOD date and candidates used to name a job's downloads (lock held)."""
        result = self._try_job_result(job)
        if result is not None:
            candidates = {c.candidate_id: c for c in result.candidates}
            return result.vod.broadcaster_display_name, result.vod.created_at, candidates
        if job is not None:
            return job.broadcaster_display_name, job.times.created_at, {}
        return None, fallback_created_at, {}

    def _try_job_result(self, job: _JobRecord | None) -> AnalysisResult | None:
        if job is None:
            return None
        if job.result is not None:
            return job.result
        if job.result_path is not None:
            job.result = read_result_json(job.result_path)
            return job.result
        return None

    def _base_render_filename(
        self,
        render: _RenderRecord,
        *,
        channel: str | None,
        created_at: datetime | None,
        candidates: Mapping[str, Candidate],
    ) -> str:
        if render.kind == "export":
            req = render.request
            cand = candidates.get(req.candidate_id) if req is not None else None
            return build_clip_filename(
                channel=channel,
                created_at=created_at,
                title=cand.title if cand is not None else None,
                rank=cand.rank if cand is not None else None,
                start_s=req.start_s if req is not None else 0.0,
                fallback_id=render.video_id,
            )
        return build_highlights_filename(
            channel=channel,
            created_at=created_at,
            fallback_id=render.video_id,
        )

    def _assign_render_filename(self, record: _RenderRecord) -> str:
        """Assign and return a stable, unique filename for `record` (call with `_lock` held)."""
        if record.filename is not None:
            return record.filename
        channel, created_at, candidates = self._naming_context(
            self._jobs.get(record.job_id), record.times.created_at
        )
        used: set[str] = {
            collision_key(peer.filename)
            for peer in self._renders.values()
            if peer.job_id == record.job_id
            and peer.kind == record.kind
            and peer.render_id != record.render_id
            and peer.state == "done"
            and peer.filename is not None
        }
        base = self._base_render_filename(
            record, channel=channel, created_at=created_at, candidates=candidates
        )
        assigned = deduplicate_filename(base, used)
        record.filename = assigned
        return assigned

    def _render_filename(self, record: _RenderRecord) -> str:
        """Return the assigned filename for `record`, lazily assigning legacy rows without one."""
        if record.filename is not None:
            return record.filename
        filename = self._assign_render_filename(record)
        try:
            self._persist_render(record)
        except (sqlite3.Error, OSError):
            log.exception("couldn't persist download filename id=%s", record.render_id)
        return filename

    # --- Lookups (call with the lock held) -----------------------------------------------------

    def _job(self, job_id: str) -> _JobRecord:
        record = self._jobs.get(job_id)
        if record is None:
            raise ApiException(
                404,
                "job_not_found",
                "This analysis no longer exists. The server may have restarted; analyze the VOD "
                "again.",
            )
        return record

    def _done_job(self, job_id: str) -> _JobRecord:
        record = self._job(job_id)
        if record.state != "done":
            raise ApiException(
                409, "job_not_done", "This analysis hasn't finished yet.", {"state": record.state}
            )
        return record

    def _candidate(self, job: _JobRecord, candidate_id: str) -> Candidate:
        result = self._job_result(job)
        for candidate in result.candidates:
            if candidate.candidate_id == candidate_id:
                return candidate
        raise ApiException(404, "candidate_not_found", "This moment isn't part of the analysis.")

    def _render(self, render_id: str, kind: Literal["export", "highlights"]) -> _RenderRecord:
        record = self._renders.get(render_id)
        if record is None or record.kind != kind:
            if kind == "export":
                raise ApiException(
                    404,
                    "export_not_found",
                    "This export no longer exists. The server may have restarted; export the "
                    "clip again.",
                )
            raise ApiException(
                404,
                "highlights_not_found",
                "This highlights video no longer exists. The server may have restarted; make it "
                "again.",
            )
        return record

    def _finished_file(self, record: _RenderRecord) -> Path:
        export = record.kind == "export"
        if record.state != "done":
            what = "export" if export else "highlights video"
            raise ApiException(
                409,
                "export_not_done" if export else "highlights_not_done",
                f"This {what} hasn't finished yet.",
                {"state": record.state},
            )
        if record.file is None or not record.file.is_file():
            if export:
                raise ApiException(
                    404,
                    "export_file_missing",
                    "This exported file is no longer on disk. Export the clip again.",
                )
            raise ApiException(
                404,
                "highlights_file_missing",
                "This highlights video is no longer on disk. Make it again.",
            )
        return record.file

    def _media_for(self, job: _JobRecord, candidate: Candidate) -> CandidateMedia:
        result = self._job_result(job)
        duration_s = result.vod.duration_s
        # The VOD's video can't be downloaded, but a local file works (§ 5 source_unavailable).
        source_path = self._source_video_path_locked(job)
        if self._has_local_source(job) and (source_path is None or not source_path.is_file()):
            preview = PreviewMedia(
                status="unavailable",
                kind=None,
                url=None,
                media_start_s=None,
                media_end_s=None,
                reason="local_file_missing",
                message=_LOCAL_FILE_MISSING,
            )
            export = ExportAvailability(
                status="unavailable",
                reason="local_file_missing",
                message=_LOCAL_FILE_MISSING,
                min_start_s=None,
                max_end_s=None,
                max_duration_s=None,
                layouts=(),
            )
        elif self._has_local_source(job):
            # The server reads the user's file and downloads no video (§ 5); it is the whole
            # VOD (checked against the VOD's length at ingest), so its time 0 is VOD time 0.
            # The preview serves a playable copy prepared in the background (#46, § 5.1).
            file_state = self._file_media.get(job.job_id)
            preview_end_s = (
                None
                if file_state is None
                else _covered_preview_end(file_state.coverage, candidate, duration_s)
            )
            if file_state is not None and preview_end_s is not None:
                preview = PreviewMedia(
                    status="available",
                    kind="video_file",
                    url=f"/api/v1/media/{file_state.media_id}",
                    media_start_s=0.0,
                    media_end_s=preview_end_s,
                    reason=None,
                    message=None,
                )
            elif file_state is not None and file_state.status != "preparing":
                preview = PreviewMedia(
                    status="unavailable",
                    kind=None,
                    url=None,
                    media_start_s=None,
                    media_end_s=None,
                    reason="fetch_failed",
                    message=(
                        _LOCAL_PREVIEW_FAILED
                        if file_state.status == "failed"
                        else _LOCAL_PREVIEW_UNCOVERED
                    ),
                )
            else:
                preview = PreviewMedia(
                    status="preparing",
                    kind=None,
                    url=None,
                    media_start_s=None,
                    media_end_s=None,
                    reason=None,
                    message=_PREPARING_LOCAL_MESSAGE,
                )
            export = self._whole_vod_export(job, duration_s)
        elif self._needs_segment(job):
            return self._segment_media(job, candidate)
        elif job.video_id == mock.NO_MEDIA_VIDEO_ID:
            preview = PreviewMedia(
                status="unavailable",
                kind=None,
                url=None,
                media_start_s=None,
                media_end_s=None,
                reason="source_unavailable",
                message=_SOURCE_PREVIEW_UNAVAILABLE,
            )
            export = ExportAvailability(
                status="unavailable",
                reason="source_unavailable",
                message=_SOURCE_EXPORT_UNAVAILABLE,
                min_start_s=None,
                max_end_s=None,
                max_duration_s=None,
                layouts=(),
            )
        else:
            # The mock scenarios serve their sample video (§ 11).
            preview = PreviewMedia(
                status="available",
                kind="video_file",
                url=f"/api/v1/media/{self._preview_media_id}",
                media_start_s=0.0,
                media_end_s=duration_s,
                reason=None,
                message=None,
            )
            export = self._whole_vod_export(job, duration_s)
        return CandidateMedia(
            job_id=job.job_id,
            candidate_id=candidate.candidate_id,
            video_id=job.video_id,
            preview=preview,
            export=export,
        )

    def _stacked_layout_for(self, job: _JobRecord) -> StackedLayout | None:
        """The channel's stacked_layout preset for `job`, if any (#68).

        It is the fallback when no facecam is detected in the clip (#191).
        """
        if self.channel_layouts and job.result is not None:
            vod = job.result.vod
            layouts = {k.strip().lower(): v for k, v in self.channel_layouts.items()}
            # Prefer canonical broadcaster_id or login (from processed/broadcasters.parquet
            # when present) before falling back to display name.
            keys: list[str] = [vod.broadcaster_id.strip().lower()]
            login = self._broadcaster_login(vod.broadcaster_id)
            if login is not None:
                keys.append(login)
            display_name = vod.broadcaster_display_name.strip().lower()
            if display_name:
                keys.append(display_name)
            for key in keys:
                preset = layouts.get(key)
                if preset is not None:
                    return preset
        return None

    def _broadcaster_login(self, broadcaster_id: str) -> str | None:
        """Resolve canonical Twitch login for `broadcaster_id` from processed data, if present."""
        if self.data_dir is None:
            return None
        try:
            from autoclipper.ingest.vod import read_broadcaster_login

            return read_broadcaster_login(self.data_dir, broadcaster_id)
        except Exception:
            return None

    def _layout_key(self, job: _JobRecord, layout: str) -> str:
        """What `layout` resolves from at render time, so a changed region isn't reused.

        ``stacked`` resolves from the env facecam region, the facecam detected in the clip (the
        same for the same media and detector version) and the channel preset (#191);
        ``configured_region`` from the env region.
        """
        if layout == "stacked":
            preset = self._stacked_layout_for(job)
            return f"stacked:v{DETECTOR_VERSION}:{self.export_facecam_region!r}:{preset!r}"
        if layout == "configured_region":
            return f"configured_region:{self.export_region!r}"
        return layout

    def _layouts_for(self, job: _JobRecord) -> tuple[str, ...]:
        # `stacked` needs no configuration: the facecam is detected in the clip, with the
        # channel preset or center_crop as fallbacks (#191).
        return available_layouts(self.export_region, facecam_detection=True)

    def _whole_vod_export(self, job: _JobRecord, duration_s: float) -> ExportAvailability:
        """Export availability over a source that covers the whole VOD."""
        return ExportAvailability(
            status="available",
            reason=None,
            message=None,
            min_start_s=0.0,
            max_end_s=duration_s,
            max_duration_s=MAX_EXPORT_DURATION_S,
            layouts=self._layouts_for(job),
        )

    def _segment_media(self, job: _JobRecord, candidate: Candidate) -> CandidateMedia:
        """Preview and export availability from a candidate's downloaded segment (#46).

        The segment state is created by `candidate_media`; this builds the response from
        whatever snapshot exists (`preparing`, `ready` or `failed`). A preview may be
        `preparing` while an export is already available (§ 5): a render fetches its own
        media in its first stage.
        """
        if self.segment_fetcher is None:
            return CandidateMedia(
                job_id=job.job_id,
                candidate_id=candidate.candidate_id,
                video_id=job.video_id,
                preview=PreviewMedia(
                    status="unavailable",
                    kind=None,
                    url=None,
                    media_start_s=None,
                    media_end_s=None,
                    reason="fetch_failed",
                    message=_PIPELINE_MEDIA_IN_MOCK,
                ),
                export=ExportAvailability(
                    status="unavailable",
                    reason="fetch_failed",
                    message=_PIPELINE_MEDIA_IN_MOCK,
                    min_start_s=None,
                    max_end_s=None,
                    max_duration_s=None,
                    layouts=(),
                ),
            )
        state = self._segments.get((job.job_id, candidate.candidate_id))
        if state is None:
            span = self._segment_span(job, candidate.start_s, candidate.end_s)
            state = _SegmentState("preparing", *span)
        if state.status == "failed":
            assert state.reason is not None
            if state.reason == "source_unavailable":
                preview = PreviewMedia(
                    status="unavailable",
                    kind=None,
                    url=None,
                    media_start_s=None,
                    media_end_s=None,
                    reason=state.reason,
                    message=_SOURCE_PREVIEW_UNAVAILABLE,
                )
                export = ExportAvailability(
                    status="unavailable",
                    reason=state.reason,
                    message=_SOURCE_EXPORT_UNAVAILABLE,
                    min_start_s=None,
                    max_end_s=None,
                    max_duration_s=None,
                    layouts=(),
                )
                return CandidateMedia(
                    job_id=job.job_id,
                    candidate_id=candidate.candidate_id,
                    video_id=job.video_id,
                    preview=preview,
                    export=export,
                )
            # Transient download failures remain retryable: preview reports `fetch_failed` once
            # (evicted after reporting), while exports keep the planned bounds so retrying an
            # export fetches fresh media instead of failing from cached state.
            preview = PreviewMedia(
                status="unavailable",
                kind=None,
                url=None,
                media_start_s=None,
                media_end_s=None,
                reason=state.reason,
                message=state.message or _FETCH_FAILED_PREVIEW,
            )
            export = ExportAvailability(
                status="available",
                reason=None,
                message=None,
                min_start_s=state.planned_begin_s,
                max_end_s=state.planned_end_s,
                max_duration_s=MAX_EXPORT_DURATION_S,
                layouts=self._layouts_for(job),
            )
            return CandidateMedia(
                job_id=job.job_id,
                candidate_id=candidate.candidate_id,
                video_id=job.video_id,
                preview=preview,
                export=export,
            )
        preview_end_s = _covered_preview_end(
            state.preview_coverage, candidate, self._job_result(job).vod.duration_s
        )
        if state.status == "ready" and state.media_id is None and state.preview_preparing:
            preview = PreviewMedia(
                status="preparing",
                kind=None,
                url=None,
                media_start_s=None,
                media_end_s=None,
                reason=None,
                message=_PREPARING_LOCAL_MESSAGE,
            )
        elif state.status == "ready" and (state.media_id is None or preview_end_s is None):
            # The segment exists for cuts, but no playable preview could be prepared from it
            # (#46), or the prepared file's measured coverage misses the candidate: report that
            # instead of serving unverified bytes (§ 5.1). Only a failed preparation is retried.
            preview = PreviewMedia(
                status="unavailable",
                kind=None,
                url=None,
                media_start_s=None,
                media_end_s=None,
                reason="fetch_failed",
                message=(
                    _SEGMENT_PREVIEW_FAILED
                    if state.media_id is None
                    else _SEGMENT_PREVIEW_UNCOVERED
                ),
            )
        elif state.status == "ready":
            preview = PreviewMedia(
                status="available",
                kind="video_file",
                url=f"/api/v1/media/{state.media_id}",
                media_start_s=state.begin_s,
                media_end_s=preview_end_s,
                reason=None,
                message=None,
            )
        else:
            preview = PreviewMedia(
                status="preparing",
                kind=None,
                url=None,
                media_start_s=None,
                media_end_s=None,
                reason=None,
                message=_PREPARING_MESSAGE,
            )
        # While preparing the bounds are the planned span; once ready, the probed coverage.
        begin = state.planned_begin_s if state.status == "preparing" else state.begin_s
        end = state.planned_end_s if state.status == "preparing" else state.end_s
        assert begin is not None
        assert end is not None
        export = ExportAvailability(
            status="available",
            reason=None,
            message=None,
            min_start_s=begin,
            max_end_s=end,
            max_duration_s=MAX_EXPORT_DURATION_S,
            layouts=self._layouts_for(job),
        )
        return CandidateMedia(
            job_id=job.job_id,
            candidate_id=candidate.candidate_id,
            video_id=job.video_id,
            preview=preview,
            export=export,
        )

    def _require_available(self, export: ExportAvailability) -> None:
        if export.status != "available":
            raise ApiException(
                409, "media_unavailable", export.message or "", {"reason": export.reason}
            )

    def _valid_video(self, video_path: str) -> bool:
        path = Path(video_path)
        if not path.is_absolute():
            return False
        valid, _ = validate_video_file(path, probe_video=self.probe_video)
        return valid

    # --- Workers -------------------------------------------------------------------------------

    def _sleep(self) -> None:
        if self.step_s > 0:
            time.sleep(self.step_s)

    def _enqueue(self, work: "queue.Queue[_W | None]", item: _W, run: Callable[[_W], None]) -> None:
        if self.synchronous:
            run(item)
        else:
            work.put(item)

    def _run_jobs(self) -> None:
        while (job_id := self._analysis_queue.get()) is not None:
            self._run_job_safely(job_id)

    def _run_job_safely(self, job_id: str) -> None:
        try:
            self._run_job(job_id)
        except Exception:  # a bug: fail the job, keep the worker alive
            log.exception("job failed job_id=%s", job_id)
            self._fail_job(job_id, ApiError(code="internal_error", message=_INTERNAL))

    def _run_job(self, job_id: str) -> None:
        if self.job_runner == "pipeline":
            self._run_pipeline_job(job_id)
            return
        with self._lock:
            record = self._jobs.get(job_id)
            if record is None:
                return
            scenario = mock.scenario_for(record.video_id)
            now = self.clock()
            record.state = "running"
            record.progress = mock.stage_progress(1)  # never running without progress (§ 3.4)
            record.times.started_at = record.times.updated_at = now
            self._persist_job(record)
        for index in range(1, len(mock.JOB_STAGES) + 1):
            if index > 1:
                with self._lock:
                    record = self._jobs.get(job_id)
                    if record is None:
                        return
                    record.progress = mock.stage_progress(index)
                    record.times.updated_at = self.clock()
                    self._persist_job(record)
            self._sleep()
            if scenario.error is not None and index == scenario.fail_at_stage:
                with self._lock:
                    record = self._jobs.get(job_id)
                    if record is None:
                        return
                    record.warnings = scenario.warnings
                self._fail_job(job_id, scenario.error)
                return
        assert scenario.result is not None
        now = self.clock()
        final_result = scenario.result.model_copy(update={"generated_at": now})
        result_file = self.output_dir / "analysis" / f"{job_id}.json"
        write_result_json(result_file, final_result)
        with self._lock:
            record = self._jobs.get(job_id)
            if record is None:
                return
            record.result = final_result
            record.result_path = result_file
            record.title = final_result.vod.title
            record.broadcaster_id = final_result.vod.broadcaster_id
            record.broadcaster_display_name = final_result.vod.broadcaster_display_name
            record.duration_s = final_result.vod.duration_s
            record.candidate_count = len(final_result.candidates)
            record.warnings = scenario.warnings
            record.state = "done"
            record.progress = None
            record.times.updated_at = record.times.finished_at = now
            self._persist_job(record)

    def _run_pipeline_job(self, job_id: str) -> None:
        """Run one job through the real analysis, reporting progress as stages begin."""
        assert self.data_dir is not None
        assert self.artifacts_dir is not None
        assert self.lexicon is not None
        assert self.tools_for is not None  # checked in __post_init__ for pipeline mode
        with self._lock:
            record = self._jobs.get(job_id)
            if record is None:
                return
            now = self.clock()
            video_id = record.video_id
            source_file = self._source_video_path_locked(record)
            missing_upload = record.upload_id is not None and (
                source_file is None or not source_file.is_file()
            )
            record.state = "running"
            record.progress = mock.stage_progress(1)  # never running without progress (§ 3.4)
            record.times.started_at = record.times.updated_at = now
            self._persist_job(record)
        if missing_upload:
            self._fail_job(
                job_id,
                ApiError(
                    code="invalid_video_upload",
                    message=(
                        "The uploaded video file is missing or can't be opened. Upload the video "
                        "again."
                    ),
                ),
            )
            return

        def stage(name: str) -> None:
            index = _STAGE_INDEX[name]
            with self._lock:
                rec = self._jobs.get(job_id)
                if rec is None:
                    return
                rec.progress = mock.stage_progress(index)
                rec.times.updated_at = self.clock()
                self._persist_job(rec)

        def warnings(found: Sequence[AnalysisWarning]) -> None:
            with self._lock:  # the job's warnings mean "degradations found so far" (§ 3.5)
                rec = self._jobs.get(job_id)
                if rec is None:
                    return
                rec.warnings = tuple(found)
                rec.times.updated_at = self.clock()
                self._persist_job(rec)

        tools = self.tools_for()
        log.info("pipeline job start job_id=%s video_id=%s", job_id, video_id)
        try:
            result = self.analyze(
                video_id,
                data_dir=self.data_dir,
                artifacts_dir=self.artifacts_dir,
                tools=tools,
                lexicon=self.lexicon,
                video_file=source_file,
                baseline=self.baseline,
                clock=self.clock,
                on_stage=stage,
                on_warnings=warnings,
            )
        except AnalyzeError as exc:  # the analysis failed with a § 3.5 code
            self._fail_job(job_id, ApiError(code=exc.code, message=exc.message))
            return
        finally:
            if tools.helix is not None:
                tools.helix.close()
        result_file = self.output_dir / "analysis" / f"{job_id}.json"
        write_result_json(result_file, result)
        with self._lock:
            rec = self._jobs.get(job_id)
            if rec is None:
                return
            now = self.clock()
            rec.result = result
            rec.result_path = result_file
            rec.title = result.vod.title
            rec.broadcaster_id = result.vod.broadcaster_id
            rec.broadcaster_display_name = result.vod.broadcaster_display_name
            rec.duration_s = result.vod.duration_s
            rec.candidate_count = len(result.candidates)
            rec.warnings = result.warnings
            rec.state = "done"
            rec.progress = None
            rec.times.updated_at = rec.times.finished_at = now
            self._persist_job(rec)
        log.info(
            "pipeline job done job_id=%s video_id=%s candidates=%d warnings=%d",
            job_id,
            video_id,
            len(result.candidates),
            len(result.warnings),
        )

    def _fail_job(self, job_id: str, error: ApiError) -> None:
        with self._lock:
            record = self._jobs.get(job_id)
            if record is None:
                return
            now = self.clock()
            record.state = "failed"
            record.error = error
            record.times.updated_at = record.times.finished_at = now
            try:
                self._persist_job(record)
            except (sqlite3.Error, OSError):
                log.exception("couldn't persist failed job state job_id=%s", job_id)

    def _run_renders(self) -> None:
        while (render_id := self._render_queue.get()) is not None:
            self._run_render_safely(render_id)

    def _run_copies(self) -> None:
        with self._lock:
            folder = self._output_folder
        if folder is not None:  # partial copies a crash or power loss left behind
            remove_stale_partials(folder)
        while (item := self._copy_queue.get()) is not None:
            self._run_copy_safely(item)

    def _run_copy_safely(self, item: tuple[str, Path]) -> None:
        """Copy a finished render into the output folder it finished under; never raises."""
        render_id, folder = item
        with self._lock:
            if self._output_folder != folder:
                return  # cleared or changed since the render finished: the copy is dropped
            record = self._renders.get(render_id)
            if record is None or record.state != "done" or record.file is None:
                return  # deleted with its job before the copy started
            source, kind = record.file, record.kind
            filename = self._render_filename(record)
        copied: Path | None = None
        message: str | None = None
        try:
            copied = copy_into_folder(source, folder, filename, stop=self._copy_stop)
            log.info("copied render id=%s to %s", render_id, copied)
        except CopyCancelled:
            log.info("stopped copying render id=%s: the server is shutting down", render_id)
            return
        except OutputFolderError as exc:
            log.warning("couldn't copy render id=%s into %s: %s", render_id, folder, exc.reason)
            message = _COPY_FOLDER_GONE if exc.reason == "not_creatable" else exc.message
        except FileNotFoundError:  # the folder was checked first, so this is the render's file
            log.warning("couldn't copy render id=%s: its file %s is gone", render_id, source)
            message = _COPY_SOURCE_GONE
        except OSError as exc:
            log.warning("couldn't copy render id=%s into %s: %s", render_id, folder, exc)
            message = _COPY_FAILED
        except Exception:  # a bug: report it, keep the worker alive
            log.exception("copying render id=%s failed", render_id)
            message = _COPY_FAILED
        with self._lock:
            self._last_copy = OutputCopy(
                render_id=render_id,
                kind=kind,
                state="copied" if copied is not None else "failed",
                filename=copied.name if copied is not None else None,
                message=message,
                finished_at=self.clock(),
            )

    def _run_fetches(self) -> None:
        while (item := self._fetch_queue.get()) is not None:
            self._run_fetch_item_safely(item)

    def _run_fetch_item_safely(self, item: _FetchWork) -> None:
        if item[0] == "file":
            job_id = item[1]
            try:
                self._prepare_file_media(job_id)
            except Exception:  # a bug: fail preview preparation, keep the worker alive
                log.exception("preview preparation failed job_id=%s", job_id)
                with self._lock:
                    current = self._file_media.get(job_id)
                    if current is None or current.status != "ready":
                        self._file_media[job_id] = _FileMediaState("failed")
            return
        if item[0] == "segment_preview":
            key = (item[1], item[2])
            try:
                self._retry_segment_preview(*key)
            except Exception:
                log.exception("segment preview retry failed job_id=%s", key[0])
                with self._lock:
                    state = self._segments.get(key)
                    if state is not None and state.media_id is None:
                        self._segments[key] = replace(
                            state, preview_preparing=False, preview_failed=True
                        )
            return
        key = (item[1], item[2])
        try:
            self._run_fetch(*key)
        except Exception:  # a bug: fail the segment's fetch, keep the worker alive
            log.exception("segment fetch failed job_id=%s", key[0])
            with self._lock:
                state = self._segments.get(key)
                if state is not None and state.status != "ready":
                    self._segments[key] = replace(state, status="failed", reason="fetch_failed")

    def _retry_segment_preview(self, job_id: str, candidate_id: str) -> None:
        """Retry preparing a browser-playable preview from an already downloaded segment."""
        key = (job_id, candidate_id)
        with self._lock:
            state = self._segments.get(key)
            if (
                state is None
                or state.status != "ready"
                or state.media_id is not None
                or state.path is None
                or state.begin_s is None
                or state.end_s is None
            ):
                return
            segment = VideoSegment(state.path, state.begin_s, state.end_s)
            self._active_segment_paths[segment.path] += 1
        try:
            self._sleep()
            media_id, coverage = self._register_segment_media(segment)
            with self._lock:
                current = self._segments.get(key)
                if current is None or current.status != "ready" or current.media_id is not None:
                    return
                self._segments[key] = replace(
                    current,
                    media_id=media_id,
                    preview_coverage=coverage,
                    preview_preparing=False,
                    preview_failed=media_id is None,
                )
        finally:
            self._release_segment_path(segment.path)

    def _run_fetch(self, job_id: str, candidate_id: str) -> None:
        """Download one candidate's video segment in the background and record it (#46)."""
        assert self.segment_fetcher is not None  # pipeline mode validates it
        with self._lock:
            job = self._jobs[job_id]
            candidate = self._candidate(job, candidate_id)
            state = self._segments[(job_id, candidate_id)]
            video_id, begin, end = job.video_id, state.planned_begin_s, state.planned_end_s
        self._sleep()
        log.info(
            "fetching video segment job_id=%s video_id=%s range=%.0f-%.0f",
            job_id,
            video_id,
            begin,
            end,
        )
        try:
            segment = self._fetch_coordinated(
                video_id,
                begin,
                end,
                need_begin_s=candidate.start_s,
                need_end_s=candidate.end_s,
                lease_path=True,
            )
        except Exception as exc:  # the downloader's typed errors (§ 5.2 reasons)
            reason, preview_message, _ = _classify_segment_error(exc)
            log.warning("video segment failed job_id=%s reason=%s: %s", job_id, reason, exc)
            self._store_segment(
                job,
                candidate,
                replace(
                    state,
                    status="failed",
                    reason=reason,
                    message=preview_message if reason == "fetch_failed" else None,
                ),
            )
            with self._lock:
                self._fetches_done += 1
            return
        try:
            media_id, coverage = self._register_segment_media(segment)
            self._store_segment(
                job,
                candidate,
                _SegmentState(
                    "ready",
                    planned_begin_s=state.planned_begin_s,
                    planned_end_s=state.planned_end_s,
                    path=segment.path,
                    begin_s=segment.begin_s,
                    end_s=segment.end_s,
                    media_id=media_id,
                    preview_coverage=coverage,
                    preview_failed=media_id is None,
                ),
                fail_if_short=True,
            )
        finally:
            self._release_segment_path(segment.path)
        with self._lock:
            self._fetches_done += 1
        log.info(
            "video segment ready job_id=%s video_id=%s range=%.0f-%.0f path=%s",
            job_id,
            video_id,
            begin,
            end,
            segment.path,
        )

    def _invalidate_short_segment(self, path: Path) -> None:
        """Remove a short downloaded segment from the cache slot so retries fetch fresh.

        If an active render or preview preparation is currently reading `path`, defer
        invalidation until it finishes so FFmpeg never loses its input file mid-operation. If
        another candidate's ready preview still points at `path`, relocate it to a detached
        sibling (ignored by `download_video_segment`'s cache lookup) and update both ready
        segment records and published `_media` URLs atomically so existing previews and exports
        keep working while retries download fresh footage. Detached files are reclaimed on
        `close()`.
        """
        with self._lock:
            if self._active_segment_paths.get(path, 0) > 0:
                self._pending_invalidations.add(path)
                return
            ready_keys = [
                key
                for key, state in self._segments.items()
                if state.status == "ready" and state.path == path
            ]
            media_ids = [mid for mid, media_path in self._media.items() if media_path == path]
            if ready_keys or media_ids:
                if path.exists():
                    detached = path.with_name(
                        f"{path.stem}.pinned-{secrets.token_hex(4)}{path.suffix}"
                    )
                    try:
                        path.replace(detached)
                    except OSError as exc:
                        log.warning(
                            "couldn't detach short video segment path=%s: %s",
                            path,
                            exc,
                        )
                    else:
                        self._detached_segments.append(detached)
                        for key in ready_keys:
                            self._segments[key] = replace(self._segments[key], path=detached)
                        for mid in media_ids:
                            self._media[mid] = detached
                return
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                log.warning("couldn't remove short video segment path=%s: %s", path, exc)

    def _release_segment_path(self, path: Path | None) -> None:
        """Release a lease on a downloaded segment and run any deferred invalidation."""
        if path is None:
            return
        with self._lock:
            remaining = self._active_segment_paths.get(path, 0) - 1
            if remaining > 0:
                self._active_segment_paths[path] = remaining
                return
            self._active_segment_paths.pop(path, None)
            should_invalidate = path in self._pending_invalidations
            self._pending_invalidations.discard(path)
        if should_invalidate:
            self._invalidate_short_segment(path)

    def _fetch_coordinated(
        self,
        video_id: str,
        begin_s: float,
        end_s: float,
        *,
        need_begin_s: float | None = None,
        need_end_s: float | None = None,
        end_tolerance_s: float = 0.0,
        lease_path: bool = False,
    ) -> VideoSegment:
        """One fetch per (VOD, range) at a time.

        A caller for a range already in flight waits for it (bounded) and then fetches again,
        which the segment cache serves at once. When `need_begin_s`/`need_end_s` are given, a
        returned file that doesn't cover that interval (within `end_tolerance_s` at the end)
        is invalidated from the cache slot before releasing the range key so waiting or
        retrying callers fetch fresh footage. When `lease_path` is True, a lease is acquired on
        the returned path before releasing the range key so a concurrent fetch cannot remove or
        rename it while the caller prepares previews or renders.

        Raises:
            TimeoutError: A fetch of the same range is taking longer than `FETCH_WAIT_S`.
            DownloadIntegrityError: The returned segment doesn't cover the needed interval.
        """
        assert self.segment_fetcher is not None  # pipeline mode validates it
        # The downloader rounds ranges to whole seconds, and ranges that round together write
        # the same files: coordinate on the rounded key (#46).
        key = (video_id, round(begin_s), round(end_s))
        with self._fetch_done:
            while key in self._fetching:
                if not self._fetch_done.wait(timeout=FETCH_WAIT_S):
                    raise TimeoutError(f"still fetching {video_id} {begin_s:g}-{end_s:g} s")
            self._fetching.add(key)
        try:
            segment = self.segment_fetcher(video_id, begin_s, end_s)
            if (
                need_begin_s is not None
                and need_end_s is not None
                and not (
                    segment.begin_s <= need_begin_s
                    and need_end_s <= segment.end_s + end_tolerance_s
                )
            ):
                self._invalidate_short_segment(segment.path)
                raise DownloadIntegrityError(
                    video_id,
                    f"segment {segment.begin_s:.2f}-{segment.end_s:.2f} s doesn't cover "
                    f"{need_begin_s:.2f}-{need_end_s:.2f} s",
                )
            if lease_path:
                with self._lock:
                    self._active_segment_paths[segment.path] += 1
            return segment
        finally:
            with self._fetch_done:
                self._fetching.discard(key)
                self._fetch_done.notify_all()

    def _fetch_segment(
        self,
        job: _JobRecord,
        begin_s: float,
        end_s: float,
        *,
        lease_path: bool = False,
    ) -> VideoSegment:
        """Fetch a video segment inline, a render's first stage (#46, § 6.3 and § 7.3).

        Raises:
            _MediaUnavailable: The segment couldn't be downloaded, or a fetch of the same range
                is taking longer than `FETCH_WAIT_S`; the reason is the contract's (§ 5.2).
        """
        assert self.segment_fetcher is not None  # pipeline mode validates it
        span = self._segment_span(job, begin_s, end_s)
        try:
            return self._fetch_coordinated(
                job.video_id,
                *span,
                need_begin_s=begin_s,
                need_end_s=end_s,
                end_tolerance_s=_RENDER_END_TOLERANCE_S,
                lease_path=lease_path,
            )
        except Exception as exc:
            reason, _, export_message = _classify_segment_error(exc)
            if reason != "source_unavailable":
                log.warning("video segment fetch failed video_id=%s: %s", job.video_id, exc)
            raise _MediaUnavailable(reason, export_message) from None

    def _store_segment(
        self,
        job: _JobRecord,
        candidate: Candidate,
        new: _SegmentState,
        *,
        fail_if_short: bool = False,
    ) -> None:
        """Record a candidate's segment state, never letting it regress (#46).

        A `failed` fetch never overwrites usable media (a render may have installed a segment
        while the preview's own fetch failed), a `ready` segment never replaces one that
        already covers the candidate as well or better, and a segment that doesn't cover the
        candidate's own bounds never becomes its preview at all. When `fail_if_short` is set,
        a short `ready` segment turns into a retryable `fetch_failed` state (unless usable
        media is already present) and its file is invalidated rather than leaving `preparing`
        stuck forever.
        """
        key = (job.job_id, candidate.candidate_id)
        short_path: Path | None = None
        stale_playable: Path | None = None
        with self._lock:
            current = self._segments.get(key)
            if new.status == "ready":
                if (
                    new.begin_s is None
                    or new.end_s is None
                    or not new.begin_s <= candidate.start_s
                    or not candidate.end_s <= new.end_s
                ):
                    if not fail_if_short:
                        return  # the render's narrow span: keep the preview's own coverage
                    short_path = new.path
                    if new.media_id is not None:
                        stale_playable = self._media.pop(new.media_id, None)
                    if current is None or current.status != "ready":
                        self._segments[key] = _SegmentState(
                            "failed",
                            planned_begin_s=new.planned_begin_s,
                            planned_end_s=new.planned_end_s,
                            reason="fetch_failed",
                        )
                elif (
                    current is not None
                    and current.status == "ready"
                    and current.begin_s is not None
                    and current.end_s is not None
                    and current.begin_s <= new.begin_s
                    and new.end_s <= current.end_s
                ):
                    return  # no better than what is already served
                else:
                    self._segments[key] = new
            elif (
                new.status == "failed"
                and current is not None
                and (
                    current.status == "ready"
                    or (
                        current.status == "failed"
                        and current.reason == "source_unavailable"
                        and new.reason != "source_unavailable"
                    )
                )
            ):
                return  # usable media beats a late failure; permanent refusals beat retryable ones
            else:
                self._segments[key] = new
        if short_path is not None:
            self._invalidate_short_segment(short_path)
        if stale_playable is not None and stale_playable != short_path:
            with contextlib.suppress(OSError):
                stale_playable.unlink(missing_ok=True)

    def _register_segment_media(self, segment: VideoSegment) -> tuple[str | None, Interval | None]:
        """Return the playable media ID and usable VOD coverage, or two Nones on failure.

        When preparation fails the preview reports unavailable instead of serving unverified
        bytes, while exports keep cutting the original segment.
        """
        media_id = new_id("med")
        target = self.output_dir / "media" / f"{media_id}.mp4"
        try:
            playable, coverage = _prepare_preview(
                segment.path, target, origin_s=segment.begin_s, max_end_s=segment.end_s
            )
        except Exception as exc:
            log.warning("segment preview preparation failed: %s", exc)
            return None, None
        with self._lock:
            self._media[media_id] = playable
        return media_id, coverage

    def _segment_for_render(
        self,
        job: _JobRecord,
        candidate: Candidate,
        begin_needed_s: float,
        end_needed_s: float,
        *,
        lease_path: bool = False,
    ) -> VideoSegment:
        """The segment a render cuts from.

        The candidate's own segment if it covers the trim; else a fresh fetch. A fetched span
        that covers the candidate also becomes its preview segment (#46); a narrower one (a
        trim far inside the candidate) never shrinks the preview's coverage.
        """
        key = (job.job_id, candidate.candidate_id)
        with self._lock:
            state = self._segments.get(key)
            if (
                state is not None
                and state.status == "ready"
                and state.begin_s is not None
                and state.end_s is not None
                and state.begin_s <= begin_needed_s
                and end_needed_s <= state.end_s + _RENDER_END_TOLERANCE_S
            ):
                assert state.path is not None
                if lease_path:
                    self._active_segment_paths[state.path] += 1
                return VideoSegment(state.path, state.begin_s, state.end_s)
        if state is not None and state.status == "failed" and state.reason == "source_unavailable":
            raise _MediaUnavailable(state.reason, _SOURCE_EXPORT_UNAVAILABLE)
        try:
            segment = self._fetch_segment(job, begin_needed_s, end_needed_s, lease_path=lease_path)
        except _MediaUnavailable as exc:
            if exc.reason == "source_unavailable":
                span = self._segment_span(job, candidate.start_s, candidate.end_s)
                self._store_segment(
                    job,
                    candidate,
                    _SegmentState("failed", *span, reason="source_unavailable"),
                )
            raise
        try:
            if segment.begin_s <= candidate.start_s and candidate.end_s <= segment.end_s:
                media_id, coverage = self._register_segment_media(segment)
                self._store_segment(
                    job,
                    candidate,
                    _SegmentState(
                        "ready",
                        planned_begin_s=segment.begin_s,
                        planned_end_s=segment.end_s,
                        path=segment.path,
                        begin_s=segment.begin_s,
                        end_s=segment.end_s,
                        media_id=media_id,
                        preview_coverage=coverage,
                        preview_failed=media_id is None,
                    ),
                )
        except Exception:
            if lease_path:
                self._release_segment_path(segment.path)
            raise
        return segment

    def _render_source(
        self, job: _JobRecord, candidate: Candidate, begin_needed_s: float, end_needed_s: float
    ) -> tuple[Path, float, Path | None]:
        """The file a render cuts from, its VOD time at file time 0, and any leased path (#46).

        The job's `video_path` is read directly; a mock-scenario job serves the sample; a
        pipeline job without a file gets a downloaded segment around the needed span with a
        lease that the caller must release via `_release_segment_path`.
        """
        if self._has_local_source(job):
            source = self._source_video_path(job)
            if source is None or not source.is_file():
                raise _MediaUnavailable("local_file_missing", _LOCAL_FILE_MISSING)
            return source, 0.0, None
        if not self._needs_segment(job):
            return self.samples.preview, 0.0, None
        segment = self._segment_for_render(
            job, candidate, begin_needed_s, end_needed_s, lease_path=True
        )
        return segment.path, segment.begin_s, segment.path

    def _run_render_safely(self, render_id: str) -> None:
        try:
            self._run_render(render_id)
        except Exception:  # a bug: fail the render, keep the worker alive
            log.exception("mock render failed id=%s", render_id)
            with self._lock:
                record = self._renders.get(render_id)
                if record is None:
                    return
                record.state = "failed"
                record.error = ApiError(code="internal_error", message=_INTERNAL)
                record.times.updated_at = record.times.finished_at = self.clock()
                try:
                    self._persist_render(record)
                except (sqlite3.Error, OSError):
                    log.exception("couldn't persist failed render state id=%s", render_id)

    def _run_render(self, render_id: str) -> None:
        with self._lock:
            record = self._renders.get(render_id)
            if record is None:
                return
        if record.kind == "export":
            self._run_export(record)
            return
        self._run_highlights(record)

    def _cached_segment_covers(
        self,
        path: Path,
        begin_s: float,
        end_s: float,
        need_start_s: float,
        need_end_s: float,
    ) -> bool:
        """Whether a cached segment file still exists and its shared A/V span covers the trim."""
        if not path.is_file():
            return False
        try:
            info = probe(path)
        except FFmpegError:
            return False
        if not info.video_streams:
            return False
        # Fake-fetcher tests supply a short dummy file with a longer synthetic span; only
        # check stream packet ends when the file's probed duration matches the recorded span.
        if abs(info.duration_s - (end_s - begin_s)) > 1.0:
            return True
        video = info.video_streams[0]
        audio = info.audio_streams[0] if info.audio_streams else None
        used_streams = [video.index] if audio is None else [video.index, audio.index]
        try:
            source_times = probe_stream_times(path)
            stream_ends_s = probe_stream_ends(
                path,
                used_streams,
                container_start_s=source_times.container_start_s,
                duration_hint_s=info.duration_s,
            )
        except FFmpegError:
            return False
        media_start_s, media_end_s = covered_span(
            source_times, used_streams, info.duration_s, stream_ends_s
        )
        local_start_s = need_start_s - begin_s
        local_end_s = need_end_s - begin_s
        return (
            media_start_s <= local_start_s + _RENDER_END_TOLERANCE_S
            and local_end_s <= media_end_s + _RENDER_END_TOLERANCE_S
        )

    def _highlights_segments(
        self, job: _JobRecord, requested: Sequence[HighlightsSegment]
    ) -> tuple[Path, list[Segment], list[Path]]:
        """Resolve per-moment downloaded segments for a highlights video.

        Reuses already-ready segments that exist on disk and cover a moment's shared A/V
        span, then merges overlapping padded fetch spans for the remaining moments so distant
        moments download only their own footage instead of the whole span between the first
        and last moment. Acquires a lease on every segment path returned; releases all leases
        if any fetch fails.
        """
        leased_paths: list[Path] = []
        resolved: list[Segment | None] = [None] * len(requested)
        pending: list[tuple[int, HighlightsSegment, float, float]] = []
        tentative: list[tuple[int, HighlightsSegment, Path, float, float]] = []
        try:
            with self._lock:
                ready_for_job = [
                    state
                    for (seg_job_id, _), state in self._segments.items()
                    if seg_job_id == job.job_id
                    and state.status == "ready"
                    and state.path is not None
                    and state.begin_s is not None
                    and state.end_s is not None
                ]
                for index, seg in enumerate(requested):
                    own = self._segments.get((job.job_id, seg.candidate_id))
                    if (
                        own is not None
                        and own.status == "failed"
                        and own.reason == "source_unavailable"
                    ):
                        raise _MediaUnavailable(own.reason, _SOURCE_EXPORT_UNAVAILABLE)
                    candidates = [own, *ready_for_job] if own is not None else ready_for_job
                    match = next(
                        (
                            state
                            for state in candidates
                            if state.status == "ready"
                            and state.path is not None
                            and state.begin_s is not None
                            and state.end_s is not None
                            and state.begin_s <= seg.start_s
                            and seg.end_s <= state.end_s + _RENDER_END_TOLERANCE_S
                            and state.path.is_file()
                        ),
                        None,
                    )
                    if match is not None:
                        assert match.path is not None
                        assert match.begin_s is not None
                        assert match.end_s is not None
                        self._active_segment_paths[match.path] += 1
                        leased_paths.append(match.path)
                        tentative.append((index, seg, match.path, match.begin_s, match.end_s))
                    else:
                        plan_begin, plan_end = self._segment_span(job, seg.start_s, seg.end_s)
                        pending.append((index, seg, plan_begin, plan_end))

            # Verify shared audio/video coverage outside the lock; fall back to fetching a
            # fresh padded span if a cached file's audio ends before the trim.
            for index, seg, path, begin_s, end_s in tentative:
                if self._cached_segment_covers(path, begin_s, end_s, seg.start_s, seg.end_s):
                    resolved[index] = Segment(
                        seg.candidate_id,
                        seg.start_s - begin_s,
                        seg.end_s - begin_s,
                        source=path,
                    )
                else:
                    leased_paths.remove(path)
                    self._release_segment_path(path)
                    plan_begin, plan_end = self._segment_span(job, seg.start_s, seg.end_s)
                    pending.append((index, seg, plan_begin, plan_end))

            # Merge overlapping (or touching on the whole-second download grid) planned fetch
            # spans so nearby moments share one download while distant moments each fetch only
            # their own footage.
            pending.sort(key=lambda item: (item[2], item[3], item[0]))
            groups: list[
                tuple[float, float, float, float, list[tuple[int, HighlightsSegment]]]
            ] = []
            for index, seg, plan_begin, plan_end in pending:
                if groups and round(plan_begin) <= round(groups[-1][1]):
                    g_begin, g_end, g_need_begin, g_need_end, members = groups[-1]
                    members.append((index, seg))
                    groups[-1] = (
                        g_begin,
                        max(g_end, plan_end),
                        min(g_need_begin, seg.start_s),
                        max(g_need_end, seg.end_s),
                        members,
                    )
                else:
                    groups.append((plan_begin, plan_end, seg.start_s, seg.end_s, [(index, seg)]))

            for plan_begin, plan_end, need_begin, need_end, members in groups:
                try:
                    video = self._fetch_coordinated(
                        job.video_id,
                        plan_begin,
                        plan_end,
                        need_begin_s=need_begin,
                        need_end_s=need_end,
                        end_tolerance_s=_RENDER_END_TOLERANCE_S,
                        lease_path=True,
                    )
                except Exception as exc:
                    reason, _, export_message = _classify_segment_error(exc)
                    if reason == "source_unavailable":
                        with self._lock:
                            candidates_to_mark = [
                                self._candidate(job, member_seg.candidate_id)
                                for _, member_seg in members
                            ]
                        for cand in candidates_to_mark:
                            span = self._segment_span(job, cand.start_s, cand.end_s)
                            self._store_segment(
                                job,
                                cand,
                                _SegmentState("failed", *span, reason="source_unavailable"),
                            )
                    else:
                        log.warning("video segment fetch failed video_id=%s: %s", job.video_id, exc)
                    raise _MediaUnavailable(reason, export_message) from None
                leased_paths.append(video.path)
                for index, seg in members:
                    resolved[index] = Segment(
                        seg.candidate_id,
                        seg.start_s - video.begin_s,
                        seg.end_s - video.begin_s,
                        source=video.path,
                    )
        except Exception:
            for path in leased_paths:
                self._release_segment_path(path)
            raise

        segments = [seg for seg in resolved if seg is not None]
        assert len(segments) == len(requested)
        assert segments[0].source is not None
        return segments[0].source, segments, leased_paths

    def _run_highlights(self, record: _RenderRecord) -> None:
        """Render one highlights video for real (§ 7.3): obtain the media, transcribe, join."""
        self._set_render_stage(record, "obtaining_media")
        with self._lock:
            job = self._jobs[record.job_id]
        leased_paths: list[Path] = []
        try:
            if self._needs_segment(job):
                source, segments, leased_paths = self._highlights_segments(job, record.segments)
            else:
                # No per-candidate span here: the job's file or the sample.
                first = self._candidate(job, record.segments[0].candidate_id)
                source, offset_s, leased = self._render_source(job, first, 0.0, 0.0)
                if leased is not None:
                    leased_paths.append(leased)
                segments = [
                    Segment(
                        segment.candidate_id,
                        segment.start_s - offset_s,
                        segment.end_s - offset_s,
                    )
                    for segment in record.segments
                ]
        except _MediaUnavailable as exc:
            self._finish_render(
                record,
                error=ApiError(
                    code="media_unavailable",
                    message=exc.message,
                    details={"reason": exc.reason},
                ),
            )
            return
        try:
            folder = self.output_dir / "highlights"
            target = folder / f"{record.render_id}.mp4"
            request = HighlightsRequest(
                source=source,
                segments=segments,
                output=target,
                order=record.order,
                captions=record.captions,
                overwrite=True,
            )
            try:
                result = self.highlights_render(
                    request,
                    transcriber=self.transcriber,
                    on_stage=lambda stage: self._set_render_stage(record, stage),
                )
            except CaptioningError as exc:
                log.warning("highlights captions failed id=%s: %s", record.render_id, exc)
                self._finish_render(record, error=_CAPTIONING_FAILED)
                return
            except (SourceMissingError, FileNotFoundError):
                self._finish_render(
                    record,
                    error=ApiError(
                        code="media_unavailable",
                        message=_LOCAL_FILE_MISSING,
                        details={"reason": "local_file_missing"},
                    ),
                )
                return
            except (ExportError, FFmpegError, OSError) as exc:
                log.warning("highlights render failed id=%s: %s", record.render_id, exc)
                self._finish_render(record, error=_RENDER_FAILED)
                return
        finally:
            for path in leased_paths:
                self._release_segment_path(path)
        # The finished file's probed duration replaces the planned total, published together
        # with the done state (§ 7.2).
        self._finish_render(record, file=result.output, duration_s=result.duration_s)

    def _set_render_stage(self, record: _RenderRecord, stage: str) -> None:
        index = RENDER_STAGES.index(stage) + 1
        final = (
            "Rendering the clip" if record.kind == "export" else "Rendering the highlights video"
        )
        message = _RENDER_MESSAGES.get(stage, final)
        with self._lock:
            if record.render_id not in self._renders:
                return
            now = self.clock()
            if record.state == "queued":  # running and its first progress together (§ 3.4)
                record.state = "running"
                record.times.started_at = now
            record.progress = Progress(
                stage=stage,
                stage_index=index,
                stage_count=len(RENDER_STAGES),
                stage_fraction=None,
                message=message,
            )
            record.times.updated_at = now
            self._persist_render(record)

    def _finish_render(
        self,
        record: _RenderRecord,
        *,
        file: Path | None = None,
        error: ApiError | None = None,
        duration_s: float | None = None,
    ) -> None:
        copy_to: Path | None = None
        with self._lock:
            if record.render_id not in self._renders:
                return
            now = self.clock()
            record.state = "done" if error is None else "failed"
            record.file = file
            record.error = error
            if error is None:
                record.progress = None
                self._assign_render_filename(record)
                # Published with the state, so a status read never sees done with the
                # planned duration still in place (§ 7.2; the highlights probed it).
                if duration_s is not None:
                    record.duration_s = duration_s
            record.times.updated_at = record.times.finished_at = now
            try:
                self._persist_render(record)
            except (sqlite3.Error, OSError):
                if error is not None:
                    log.exception("couldn't persist failed render state id=%s", record.render_id)
                else:
                    raise
            if error is None and file is not None:
                copy_to = self._output_folder
        if copy_to is not None:
            self._enqueue(self._copy_queue, (record.render_id, copy_to), self._run_copy_safely)

    def _run_export(self, record: _RenderRecord) -> None:
        """Render one export for real (§ 6.3): obtain the media, transcribe, render."""
        request = record.request
        assert request is not None
        self._set_render_stage(record, "obtaining_media")
        with self._lock:
            job = self._jobs[record.job_id]
            candidate = self._candidate(job, request.candidate_id)
        try:
            source, offset_s, leased_path = self._render_source(
                job, candidate, request.start_s, request.end_s
            )
        except _MediaUnavailable as exc:
            self._finish_render(
                record,
                error=ApiError(
                    code="media_unavailable",
                    message=exc.message,
                    details={"reason": exc.reason},
                ),
            )
            return
        try:
            folder = self.output_dir / "exports"
            target = folder / f"{record.render_id}.mp4"
            # A downloaded segment starts at VOD time `offset_s`; the trim is VOD time, so the
            # cut's interval is the trim shifted into the file (#46).
            spec = ClipSpec(
                source=source,
                interval=Interval(request.start_s - offset_s, request.end_s - offset_s),
                output=target,
                layout=request.layout,
                captions=request.captions,
                # An explicit facecam region wins; otherwise the facecam is detected in the clip
                # and the channel preset is the fallback (#191).
                stacked_layout=(
                    stacked_layout_from_region(self.export_facecam_region)
                    if request.layout == "stacked" and self.export_facecam_region is not None
                    else None
                ),
                stacked_fallback=(
                    self._stacked_layout_for(job) if request.layout == "stacked" else None
                ),
            )
            try:
                self.render(
                    spec,
                    region=self.export_region,
                    transcriber=self.transcriber,
                    on_stage=lambda stage: self._set_render_stage(record, stage),
                )
            except CaptioningError as exc:
                log.warning("export captions failed id=%s: %s", record.render_id, exc)
                self._finish_render(record, error=_CAPTIONING_FAILED)
                return
            except SourceMissingError:
                self._finish_render(
                    record,
                    error=ApiError(
                        code="media_unavailable",
                        message=_LOCAL_FILE_MISSING,
                        details={"reason": "local_file_missing"},
                    ),
                )
                return
            except (IntervalOutsideMediaError, IntervalTooShortError) as exc:
                log.warning("export outside the media id=%s: %s", record.render_id, exc)
                self._finish_render(record, error=_OUTSIDE_MEDIA)
                return
            except (ExportError, FFmpegError, OSError) as exc:  # OSError: e.g. the disk is full
                log.warning("export render failed id=%s: %s", record.render_id, exc)
                self._finish_render(record, error=_RENDER_FAILED)
                return
        finally:
            self._release_segment_path(leased_path)
        if not target.is_file():  # a renderer that returned without writing is a bug
            raise RuntimeError(f"the renderer wrote no file for {record.render_id}")
        self._finish_render(record, file=target)


_INTERNAL: Final = "Something went wrong on the server. Try again."
_RESULT_FILE_MISSING: Final = "This analysis result is no longer on disk. Analyze the VOD again."
_DELETE_FAILED: Final = (
    "Some files for this job couldn't be removed from disk. Let any download of them finish, "
    "close programs using them, and try deleting again."
)
_PIPELINE_MEDIA_IN_MOCK: Final = (
    "This VOD was analyzed with the real pipeline. Restart the server in pipeline mode to "
    "preview or export it."
)
_JOB_INTERRUPTED: Final = ApiError(
    code="interrupted",
    message="The server stopped while this analysis was running. Analyze the VOD again.",
)
_EXPORT_INTERRUPTED: Final = ApiError(
    code="interrupted",
    message="The server stopped while this export was running. Export the clip again.",
)
_HIGHLIGHTS_INTERRUPTED: Final = ApiError(
    code="interrupted",
    message="The server stopped while this highlights video was rendering. Make it again.",
)


def _job_key(record: _JobRecord) -> tuple[str, str | None, str | None]:
    """What makes two analyses the same: the VOD and the video source it reads (#186)."""
    return record.video_id, record.video_path, record.upload_id


def _job_sort_key(record: _JobRecord) -> tuple[datetime, int, str]:
    """Library order key: `created_at`, creation sequence, then `job_id` (§ 3.7)."""
    return record.times.created_at, record.seq, record.job_id


def _same_layout(recorded: str | None, current: str, layout: str) -> bool:
    """Whether a render's layout matches a new request's (`MockService._layout_key`).

    A render restored from the database has no key; only ``center_crop``, which reads no
    configuration, can be reused then.
    """
    return recorded == current or (recorded is None and layout == "center_crop")


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


_LOCAL_FILE_MISSING: Final = (
    "The video file for this analysis is missing or can't be read. Check that it's still there, "
    "or analyze the VOD again."
)
_SOURCE_PREVIEW_UNAVAILABLE: Final = (
    "Preview isn't available: Twitch doesn't offer this VOD's video for download. "
    "It may have been deleted or be subscriber-only."
)
_SOURCE_EXPORT_UNAVAILABLE: Final = (
    "Export isn't available: Twitch doesn't offer this VOD's video for download. "
    "If you have the video file, analyze the VOD again with it."
)
_CAPTIONING_FAILED: Final = ApiError(
    code="captioning_failed",
    message="Captions couldn't be generated for this clip. Try again, or export without captions.",
)
_RENDER_FAILED: Final = ApiError(
    code="render_failed",
    message="The clip couldn't be rendered. Try again, or pick another layout.",
)
_OUTSIDE_MEDIA: Final = ApiError(
    code="render_failed",
    message=(
        "The video file doesn't cover this whole clip. Trim the clip to fit inside the video and "
        "try again."
    ),
)


def _within(
    export: ExportAvailability,
    start_s: float,
    end_s: float,
    *,
    min_duration_s: float = 0.0,
) -> bool:
    assert export.min_start_s is not None
    assert export.max_end_s is not None
    assert export.max_duration_s is not None
    if not (export.min_start_s <= start_s < end_s <= export.max_end_s):
        return False
    duration_s = end_s - start_s
    if duration_s > export.max_duration_s:
        return False
    return not (min_duration_s > 0.0 and duration_s < min_duration_s - DURATION_EPSILON_S)


# How long a render waits for a fetch of the same range that is already running (#46):
# far more than a segment download needs, bounded so a render never hangs forever.
FETCH_WAIT_S: Final = 600.0


def _covered_preview_end(
    coverage: Interval | None, candidate: Candidate, duration_s: float
) -> float | None:
    """The `media_end_s` to advertise, or None when the preview doesn't cover the candidate.

    `coverage` is the prepared file's measured usable span in VOD time (`_prepare_preview`); its
    end is capped by the VOD duration, and the capped span must contain the candidate's own
    ``[start_s, end_s)`` (§ 5.3 guarantee).
    """
    if coverage is None:
        return None
    end_s = min(coverage.end_s, duration_s)
    if candidate.start_s < coverage.start_s or end_s < candidate.end_s:
        return None
    return end_s


def _prepare_preview(
    source: Path, target: Path, *, origin_s: float, max_end_s: float | None = None
) -> tuple[Path, Interval]:
    """Prepare a playable file and measure its usable VOD span from headers and tail packets."""
    playable = _playable_copy(source, target)
    try:
        info, times = probe_media_and_times(playable)
        used_streams = [info.video_streams[0].index]
        if info.audio_streams:
            used_streams.append(info.audio_streams[0].index)
        stream_ends_s = probe_stream_ends(
            playable,
            used_streams,
            container_start_s=times.container_start_s,
            duration_hint_s=info.duration_s,
        )
        start_s, end_s = covered_span(times, used_streams, info.duration_s, stream_ends_s)
        end_s = origin_s + min(end_s, info.duration_s)
        if max_end_s is not None:
            end_s = min(end_s, max_end_s)
        return playable, Interval(origin_s + start_s, end_s)
    except Exception:
        if playable == target:
            with contextlib.suppress(OSError):
                playable.unlink(missing_ok=True)
        raise


def _playable_copy(source: Path, target: Path) -> Path:
    """An H.264/AAC MP4 a browser can play, from `source` (#46, § 5.1).

    The preview URL feeds an HTML ``<video>`` element. The source itself is returned when it
    already is playable; the same codecs in another container (MPEG-TS, AVI) are remuxed,
    which only rewrites the container; anything else is transcoded once into `target` (named
    for its media ID, so it is made at most once per media). Reads `source` with
    `TIMELINE_OPTIONS` (and `TIMELINE_AUDIO_FILTER` when transcoding audio) so the
    preview stays on the same timeline as analysis and exports. The original is kept for
    rendering.

    Raises:
        FFmpegError: FFmpeg or FFprobe failed (not installed, timed out); nothing is written.
    """
    info = probe(source)
    if not info.video_streams:
        raise FFmpegError(f"preview source {source} has no video stream")
    video = info.video_streams[0]
    audio = info.audio_streams[0] if info.audio_streams else None
    # 8-bit yuv420p is what browsers reliably decode; 4:4:4 and 10-bit H.264 may be
    # technically playable by some, but a preview must just work everywhere (#46).
    browser_video = video.codec_name == OUTPUT_PLAYABLE_VIDEO and (
        video.pix_fmt in OUTPUT_PLAYABLE_PIX_FMTS
    )
    browser_audio = audio is None or audio.codec_name == OUTPUT_PLAYABLE_AUDIO
    if browser_video and browser_audio and "mp4" in info.format_name.split(","):
        return source

    video_options: tuple[str, ...] = ("-map", f"0:{video.index}", "-c:v", "copy")
    if not browser_video:
        video_options = (
            "-map",
            f"0:{video.index}",
            "-c:v",
            OUTPUT_PLAYABLE_VIDEO_LIB,
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-pix_fmt",
            "yuv420p",
            "-fps_mode",
            "passthrough",
        )
    audio_options: tuple[str, ...] = ()
    if audio is not None:
        audio_options = ("-map", f"0:{audio.index}", "-c:a", "copy")
        if not browser_audio:
            audio_options = (
                "-map",
                f"0:{audio.index}",
                "-c:a",
                OUTPUT_PLAYABLE_AUDIO,
                "-af",
                TIMELINE_AUDIO_FILTER,
            )
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(f"{target.stem}.part.mp4")
    try:
        run_command(
            ffmpeg_command(
                "-y",
                *TIMELINE_OPTIONS,
                "-i",
                source,
                *video_options,
                *audio_options,
                "-movflags",
                "+faststart",
                partial,
            ),
            timeout_s=encode_timeout_s(info.duration_s),
        )
        partial.replace(target)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    return target


def _bounds_message(
    export: ExportAvailability,
    start_s: float,
    end_s: float,
    what: str,
    *,
    min_duration_s: float = 0.0,
) -> str:
    if end_s <= start_s:
        return "The end must come after the start. Adjust the selection and try again."
    if end_s - start_s > (export.max_duration_s or 0):
        seconds = f"{export.max_duration_s:g}"
        tail = (
            "Shorten the selection and try again."
            if what == "Clips"
            else "Shorten it and try again."
        )
        return f"{what} can be at most {seconds} seconds long. {tail}"
    if (
        export.min_start_s is not None
        and export.max_end_s is not None
        and (start_s < export.min_start_s or end_s > export.max_end_s)
    ):
        return "The selection goes past the start or end of the video. Adjust it and try again."
    if min_duration_s > 0.0 and end_s - start_s < min_duration_s - DURATION_EPSILON_S:
        return (
            f"{what} must be at least {min_duration_s:g} seconds long. "
            "Widen the selection and try again."
        )
    return "The selection goes past the start or end of the video. Adjust it and try again."


def _ordered(
    segments: list[HighlightsSegment],
    candidates: dict[str, Candidate],
    order: Literal["chronological", "rank"],
) -> list[HighlightsSegment]:
    if order == "chronological":
        return sorted(segments, key=lambda s: (s.start_s, s.end_s, candidates[s.candidate_id].rank))
    return sorted(segments, key=lambda s: (candidates[s.candidate_id].rank, s.start_s))
