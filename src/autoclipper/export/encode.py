"""Export a source interval as a vertical 1080x1920 H.264/AAC MP4.

`export_clip` is the entry point: it probes the source, plans the cut (`autoclipper.export.cut`)
and the layout (`autoclipper.export.reframe`), runs one FFmpeg command built by
`build_export_command`, checks the result with `probe`, and only then moves it to the requested
path. Every FFmpeg call goes through `autoclipper.ffmpeg`, so tests can pass a fake runner.

Encoding decisions (docs/architecture/overview.md §4.6):

- **Accurate cut.** ``-ss`` is an input option, so FFmpeg jumps near the start instead of
  decoding the VOD from the beginning, but it seeks `SEEK_PREROLL_S` before the start. The
  ``trim``/``atrim`` filters then drop everything before the exact start, so a user-trimmed start
  between keyframes is respected. Seeking straight to the start is not enough for every
  container: MPEG-TS has no index, FFmpeg can land after the keyframe that precedes the start,
  and the clip would then begin on a later frame. ``-t`` is an output option, so the clip lasts
  exactly the interval. Output timestamps start at 0 at the actual start.
- **Constant frame rate.** The ``fps`` filter resamples to `EncodeSettings.frame_rate` (30 by
  default) starting at output time 0, so every clip has a fixed frame rate and a frame at time 0
  whatever the source's rate.
- **Duration.** The output lasts the planned interval within `EncodeSettings.duration_tolerance_s`
  (one output frame plus one AAC frame); `export_clip` fails if it doesn't.
- **Audio.** The first audio stream is encoded as stereo AAC. A source without audio gets a
  silent AAC track, so every export has the same H.264/AAC shape; `ExportResult.silent_audio`
  says so.
- **Atomic output.** FFmpeg writes a hidden temporary file next to the output; it is renamed to
  the output path only after the encode succeeded and the probe matched. A failed or interrupted
  export leaves no file at the output path (and no temporary file, unless the process was killed).
"""

import contextlib
import logging
import os
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from autoclipper.export.cut import (
    ContextPadding,
    CutPlan,
    Interval,
    plan_cut,
)
from autoclipper.export.errors import ExportError
from autoclipper.export.reframe import (
    OUTPUT_HEIGHT,
    OUTPUT_WIDTH,
    CenterCrop,
    Layout,
    ReframePlan,
    plan_reframe,
)
from autoclipper.ffmpeg import (
    AudioStream,
    FFmpegCommand,
    FFmpegOutputError,
    MediaInfo,
    Runner,
    VideoStream,
    ffmpeg_command,
    probe,
    run_command,
)

log = logging.getLogger(__name__)

# Codec names as `probe` reports them for a finished export.
OUTPUT_VIDEO_CODEC = "h264"
OUTPUT_AUDIO_CODEC = "aac"

# Encode timeout: a fixed allowance plus this many seconds per second of output. Generous on
# purpose: encode speed depends on the CPU, the preset and the source resolution.
ENCODE_TIMEOUT_BASE_S = 120.0
ENCODE_TIMEOUT_PER_OUTPUT_S = 10.0

# How far before the start FFmpeg seeks; the frames in between are decoded and dropped by the
# trim filters. It must exceed the source's keyframe interval (Twitch asks broadcasters for 2 s)
# so that a keyframe lies between where a seek lands and the start, even in MPEG-TS sources.
SEEK_PREROLL_S = 5.0

# Samples per AAC frame; the audio track's length is a multiple of this.
_AAC_FRAME_SAMPLES = 1024

_X264_PRESETS = frozenset(
    {
        "ultrafast",
        "superfast",
        "veryfast",
        "faster",
        "fast",
        "medium",
        "slow",
        "slower",
        "veryslow",
    }
)


class UnsupportedSourceError(ExportError):
    """The source can't be exported: it has no video stream, or it is the output file."""


