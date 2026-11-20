"""Join an analysis result's top moments into one 16:9 highlights video (#71).

`render_highlights` is the second product output's renderer (docs/architecture/api-contract.md
§ 7): the chosen segments, each cut exactly like a clip export, kept at the source's own
framing in a 1920x1080 frame (a source that isn't 16:9 is scaled to fit and letterboxed),
joined with a 0.5 s fade through black — the last 0.25 s of one segment fades out and the
first 0.25 s of the next fades in, picture and sound together, so the video lasts exactly
the sum of its segments. Optional captions are burned in per segment with the same
captioning as clip exports (#21). The result is one H.264/AAC MP4, written atomically.

`resolve_segments` applies § 7.1 to a saved `AnalysisResult`: the explicit segments, or the
top `count` candidates' own bounds, ordered chronologically (stream order) or by rank. The
API's highlights endpoints (#72) will resolve the same way.

Every FFmpeg call goes through `autoclipper.ffmpeg`, and each segment is cut on the timeline
analysis uses (`autoclipper.export.timeline`), like every export.
"""

import contextlib
import logging
import os
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from autoclipper.analysis import AnalysisResult
from autoclipper.config import get_settings
from autoclipper.export.captions import (
    Transcriber,
    ass_subtitles,
    caption_lines_for,
    subtitles_filter,
    transcriber_for,
)
from autoclipper.export.cut import Interval, IntervalTooShortError, plan_cut
from autoclipper.export.encode import (
    OUTPUT_AUDIO_CODEC,
    OUTPUT_VIDEO_CODEC,
    EncodeSettings,
    UnsupportedSourceError,
    build_export_command,
    encode_timeout_s,
    publish_output,
)
from autoclipper.export.errors import ExportError
from autoclipper.export.reframe import PixelRect, ReframePlan
from autoclipper.export.seek import (
    covered_span,
    find_keyframe,
    probe_stream_ends,
    probe_stream_times,
    seek_point,
)
from autoclipper.export.timeline import timeline_window
from autoclipper.ffmpeg import (
    FFmpegOutputError,
    MediaInfo,
    Runner,
    VideoStream,
    ffmpeg_command,
    probe,
    run_command,
)

log = logging.getLogger(__name__)

type Order = Literal["chronological", "rank"]
"""How the segments appear in the video (api-contract.md § 7.1)."""

type Stage = Literal["obtaining_media", "transcribing", "rendering"]
"""The highlights pipeline's stages, as the API reports them (api-contract.md § 7.3)."""

HIGHLIGHTS_WIDTH: Final = 1920
HIGHLIGHTS_HEIGHT: Final = 1080
"""The 16:9 output frame: the source's own framing, letterboxed when it isn't 16:9."""

MAX_SEGMENT_DURATION_S: Final = 90.0
"""The longest one segment may be (the contract's export `max_duration_s`)."""

MAX_HIGHLIGHTS_SEGMENTS: Final = 20
"""How many segments one highlights video may have (api-contract.md § 7.1)."""

MAX_TOTAL_DURATION_S: Final = 900.0
"""The longest the whole video may be: 15 minutes (api-contract.md § 7.1)."""

FADE_S: Final = 0.5
"""The fade through black between segments: 0.25 s out, then 0.25 s in, adding no time."""

# Joining is a stream copy, so it needs far less time per output second than encoding.
CONCAT_TIMEOUT_BASE_S: Final = 120.0
CONCAT_TIMEOUT_PER_OUTPUT_S: Final = 2.0


class InvalidHighlightsBoundsError(ExportError, ValueError):
    """A segment or the total is out of bounds (the contract's ``invalid_highlights_bounds``).

    Attributes:
        details: The first offending segment's 0-based index in the ordered list (None when
            the total or an empty segment list is the problem) and the limits.
    """

    def __init__(self, message: str, details: Mapping[str, object]) -> None:
        super().__init__(message)
        self.details = dict(details)


@dataclass(frozen=True, slots=True)
class Segment:
    """One moment of the video: a trimmed interval and the candidate it came from."""

    candidate_id: str
    start_s: float
    end_s: float


