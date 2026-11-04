"""The mock backend's state and background work (docs/architecture/api-contract.md §§ 3, 5-7, 9).

`MockService` keeps job, export, highlights and media records in memory (a restart forgets
them, § 9.3), validates requests in the contract's order and raises `ApiException` with the
contract's codes. Two worker threads run the work: one for analysis jobs, one shared by exports
and highlights videos (one render at a time), each first in, first out. The mock job walks
through the six analysis stages and finishes with its § 11 scenario.

Exports are real: the render worker cuts the requested interval from the job's ``video_path``,
or from the sample preview video when the job has none, and renders it with
`autoclipper.export.clip.render_clip` (exact interval, layout, optional captions), one file per
export. Highlights videos are still simulated: they step through their stages and finish with a
copy of a pre-generated sample MP4 (#72 replaces that).
"""

import logging
import queue
import secrets
import shutil
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Literal, Protocol

from autoclipper.analysis import AnalysisResult, AnalysisWarning, Candidate
from autoclipper.api import mock
from autoclipper.export.captions import CaptioningError, Stage, Transcriber
from autoclipper.export.clip import (
    MAX_CLIP_DURATION_S,
    ClipSpec,
    IntervalOutsideMediaError,
    SourceMissingError,
    available_layouts,
    render_clip,
)
from autoclipper.export.cut import Interval, IntervalTooShortError
from autoclipper.export.errors import ExportError
from autoclipper.ffmpeg import FFmpegError, ffmpeg_command, probe, run_command
from autoclipper.schemas import (
    ApiError,
    CandidateMedia,
    CreateExportRequest,
    CreateHighlightsRequest,
    CreateJobRequest,
    Export,
    ExportAvailability,
    HighlightsSegment,
    HighlightsVideo,
    Job,
    PreviewMedia,
    Progress,
    State,
    parse_vod,
)

log = logging.getLogger(__name__)

MAX_EXPORT_DURATION_S: Final = MAX_CLIP_DURATION_S  # api-contract.md § 5, labeling.md § 2
MAX_HIGHLIGHTS_DURATION_S: Final = 900.0  # § 7.1
RENDER_STAGES: Final = ("obtaining_media", "transcribing", "rendering")
_RENDER_MESSAGES: Final = {
    "obtaining_media": "Getting the video",
    "transcribing": "Generating captions",
}

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
    highlights: Path


PREVIEW_DURATION_S: Final = 360.0  # the longest mock VOD
_SAMPLE_TIMEOUT_S: Final = 600.0


def ensure_sample_media(
    directory: Path, *, preview_duration_s: float = PREVIEW_DURATION_S
) -> SampleMedia:
    """Generate the sample MP4s into `directory` unless they are already there and readable.

    The preview is a 640x360 test pattern with a tone, `preview_duration_s` long (exports of a
    job without a ``video_path`` are cut from it); the highlights sample is 1920x1080, 5 s. All
    go through the shared FFmpeg runner.

    Raises:
        FFmpegError: FFmpeg is missing or failed.
    """
    directory.mkdir(parents=True, exist_ok=True)
    specs = {
        "preview": ("preview.mp4", 640, 360, preview_duration_s),
        "highlights": ("highlights-1920x1080.mp4", 1920, 1080, 5.0),
    }
    paths: dict[str, Path] = {}
    for name, (filename, width, height, duration_s) in specs.items():
        path = directory / filename
        paths[name] = path
        if path.is_file():
            try:
                info = probe(path)
                if info.duration_s >= duration_s - 0.5 and info.video_streams:
                    continue
            except FFmpegError:
                pass  # damaged: generate it again
        partial = path.with_name(f"{path.stem}.part.mp4")
        command = ffmpeg_command(
            "-y",
            *("-f", "lavfi", "-i", f"testsrc2=size={width}x{height}:rate=10:duration={duration_s}"),
            *("-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:duration={duration_s}"),
            *("-c:v", "libx264", "-preset", "ultrafast", "-crf", "32", "-pix_fmt", "yuv420p"),
            *("-c:a", "aac", "-movflags", "+faststart", "-shortest", partial),
        )
        try:
            run_command(command, timeout_s=_SAMPLE_TIMEOUT_S)
            partial.replace(path)
        finally:
            partial.unlink(missing_ok=True)
        log.info("generated mock sample media path=%s", path)
    return SampleMedia(paths["preview"], paths["highlights"])


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
    state: State = "queued"
    progress: Progress | None = None
    warnings: tuple[AnalysisWarning, ...] = ()
    error: ApiError | None = None
    result: AnalysisResult | None = None

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

    def export_model(self) -> Export:
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
            created_at=self.times.created_at,
            started_at=self.times.started_at,
            updated_at=self.times.updated_at,
            finished_at=self.times.finished_at,
        )

    def highlights_model(self) -> HighlightsVideo:
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
            duration_s=self.duration_s if done else None,
            created_at=self.times.created_at,
            started_at=self.times.started_at,
            updated_at=self.times.updated_at,
            finished_at=self.times.finished_at,
        )


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