@dataclass(frozen=True, slots=True)
class EncodeSettings:
    """How the output is encoded. The defaults suit short social clips on a CPU.

    Attributes:
        frame_rate: Constant output frame rate, in frames per second (1-120).
        preset: libx264 speed/size trade-off, ``"ultrafast"`` to ``"veryslow"``.
        crf: libx264 constant rate factor, 0-51; lower is higher quality and larger.
        audio_bitrate_kbps: AAC bitrate in kbit/s.
        audio_sample_rate_hz: AAC sample rate in Hz.

    Raises:
        ValueError: A value is out of range.
    """

    frame_rate: int = 30
    preset: str = "veryfast"
    crf: int = 20
    audio_bitrate_kbps: int = 160
    audio_sample_rate_hz: int = 48_000

    def __post_init__(self) -> None:
        if not 1 <= self.frame_rate <= 120:
            raise ValueError(f"frame_rate must be 1-120, got {self.frame_rate!r}")
        if self.preset not in _X264_PRESETS:
            raise ValueError(f"preset must be one of {sorted(_X264_PRESETS)}, got {self.preset!r}")
        if not 0 <= self.crf <= 51:
            raise ValueError(f"crf must be 0-51, got {self.crf!r}")
        if self.audio_bitrate_kbps <= 0 or self.audio_sample_rate_hz <= 0:
            raise ValueError("audio bitrate and sample rate must be positive")

    @property
    def duration_tolerance_s(self) -> float:
        """How far the output duration may differ from the planned interval, in seconds.

        One output frame plus one AAC frame: the video track ends on a frame boundary and the
        audio track on an AAC frame boundary.
        """
        return 1 / self.frame_rate + _AAC_FRAME_SAMPLES / self.audio_sample_rate_hz


@dataclass(frozen=True, slots=True)
class ExportRequest:
    """What to export.

    Attributes:
        source: The source media file; it must start at VOD offset 0.
        interval: The trimmed interval in source time (seconds from VOD start). Used as given:
            no chat-lag shift is applied (docs/data/labeling.md §5).
        output: Where to write the MP4. Its directory is created if needed.
        layout: Which part of the frame to show (default `CenterCrop`).
        padding: Context added around `interval` before clamping to the media (default none).
        overwrite: Replace an existing file at `output`. Without it, an existing file is an
            error.
    """

    source: Path
    interval: Interval
    output: Path
    layout: Layout = field(default_factory=CenterCrop)
    padding: ContextPadding = field(default_factory=ContextPadding)
    overwrite: bool = False


@dataclass(frozen=True, slots=True)
class ExportResult:
    """A finished export. Size, codecs and duration are read back from the output file.

    Attributes:
        output: The MP4 that was written.
        cut: The requested interval, the padding, and the actual (padded, clamped) interval.
        layout: The layout used.
        reframe: The pixel geometry used for the layout.
        duration_s: The output's duration, in seconds, from `probe`.
        width: Output frame width in pixels.
        height: Output frame height in pixels.
        video_codec: Output video codec name (``"h264"``).
        audio_codec: Output audio codec name (``"aac"``).
        silent_audio: True when the source had no audio and a silent track was added.
    """

    output: Path
    cut: CutPlan
    layout: Layout
    reframe: ReframePlan
    duration_s: float
    width: int
    height: int
    video_codec: str
    audio_codec: str
    silent_audio: bool

    @property
    def start_s(self) -> float:
        """Source time (seconds from VOD start) of the output's first frame."""
        return self.cut.actual.start_s

    @property
    def end_s(self) -> float:
        """Source time (seconds from VOD start) where the output ends."""
        return self.cut.actual.end_s


def encode_timeout_s(duration_s: float) -> float:
    """The default FFmpeg timeout for an export of `duration_s` seconds."""
    return ENCODE_TIMEOUT_BASE_S + ENCODE_TIMEOUT_PER_OUTPUT_S * duration_s