@dataclass(frozen=True, slots=True)
class HighlightsRequest:
    """What to render.

    Attributes:
        source: The VOD's video file; it must start at VOD time 0 (times are file times).
        segments: The resolved segments, in the order they appear in the video.
        output: Where to write the MP4. Its directory is created if needed.
        order: As resolved, recorded in the result.
        captions: Burn in captions of each segment's speech.
        overwrite: Replace an existing file at `output`.
    """

    source: Path
    segments: Sequence[Segment]
    output: Path
    order: Order = "chronological"
    captions: bool = False
    overwrite: bool = False


@dataclass(frozen=True, slots=True)
class HighlightsResult:
    """A finished highlights video. Size and codecs are read back from the output file.

    Attributes:
        output: The MP4 that was written.
        segments: The segments that were rendered, in video order.
        order: As requested.
        captions: Whether captions were burned in.
        duration_s: The output's duration, in seconds, from `probe`.
        width: Output frame width in pixels.
        height: Output frame height in pixels.
        video_codec: Output video codec name (``"h264"``).
        audio_codec: Output audio codec name (``"aac"``).
    """

    output: Path
    segments: tuple[Segment, ...]
    order: Order
    captions: bool
    duration_s: float
    width: int
    height: int
    video_codec: str
    audio_codec: str


def resolve_segments(
    result: AnalysisResult,
    *,
    count: int,
    segments: Sequence[Segment] | None,
    order: Order,
) -> list[Segment]:
    """§ 7.1's segments: explicit ones, or the top candidates', then ordered.

    The explicit `segments` are validated against the result (each candidate must exist);
    None means the top `count` candidates' own bounds.

    Ordering: `chronological` by ``start_s``, then ``end_s``, then the candidate's rank;
    `rank` by the candidate's rank, then ``start_s``. Segments may overlap or repeat a
    candidate; each is rendered as given.

    Raises:
        InvalidHighlightsBoundsError: `segments` is None and the result has no candidates.
        ValueError: An explicit segment's ``candidate_id`` isn't in the result.
    """
    if segments is None:
        chosen = [
            Segment(candidate.candidate_id, candidate.start_s, candidate.end_s)
            for candidate in result.candidates[:count]
        ]
        if not chosen:
            raise InvalidHighlightsBoundsError(
                "this analysis has no candidates to make a highlights video from",
                {"segment_index": None},
            )
    else:
        if not 1 <= len(segments) <= MAX_HIGHLIGHTS_SEGMENTS:  # section 7.1's 1-20 items
            raise InvalidHighlightsBoundsError(
                f"a highlights video needs 1-{MAX_HIGHLIGHTS_SEGMENTS} segments; "
                f"{len(segments)} were given",
                {"segment_index": None},
            )
        chosen = list(segments)
        known = {candidate.candidate_id for candidate in result.candidates}
        for segment in chosen:
            if segment.candidate_id not in known:
                raise ValueError(f"no candidate {segment.candidate_id} in the analysis result")
    ranks = {candidate.candidate_id: candidate.rank for candidate in result.candidates}
    if order == "rank":
        # § 7.1: rank, then start_s. Python's sort is stable, so equal keys keep the
        # supplied order.
        ordered = sorted(chosen, key=lambda s: (ranks[s.candidate_id], s.start_s))
    else:
        # § 7.1: start_s, then end_s, then the candidate's rank.
        ordered = sorted(chosen, key=lambda s: (s.start_s, s.end_s, ranks[s.candidate_id]))
    # Every segment must satisfy its candidate's export bounds, with the whole VOD as the
    # bounds here (§ 7.1, § 4.6); the details name the first offender in the ordered list.
    for index, segment in enumerate(ordered):
        if not 0.0 <= segment.start_s < segment.end_s <= result.vod.duration_s:
            raise InvalidHighlightsBoundsError(
                f"segment {index} ({segment.candidate_id}) must be inside the VOD's "
                f"0-{result.vod.duration_s:g} s; it is {segment.start_s:g}-{segment.end_s:g} s",
                {
                    "segment_index": index,
                    "max_duration_s": MAX_SEGMENT_DURATION_S,
                    "max_total_duration_s": MAX_TOTAL_DURATION_S,
                },
            )
    return ordered