@dataclass
class MockService:
    """The mock backend.

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
    """

    samples: SampleMedia
    output_dir: Path
    step_s: float = 0.5
    probe_video: Callable[[Path], bool] | None = None
    clock: Clock = _utc_now
    synchronous: bool = False
    export_region: tuple[float, float, float, float] | None = None
    transcriber: Transcriber | None = None
    render: ClipRenderer = render_clip
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)
    _jobs: dict[str, _JobRecord] = field(default_factory=dict, init=False)
    _renders: dict[str, _RenderRecord] = field(default_factory=dict, init=False)
    _media: dict[str, Path] = field(default_factory=dict, init=False)
    _preview_media_id: str = field(default="", init=False)
    _analysis_queue: "queue.Queue[str | None]" = field(default_factory=queue.Queue, init=False)
    _render_queue: "queue.Queue[str | None]" = field(default_factory=queue.Queue, init=False)
    _threads: list[threading.Thread] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        self._preview_media_id = new_id("med")
        self._media[self._preview_media_id] = self.samples.preview
        if self.synchronous:
            return
        for name, target in (("analysis", self._run_jobs), ("render", self._run_renders)):
            thread = threading.Thread(target=target, name=f"autoclipper-{name}", daemon=True)
            thread.start()
            self._threads.append(thread)

    def close(self) -> None:
        """Stop the workers: queued work is dropped, the work in hand finishes."""
        if self.synchronous:
            return
        for work in (self._analysis_queue, self._render_queue):
            while True:  # queued work is dropped, like everything else on restart (§ 9.3)
                try:
                    work.get_nowait()
                except queue.Empty:
                    break
            work.put(None)
        for thread in self._threads:
            thread.join(timeout=5)

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
        if request.video_path is not None and not self._valid_video(request.video_path):
            raise ApiException(
                422,
                "invalid_video_path",
                "That video file can't be opened. Check the path and that it's a complete video "
                "file.",
            )
        with self._lock:
            for record in self._jobs.values():
                if (
                    record.video_id == video_id
                    and record.video_path == request.video_path
                    and record.state in ("queued", "running")
                ):
                    return record.model(), False
            now = self.clock()
            record = _JobRecord(new_id("job"), video_id, request.video_path, _Timestamps(now, now))
            self._jobs[record.job_id] = record
            model = record.model()
        self._enqueue(self._analysis_queue, record.job_id, self._run_job_safely)
        return model, True

    def get_job(self, job_id: str) -> Job:
        """The job (`404 job_not_found` if unknown)."""
        with self._lock:
            return self._job(job_id).model()

    def get_result(self, job_id: str) -> AnalysisResult:
        """The result of a done job (`409 job_not_done` before)."""
        with self._lock:
            record = self._done_job(job_id)
            assert record.result is not None
            return record.result

    def candidate_media(self, job_id: str, candidate_id: str) -> CandidateMedia:
        """Preview and export availability of one candidate (§ 5)."""
        with self._lock:
            record = self._done_job(job_id)
            candidate = self._candidate(record, candidate_id)
            return self._media_for(record, candidate)

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
            if not _within(export, request.start_s, request.end_s):
                raise ApiException(
                    422,
                    "invalid_export_bounds",
                    _bounds_message(export, request.start_s, request.end_s, "Clips"),
                    {
                        "min_start_s": export.min_start_s,
                        "max_end_s": export.max_end_s,
                        "max_duration_s": export.max_duration_s,
                    },
                )
            for record in self._renders.values():
                if (
                    record.kind == "export"
                    and record.request == request
                    and record.state in ("queued", "running")
                ):
                    return record.export_model(), False
            now = self.clock()
            record = _RenderRecord(
                "export",
                new_id("exp"),
                job.job_id,
                job.video_id,
                request.captions,
                _Timestamps(now, now),
                request=request,
            )
            self._renders[record.render_id] = record
            model = record.export_model()
        self._enqueue(self._render_queue, record.render_id, self._run_render_safely)
        return model, True

    def get_export(self, export_id: str) -> Export:
        """The export (`404 export_not_found` if unknown)."""
        with self._lock:
            return self._render(export_id, "export").export_model()

    def export_file(self, export_id: str) -> tuple[Path, str]:
        """The finished export's file and suggested download name."""
        with self._lock:
            record = self._render(export_id, "export")
            path = self._finished_file(record)
            assert record.request is not None
            name = (
                f"autoclipper_{record.video_id}_{record.request.start_s}-{record.request.end_s}.mp4"
            )
            return path, name

    # --- Highlights ----------------------------------------------------------------------------

    def create_highlights(self, request: CreateHighlightsRequest) -> tuple[HighlightsVideo, bool]:
        """Create a highlights video, or return an identical queued/running one."""
        with self._lock:
            job = self._done_job(request.job_id)
            assert job.result is not None
            candidates = {c.candidate_id: c for c in job.result.candidates}
            if request.segments is None:
                top = job.result.candidates[: request.count]
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
            self._renders[record.render_id] = record
            model = record.highlights_model()
        self._enqueue(self._render_queue, record.render_id, self._run_render_safely)
        return model, True

    def get_highlights(self, highlights_id: str) -> HighlightsVideo:
        """The highlights video (`404 highlights_not_found` if unknown)."""
        with self._lock:
            return self._render(highlights_id, "highlights").highlights_model()

    def highlights_file(self, highlights_id: str) -> tuple[Path, str]:
        """The finished highlights file and suggested download name."""
        with self._lock:
            record = self._render(highlights_id, "highlights")
            return self._finished_file(record), f"autoclipper_{record.video_id}_highlights.mp4"

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
        assert job.result is not None
        for candidate in job.result.candidates:
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
        assert job.result is not None
        duration_s = job.result.vod.duration_s
        # The VOD's video can't be downloaded, but a local file works (§ 5 source_unavailable).
        if job.video_path is not None and not Path(job.video_path).is_file():
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
        elif job.video_id == mock.NO_MEDIA_VIDEO_ID and job.video_path is None:
            preview = PreviewMedia(
                status="unavailable",
                kind=None,
                url=None,
                media_start_s=None,
                media_end_s=None,
                reason="source_unavailable",
                message=(
                    "Preview isn't available: Twitch doesn't offer this VOD's video for download. "
                    "It may have been deleted or be subscriber-only."
                ),
            )
            export = ExportAvailability(
                status="unavailable",
                reason="source_unavailable",
                message=(
                    "Export isn't available: Twitch doesn't offer this VOD's video for download. "
                    "If you have the video file, analyze the VOD again with it."
                ),
                min_start_s=None,
                max_end_s=None,
                max_duration_s=None,
                layouts=(),
            )
        else:
            preview = PreviewMedia(
                status="available",
                kind="video_file",
                url=f"/api/v1/media/{self._preview_media_id}",
                media_start_s=0.0,
                media_end_s=duration_s,
                reason=None,
                message=None,
            )
            export = ExportAvailability(
                status="available",
                reason=None,
                message=None,
                min_start_s=0.0,
                max_end_s=duration_s,
                max_duration_s=MAX_EXPORT_DURATION_S,
                layouts=available_layouts(self.export_region),
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
        if not path.is_absolute() or not path.is_file():
            return False
        if self.probe_video is not None:
            return self.probe_video(path)
        try:
            info = probe(path)
        except FFmpegError:
            return False
        return bool(info.video_streams) and info.duration_s > 0

    # --- Workers -------------------------------------------------------------------------------

    def _sleep(self) -> None:
        if self.step_s > 0:
            time.sleep(self.step_s)

    def _enqueue(
        self, work: "queue.Queue[str | None]", item: str, run: Callable[[str], None]
    ) -> None:
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
            log.exception("mock job failed job_id=%s", job_id)
            self._fail_job(job_id, ApiError(code="internal_error", message=_INTERNAL))

    def _run_job(self, job_id: str) -> None:
        with self._lock:
            record = self._jobs[job_id]
            scenario = mock.scenario_for(record.video_id)
            now = self.clock()
            record.state = "running"
            record.progress = mock.stage_progress(1)  # never running without progress (§ 3.4)
            record.times.started_at = record.times.updated_at = now
        for index in range(1, len(mock.JOB_STAGES) + 1):
            if index > 1:
                with self._lock:
                    record.progress = mock.stage_progress(index)
                    record.times.updated_at = self.clock()
            self._sleep()
            if scenario.error is not None and index == scenario.fail_at_stage:
                with self._lock:
                    record.warnings = scenario.warnings
                self._fail_job(job_id, scenario.error)
                return
        assert scenario.result is not None
        with self._lock:
            now = self.clock()
            record.result = scenario.result.model_copy(update={"generated_at": now})
            record.warnings = scenario.warnings
            record.state = "done"
            record.progress = None
            record.times.updated_at = record.times.finished_at = now

    def _fail_job(self, job_id: str, error: ApiError) -> None:
        with self._lock:
            record = self._jobs[job_id]
            now = self.clock()
            record.state = "failed"
            record.error = error
            record.times.updated_at = record.times.finished_at = now

    def _run_renders(self) -> None:
        while (render_id := self._render_queue.get()) is not None:
            self._run_render_safely(render_id)

    def _run_render_safely(self, render_id: str) -> None:
        try:
            self._run_render(render_id)
        except Exception:  # a bug: fail the render, keep the worker alive
            log.exception("mock render failed id=%s", render_id)
            with self._lock:
                record = self._renders[render_id]
                record.state = "failed"
                record.error = ApiError(code="internal_error", message=_INTERNAL)
                record.times.updated_at = record.times.finished_at = self.clock()

    def _run_render(self, render_id: str) -> None:
        with self._lock:
            record = self._renders[render_id]
        if record.kind == "export":
            self._run_export(record)
            return
        stages = [
            (index, stage)
            for index, stage in enumerate(RENDER_STAGES, start=1)
            if stage != "transcribing" or record.captions  # skipped stages are passed over
        ]
        for position, (index, stage) in enumerate(stages):
            message = _RENDER_MESSAGES.get(stage, "Rendering the highlights video")
            with self._lock:
                now = self.clock()
                if position == 0:  # running and its first progress together (§ 3.4)
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
            self._sleep()
        sample = self.samples.highlights
        folder = self.output_dir / "highlights"
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / f"{render_id}.mp4"
        partial = folder / f"{render_id}.part"
        try:  # never leave a truncated file under the final name
            shutil.copyfile(sample, partial)
            partial.replace(target)
        finally:
            partial.unlink(missing_ok=True)
        with self._lock:
            now = self.clock()
            record.file = target
            record.state = "done"
            record.progress = None
            record.times.updated_at = record.times.finished_at = now

    def _set_render_stage(self, record: _RenderRecord, stage: str) -> None:
        index = RENDER_STAGES.index(stage) + 1
        message = _RENDER_MESSAGES.get(stage, "Rendering the clip")
        with self._lock:
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

    def _finish_render(
        self, record: _RenderRecord, *, file: Path | None = None, error: ApiError | None = None
    ) -> None:
        with self._lock:
            now = self.clock()
            record.state = "done" if error is None else "failed"
            record.file = file
            record.error = error
            if error is None:
                record.progress = None
            record.times.updated_at = record.times.finished_at = now

    def _run_export(self, record: _RenderRecord) -> None:
        """Render one export for real (§ 6.3): obtain the media, transcribe, render."""
        request = record.request
        assert request is not None
        self._set_render_stage(record, "obtaining_media")
        with self._lock:
            video_path = self._jobs[record.job_id].video_path
        source = Path(video_path) if video_path is not None else self.samples.preview
        if not source.is_file():
            self._finish_render(
                record,
                error=ApiError(
                    code="media_unavailable",
                    message=_LOCAL_FILE_MISSING,
                    details={"reason": "local_file_missing"},
                ),
            )
            return
        folder = self.output_dir / "exports"
        target = folder / f"{record.render_id}.mp4"
        spec = ClipSpec(
            source=source,
            interval=Interval(request.start_s, request.end_s),
            output=target,
            layout=request.layout,
            captions=request.captions,
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
        if not target.is_file():  # a renderer that returned without writing is a bug
            raise RuntimeError(f"the renderer wrote no file for {record.render_id}")
        self._finish_render(record, file=target)


_INTERNAL: Final = "Something went wrong on the server. Try again."
_LOCAL_FILE_MISSING: Final = (
    "The video file for this analysis is missing or can't be read. Check that it's still there, "
    "or analyze the VOD again."
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


def _within(export: ExportAvailability, start_s: float, end_s: float) -> bool:
    assert export.min_start_s is not None
    assert export.max_end_s is not None
    assert export.max_duration_s is not None
    return (
        export.min_start_s <= start_s < end_s <= export.max_end_s
        and end_s - start_s <= export.max_duration_s
    )


def _bounds_message(export: ExportAvailability, start_s: float, end_s: float, what: str) -> str:
    if end_s - start_s > (export.max_duration_s or 0) and start_s < end_s:
        seconds = f"{export.max_duration_s:g}"
        tail = (
            "Shorten the selection and try again."
            if what == "Clips"
            else "Shorten it and try again."
        )
        return f"{what} can be at most {seconds} seconds long. {tail}"
    if end_s <= start_s:
        return "The end must come after the start. Adjust the selection and try again."
    return "The selection goes past the start or end of the video. Adjust it and try again."


def _ordered(
    segments: list[HighlightsSegment],
    candidates: dict[str, Candidate],
    order: Literal["chronological", "rank"],
) -> list[HighlightsSegment]:
    if order == "chronological":
        return sorted(segments, key=lambda s: (s.start_s, s.end_s, candidates[s.candidate_id].rank))
    return sorted(segments, key=lambda s: (candidates[s.candidate_id].rank, s.start_s))