def build_export_command(
    source: Path,
    interval: Interval,
    video: VideoStream,
    audio: AudioStream | None,
    reframe: ReframePlan,
    output: Path,
    *,
    settings: EncodeSettings | None = None,
    video_filters: Sequence[str] = (),
) -> FFmpegCommand:
    """Build the FFmpeg command that cuts, reframes and encodes one interval.

    Args:
        source: The source media file.
        interval: The actual interval to export, in source seconds (already padded and clamped).
        video: The source video stream to use.
        audio: The source audio stream to use, or None to add a silent track.
        reframe: The layout geometry for `video`'s frame size.
        output: The file FFmpeg writes (an MP4, whatever its extension). Overwritten if it
            exists.
        settings: Encoder settings; defaults to `EncodeSettings()`.
        video_filters: Extra FFmpeg filters applied to the 1080x1920 frame after reframing and
            before encoding, e.g. burned-in subtitles. Each entry is one complete filter, escaped
            as FFmpeg's filtergraph syntax requires. Their timestamps are clip time: 0 is
            `interval.start_s`.

    Returns:
        The command; nothing runs until it is passed to `run_command`.
    """
    encode = EncodeSettings() if settings is None else settings
    # Seek to `seek_s`, where the decoded timestamps restart at 0. The filters then drop `skip`
    # seconds of preroll and shift the rest back to 0, so output time 0 is exactly the start.
    seek_s = max(0.0, interval.start_s - SEEK_PREROLL_S)
    skip = _seconds(interval.start_s - seek_s)
    filters = (
        f"trim=start={skip}",
        f"setpts=PTS-{skip}/TB",
        f"fps=fps={encode.frame_rate}:start_time=0",
        *reframe.filters(),
        *video_filters,
        "format=yuv420p",
    )
    audio_filters = (f"atrim=start={skip}", f"asetpts=PTS-{skip}/TB")
    inputs: list[str | Path] = [
        # Use the stored frame size, which is what `probe` reports and `reframe` is based on.
        "-noautorotate",
        "-accurate_seek",
        "-ss",
        _seconds(seek_s),
        "-i",
        source,
    ]
    audio_map = f"0:{audio.index}" if audio is not None else "1:a:0"
    if audio is None:
        silence = f"anullsrc=channel_layout=stereo:sample_rate={encode.audio_sample_rate_hz}"
        inputs += ["-f", "lavfi", "-i", silence]
    return ffmpeg_command(
        "-y",
        *inputs,
        "-t",
        _seconds(interval.duration_s),
        "-map",
        f"0:{video.index}",
        "-map",
        audio_map,
        "-filter:v",
        ",".join(filters),
        "-filter:a",
        ",".join(audio_filters),
        "-c:v",
        "libx264",
        "-preset",
        encode.preset,
        "-crf",
        str(encode.crf),
        "-c:a",
        "aac",
        "-b:a",
        f"{encode.audio_bitrate_kbps}k",
        "-ar",
        str(encode.audio_sample_rate_hz),
        "-ac",
        "2",
        # Source titles and chapters are not carried over into a shareable clip.
        "-map_metadata",
        "-1",
        "-map_chapters",
        "-1",
        "-movflags",
        "+faststart",
        "-f",
        "mp4",
        output,
    )