def render_highlights(
    request: HighlightsRequest,
    *,
    transcriber: Transcriber | None = None,
    on_stage: Callable[[Stage], None] | None = None,
    settings: EncodeSettings | None = None,
    runner: Runner | None = None,
) -> HighlightsResult:
    """Render the segments as one 1920x1080 H.264/AAC MP4 and write it to `request.output`.

    The video lasts the sum of the segments (within one output frame plus one AAC frame per
    segment), joined with a 0.5 s fade through black between neighbours. Captions are
    burned in per segment when `request.captions`.

    Args:
        request: What to render; the segments are resolved and ordered already.
        transcriber: For captions; defaults to faster-whisper with the configured model.
        on_stage: Called with ``obtaining_media`` once the source is opened,
            ``transcribing`` before the first captioned segment and ``rendering`` before the
            join (api-contract.md § 7.3).
        settings: Encoder settings.
        runner: Executes FFmpeg and FFprobe. Defaults to `run_subprocess`; tests pass a fake.

    Raises:
        FileNotFoundError: `request.source` isn't a file.
        FileExistsError: `request.output` exists and `request.overwrite` is False.
        InvalidHighlightsBoundsError: No segments, a segment longer than
            `MAX_SEGMENT_DURATION_S`, a total longer than `MAX_TOTAL_DURATION_S`, or a
            segment the media doesn't cover.
        CaptioningError: Captions were asked for and failed (nothing is written).
        ExportError, FFmpegError: As raised by the per-segment encode or the join.

    Returns:
        The written video.
    """
    encode = EncodeSettings() if settings is None else settings
    output = request.output.absolute()
    _check_bounds(request.segments)
    if not request.source.is_file():
        raise FileNotFoundError(f"video file not found: {request.source}")
    if output.exists() and not request.overwrite:
        raise FileExistsError(f"highlights output already exists (pass overwrite): {output}")

    def report(stage: Stage) -> None:
        if on_stage is not None:
            on_stage(stage)

    log.info(
        "highlights start source=%s segments=%d order=%s captions=%s out=%s",
        request.source,
        len(request.segments),
        request.order,
        request.captions,
        output,
    )
    report("obtaining_media")
    if request.captions:
        report("transcribing")
        if transcriber is None:
            transcriber = transcriber_for(get_settings().whisper_model)
    with tempfile.TemporaryDirectory(prefix="autoclipper-highlights-") as work_name:
        work = Path(work_name)
        pieces: list[Path] = []
        planned_s = 0.0
        for index in range(len(request.segments)):
            piece, duration_s = _render_segment(
                request, index, work, transcriber=transcriber, settings=encode, runner=runner
            )
            pieces.append(piece)
            planned_s += duration_s
        report("rendering")
        video = _join(
            pieces,
            output,
            planned_s,
            len(request.segments),
            encode,
            overwrite=request.overwrite,
            runner=runner,
        )
    return HighlightsResult(
        output=output,
        segments=tuple(request.segments),
        order=request.order,
        captions=request.captions,
        duration_s=video.duration_s,
        width=video.video_streams[0].width,
        height=video.video_streams[0].height,
        video_codec=video.video_streams[0].codec_name,
        audio_codec=video.audio_streams[0].codec_name,
    )


# --- Pieces -------------------------------------------------------------------------------------


def _check_bounds(segments: Sequence[Segment]) -> None:
    """§ 7.1's bounds: at least one segment, each within the per-segment maximum."""
    if not segments:
        raise InvalidHighlightsBoundsError(
            "a highlights video needs at least one segment",
            {"segment_index": None, "max_duration_s": MAX_SEGMENT_DURATION_S},
        )
    total_s = 0.0
    for index, segment in enumerate(segments):
        if segment.end_s <= segment.start_s:
            raise InvalidHighlightsBoundsError(
                f"segment {index} ({segment.candidate_id}) ends before it starts",
                {"segment_index": index},
            )
        if segment.end_s - segment.start_s > MAX_SEGMENT_DURATION_S:
            raise InvalidHighlightsBoundsError(
                f"segment {index} ({segment.candidate_id}) is "
                f"{segment.end_s - segment.start_s:g} s long; each moment can be at most "
                f"{MAX_SEGMENT_DURATION_S:g} s. Shorten it and try again.",
                {
                    "segment_index": index,
                    "max_duration_s": MAX_SEGMENT_DURATION_S,
                    "max_total_duration_s": MAX_TOTAL_DURATION_S,
                },
            )
        total_s += segment.end_s - segment.start_s
    if total_s > MAX_TOTAL_DURATION_S:
        raise InvalidHighlightsBoundsError(
            f"the segments run {total_s:g} s together; a highlights video can be at most "
            f"{MAX_TOTAL_DURATION_S:g} s. Drop some moments and try again.",
            {
                "segment_index": None,
                "max_duration_s": MAX_SEGMENT_DURATION_S,
                "max_total_duration_s": MAX_TOTAL_DURATION_S,
            },
        )