def export_clip(
    request: ExportRequest,
    *,
    settings: EncodeSettings | None = None,
    video_filters: Sequence[str] = (),
    runner: Runner | None = None,
    timeout_s: float | None = None,
) -> ExportResult:
    """Cut `request.interval` from the source and write it as a 1080x1920 H.264/AAC MP4.

    Args:
        request: Source, interval, padding, layout and output path.
        settings: Encoder settings; defaults to `EncodeSettings()`.
        video_filters: Extra filters on the reframed frame (see `build_export_command`).
        runner: Executes FFmpeg and FFprobe. Defaults to `run_subprocess`; tests pass a fake.
        timeout_s: Seconds the encode may take; defaults to `encode_timeout_s` of the clip.

    Returns:
        The output path, the actual interval, and what `probe` reads from the output.

    Raises:
        FileNotFoundError: The source file doesn't exist.
        FileExistsError: `request.output` exists and `request.overwrite` is False.
        UnsupportedSourceError: The source has no video stream, or is the output path.
        IntervalTooShortError: Too little of the interval lies inside the media.
        InvalidLayoutError: The layout's region is too small for the source.
        FFmpegError: FFmpeg or FFprobe failed (`FFmpegExitError`, `FFmpegTimeoutError`, ...),
            or the output didn't match (`FFmpegOutputError`). The output path is left as it was.
    """
    encode = EncodeSettings() if settings is None else settings
    output = request.output.absolute()
    if output.exists() and not request.overwrite:
        raise FileExistsError(f"export output already exists (pass overwrite): {output}")
    if request.source.absolute().resolve() == output.resolve():
        raise UnsupportedSourceError(f"the output path is the source file: {output}")

    info = probe(request.source, runner=runner)
    video, audio = _pick_streams(info)
    cut = plan_cut(request.interval, info.duration_s, padding=request.padding)
    reframe = plan_reframe(request.layout, video.width, video.height)
    log.info(
        "export start source=%s start_s=%.3f end_s=%.3f layout=%s silent_audio=%s",
        request.source,
        cut.actual.start_s,
        cut.actual.end_s,
        request.layout.name,
        audio is None,
    )

    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{output.stem}.", suffix=".partial", dir=output.parent
    )
    os.close(fd)
    temp = Path(temp_name)
    try:
        command = build_export_command(
            request.source,
            cut.actual,
            video,
            audio,
            reframe,
            temp,
            settings=encode,
            video_filters=video_filters,
        )
        limit_s = encode_timeout_s(cut.actual.duration_s) if timeout_s is None else timeout_s
        run_command(command, timeout_s=limit_s, runner=runner)
        result = _check_output(probe(temp, runner=runner), cut, encode)
        temp.replace(output)
    except BaseException:
        # Keep the original error even if the temporary file can't be removed.
        with contextlib.suppress(OSError):
            temp.unlink(missing_ok=True)
        raise

    log.info("export done output=%s duration_s=%.3f", output, result.duration_s)
    return ExportResult(
        output=output,
        cut=cut,
        layout=request.layout,
        reframe=reframe,
        duration_s=result.duration_s,
        width=result.width,
        height=result.height,
        video_codec=result.video_codec,
        audio_codec=result.audio_codec,
        silent_audio=audio is None,
    )


def _seconds(value: float) -> str:
    # Microsecond precision, FFmpeg's internal time base; never scientific notation.
    return f"{value:.6f}"


def _pick_streams(info: MediaInfo) -> tuple[VideoStream, AudioStream | None]:
    if not info.video_streams:
        raise UnsupportedSourceError(f"the source has no video stream: {info.path}")
    audio = info.audio_streams[0] if info.audio_streams else None
    return info.video_streams[0], audio


@dataclass(frozen=True, slots=True)
class _OutputCheck:
    duration_s: float
    width: int
    height: int
    video_codec: str
    audio_codec: str


def _check_output(info: MediaInfo, cut: CutPlan, settings: EncodeSettings) -> _OutputCheck:
    """Confirm the encoded file is what an export promises, or raise FFmpegOutputError."""
    problems: list[str] = []
    videos, audios = info.video_streams, info.audio_streams
    if len(videos) != 1 or len(audios) != 1 or len(info.streams) != 2:
        problems.append(
            f"expected 1 video and 1 audio stream, got {len(videos)} video, {len(audios)} audio "
            f"and {len(info.streams) - len(videos) - len(audios)} other"
        )
    if videos:
        v = videos[0]
        if v.codec_name != OUTPUT_VIDEO_CODEC or (v.width, v.height) != (
            OUTPUT_WIDTH,
            OUTPUT_HEIGHT,
        ):
            problems.append(f"video is {v.codec_name} {v.width}x{v.height}")
    if audios and audios[0].codec_name != OUTPUT_AUDIO_CODEC:
        problems.append(f"audio is {audios[0].codec_name}")
    expected_s = cut.actual.duration_s
    if abs(info.duration_s - expected_s) > settings.duration_tolerance_s:
        problems.append(
            f"duration is {info.duration_s:.3f} s, expected {expected_s:.3f} s "
            f"(tolerance {settings.duration_tolerance_s:.3f} s)"
        )
    if problems:
        raise FFmpegOutputError(
            f"the export doesn't match a {OUTPUT_WIDTH}x{OUTPUT_HEIGHT} {OUTPUT_VIDEO_CODEC}/"
            f"{OUTPUT_AUDIO_CODEC} clip: {'; '.join(problems)}"
        )
    return _OutputCheck(
        duration_s=info.duration_s,
        width=videos[0].width,
        height=videos[0].height,
        video_codec=videos[0].codec_name,
        audio_codec=audios[0].codec_name,
    )