def _letterbox_plan(video: VideoStream) -> ReframePlan:
    """The whole source frame, scaled to fit a 1920x1080 frame and letterboxed with black."""
    scale = min(HIGHLIGHTS_WIDTH / video.width, HIGHLIGHTS_HEIGHT / video.height)
    width = min(HIGHLIGHTS_WIDTH, max(2, _even(round(video.width * scale))))
    height = min(HIGHLIGHTS_HEIGHT, max(2, _even(round(video.height * scale))))
    return ReframePlan(
        crop=PixelRect(0, 0, _even(video.width), _even(video.height)),
        scaled_width=width,
        scaled_height=height,
        pad_x=_even((HIGHLIGHTS_WIDTH - width) // 2),
        pad_y=_even((HIGHLIGHTS_HEIGHT - height) // 2),
        output_width=HIGHLIGHTS_WIDTH,
        output_height=HIGHLIGHTS_HEIGHT,
    )


def _render_segment(
    request: HighlightsRequest,
    index: int,
    work_dir: Path,
    *,
    transcriber: Transcriber | None,
    settings: EncodeSettings,
    runner: Runner | None,
) -> tuple[Path, float]:
    """Cut one segment to a 1920x1080 piece with its share of the fade, and caption it.

    Returns:
        The piece's path and its planned duration (the actual cut interval's length).
    """
    n = len(request.segments)
    segment = request.segments[index]
    interval = Interval(segment.start_s, segment.end_s)
    piece = work_dir / f"segment-{index:03d}.mp4"
    # Times are on the timeline analysis uses. For a source whose timestamps restart, the
    # cut comes from a corrected copy of the part around the interval.
    with timeline_window(request.source, interval, runner=runner) as window:
        local = window.local(interval)
        info = probe(window.path, runner=runner)
        if not info.video_streams:
            raise UnsupportedSourceError(f"{request.source} has no video stream")
        video = info.video_streams[0]
        audio = info.audio_streams[0] if info.audio_streams else None
        used_streams = [video.index] if audio is None else [video.index, audio.index]
        source_times = probe_stream_times(window.path, runner=runner)
        stream_ends_s = probe_stream_ends(
            window.path,
            used_streams,
            container_start_s=source_times.container_start_s,
            duration_hint_s=info.duration_s,
            runner=runner,
        )
        media_start_s, media_end_s = covered_span(
            source_times, used_streams, info.duration_s, stream_ends_s
        )
        try:
            cut = plan_cut(local, media_end_s, media_start_s=media_start_s)
        except IntervalTooShortError as exc:
            raise InvalidHighlightsBoundsError(
                f"segment {index} ({segment.candidate_id}) isn't covered by the video: {exc}",
                {"segment_index": index},
            ) from None
        actual = window.corrected(cut.actual)
        if actual != interval:  # no padding, and the media covers the whole interval
            raise InvalidHighlightsBoundsError(
                f"segment {index} ({segment.candidate_id}) covers only "
                f"{actual.start_s:.3f}-{actual.end_s:.3f} s of the requested "
                f"{segment.start_s:.3f}-{segment.end_s:.3f} s",
                {"segment_index": index},
            ) from None

        reframe = _letterbox_plan(video)
        video_filters: list[str] = []
        audio_filters: list[str] = []
        if request.captions:
            assert transcriber is not None
            # The captions read the source on the corrected timeline, like the cut itself,
            # so the words land where the segment's picture shows them. They are burned in
            # BEFORE the fades, so a caption that spans a join fades out with the picture
            # instead of staying bright over the black transition.
            lines = caption_lines_for(request.source, actual, transcriber, work_dir, runner=runner)
            ass = work_dir / f"captions-{index:03d}.ass"
            ass.write_text(
                ass_subtitles(lines, play_res=(HIGHLIGHTS_WIDTH, HIGHLIGHTS_HEIGHT)),
                encoding="utf-8",
                newline="\n",
            )
            video_filters.append(subtitles_filter(ass))
        fade_out_at = max(0.0, cut.actual.duration_s - FADE_S / 2)
        if index > 0:  # fade in from the previous segment
            video_filters.append(f"fade=t=in:st=0:d={FADE_S / 2:g}")
            audio_filters.append(f"afade=t=in:st=0:d={FADE_S / 2:g}")
        if index < n - 1:  # fade out into the next segment
            video_filters.append(f"fade=t=out:st={fade_out_at:g}:d={FADE_S / 2:g}")
            audio_filters.append(f"afade=t=out:st={fade_out_at:g}:d={FADE_S / 2:g}")

        keyframe_s = find_keyframe(
            window.path,
            video.index,
            cut.actual.start_s,
            container_start_s=source_times.container_start_s,
            runner=runner,
        )
        log.info(
            "highlights segment index=%d candidate_id=%s start_s=%.3f end_s=%.3f "
            "keyframe_s=%.3f captioned=%s",
            index,
            segment.candidate_id,
            actual.start_s,
            actual.end_s,
            keyframe_s,
            request.captions,
        )
        command = build_export_command(
            window.path,
            cut.actual,
            video,
            audio,
            reframe,
            piece,
            seek_s=min(seek_point(keyframe_s), cut.actual.start_s),
            settings=settings,
            video_filters=video_filters,
            audio_filters=audio_filters,
        )
        run_command(command, timeout_s=encode_timeout_s(cut.actual.duration_s), runner=runner)
        _check_piece(probe(piece, runner=runner), cut.actual, settings)
    return piece, cut.actual.duration_s


def _check_piece(info: MediaInfo, planned: Interval, settings: EncodeSettings) -> None:
    """Confirm a segment file is the 1920x1080 H.264/AAC piece it must be to join cleanly."""
    problems: list[str] = []
    videos, audios = info.video_streams, info.audio_streams
    if len(videos) != 1 or len(audios) != 1:
        problems.append(f"expected 1 video and 1 audio stream, got {len(videos)} and {len(audios)}")
    if videos and (
        videos[0].codec_name != OUTPUT_VIDEO_CODEC
        or (videos[0].width, videos[0].height) != (HIGHLIGHTS_WIDTH, HIGHLIGHTS_HEIGHT)
    ):
        problems.append(f"video is {videos[0].codec_name} {videos[0].width}x{videos[0].height}")
    if audios and audios[0].codec_name != OUTPUT_AUDIO_CODEC:
        problems.append(f"audio is {audios[0].codec_name}")
    if abs(info.duration_s - planned.duration_s) > settings.duration_tolerance_s:
        problems.append(f"duration is {info.duration_s:.3f} s, expected {planned.duration_s:.3f} s")
    if problems:
        raise FFmpegOutputError(
            "a highlights segment doesn't match the "
            f"{HIGHLIGHTS_WIDTH}x{HIGHLIGHTS_HEIGHT} {OUTPUT_VIDEO_CODEC}/"
            f"{OUTPUT_AUDIO_CODEC} shape: {'; '.join(problems)}"
        )


def _join(
    pieces: Sequence[Path],
    output: Path,
    planned_s: float,
    n_segments: int,
    settings: EncodeSettings,
    *,
    overwrite: bool,
    runner: Runner | None,
) -> MediaInfo:
    """Stream-copy the pieces into one MP4 and check the joined result."""
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{output.stem}.", suffix=".partial", dir=output.parent
    )
    os.close(fd)
    temp = Path(temp_name)
    # The list file lives next to the pieces and names them by their plain file names, which
    # this code chose: a path with an apostrophe (a Windows user folder) can't be quoted in
    # the ffconcat syntax, but the names never contain one.
    list_path = pieces[0].parent / "segments.ffconcat"
    try:
        list_path.write_text(
            "\n".join(["ffconcat version 1.0", *(f"file '{piece.name}'" for piece in pieces)])
            + "\n",
            encoding="utf-8",
            newline="\n",
        )
        run_command(
            ffmpeg_command(
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                list_path,
                "-c",
                "copy",
                # The temporary file's extension says nothing; name the muxer.
                "-f",
                "mp4",
                "-movflags",
                "+faststart",
                temp,
            ),
            timeout_s=CONCAT_TIMEOUT_BASE_S + CONCAT_TIMEOUT_PER_OUTPUT_S * planned_s,
            runner=runner,
        )
        info = probe(temp, runner=runner)
        problems: list[str] = []
        videos, audios = info.video_streams, info.audio_streams
        if len(videos) != 1 or len(audios) != 1:
            problems.append(
                f"expected 1 video and 1 audio stream, got {len(videos)} and {len(audios)}"
            )
        if videos and (
            videos[0].codec_name != OUTPUT_VIDEO_CODEC
            or (videos[0].width, videos[0].height) != (HIGHLIGHTS_WIDTH, HIGHLIGHTS_HEIGHT)
        ):
            problems.append(f"video is {videos[0].codec_name} {videos[0].width}x{videos[0].height}")
        if audios and audios[0].codec_name != OUTPUT_AUDIO_CODEC:
            problems.append(f"audio is {audios[0].codec_name}")
        # One output frame plus one AAC frame per segment (api-contract.md § 7).
        tolerance_s = n_segments * settings.duration_tolerance_s
        if abs(info.duration_s - planned_s) > tolerance_s:
            problems.append(
                f"duration is {info.duration_s:.3f} s, expected {planned_s:.3f} s "
                f"(tolerance {tolerance_s:.3f} s)"
            )
        if problems:
            raise FFmpegOutputError(
                "the highlights video doesn't match the "
                f"{HIGHLIGHTS_WIDTH}x{HIGHLIGHTS_HEIGHT} {OUTPUT_VIDEO_CODEC}/"
                f"{OUTPUT_AUDIO_CODEC} shape: {'; '.join(problems)}"
            )
        # `overwrite` decides here too: a file that appeared at the output since the check
        # at the start of the render is kept, not silently replaced.
        publish_output(temp, output, overwrite=overwrite)
    except BaseException:
        with contextlib.suppress(OSError):
            temp.unlink(missing_ok=True)
        raise
    finally:
        list_path.unlink(missing_ok=True)
    log.info(
        "highlights done output=%s duration_s=%.3f segments=%d", output, info.duration_s, n_segments
    )
    return info


def _even(value: int) -> int:
    """The largest even number at or below `value` (FFmpeg wants even pixel sizes)."""
    return max(2, value - (value % 2))


__all__ = [
    "FADE_S",
    "HIGHLIGHTS_HEIGHT",
    "HIGHLIGHTS_WIDTH",
    "MAX_HIGHLIGHTS_SEGMENTS",
    "MAX_SEGMENT_DURATION_S",
    "MAX_TOTAL_DURATION_S",
    "HighlightsRequest",
    "HighlightsResult",
    "InvalidHighlightsBoundsError",
    "Order",
    "Segment",
    "Stage",
    "render_highlights",
    "resolve_segments",
]
