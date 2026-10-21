"""Find where decoding must start, and read stream start times and durations.

An export must begin at exactly its start frame. FFmpeg can only start decoding video at a
keyframe, and a seek doesn't always land before the keyframe that precedes the start: MPEG-TS has
no index, and a source's keyframes may be further apart than any fixed preroll. So the encoder
asks `find_keyframe` for the last video keyframe at or before the start (read from the packet
list with ffprobe, without decoding), seeks `SEEK_MARGIN_S` before it, and trims to the start.

`probe_stream_times` reads the container start time (FFmpeg's ``-ss`` and the times here count from
it) and each stream's start and duration, which `autoclipper.ffmpeg.probe` doesn't report. The
encoder uses it to confirm that every output stream covers the whole clip. Durations in a source's
metadata are missing (MKV) or estimated (MPEG-TS), so `probe_stream_ends` reads where each stream
really ends from its last packets, and `covered_span` combines both into the span an export can
use.
"""

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from autoclipper.export.errors import ExportError
from autoclipper.ffmpeg import FFmpegOutputError, Runner, ffprobe_command, run_command

# The first window searched for a keyframe ends at the start and reaches this far back. Each
# further window is `_WINDOW_GROWTH` times longer, until the last one starts at the beginning of
# the file. Twitch asks for a keyframe every 2 s, so one short window normally suffices.
KEYFRAME_SEARCH_WINDOW_S = 10.0
_WINDOW_GROWTH = 4.0

# The search reads packets this far past the start: packets are stored in decoding order, and a
# keyframe's packet can come after packets of frames shown before it.
_READ_PAST_START_S = 1.0

# How far before the keyframe the encoder seeks. A seek lands on data at or before its target, so
# this keeps the keyframe's packet, and audio stored a little ahead of it, after the landing point.
SEEK_MARGIN_S = 1.0

# Times are compared with microsecond slack, FFmpeg's internal time base.
_TIME_EPSILON_S = 1e-6

# The first read for stream ends covers this much before the end the container reports, to the
# end of the file. It widens like the keyframe search when a stream has no packets in it (the
# stream ended earlier, or the reported duration is off), finally to the whole file.
END_SEARCH_WINDOW_S = 10.0

# ffprobe only reads headers or packets (no decoding), but a wide packet read on a long VOD
# reads a lot of data: allow a base time plus this much per second of media read.
PACKET_PROBE_TIMEOUT_BASE_S = 60.0
PACKET_PROBE_TIMEOUT_PER_S = 0.1


class KeyframeNotFoundError(ExportError):
    """The source has no video keyframe at or before the start, so the start can't be decoded."""


@dataclass(frozen=True, slots=True)
class StreamSpan:
    """When a stream starts and how long it lasts, in seconds from the container start.

    Attributes:
        index: The stream's index in the file.
        codec_type: ``"video"``, ``"audio"``, ...
        start_s: First timestamp, or None when the container doesn't say.
        duration_s: Duration, or None when the container doesn't say.
    """

    index: int
    codec_type: str
    start_s: float | None
    duration_s: float | None


@dataclass(frozen=True, slots=True)
class StreamTimes:
    """Container start time and per-stream spans of a media file.

    Attributes:
        container_start_s: The container's first timestamp (0 when it doesn't report one), in
            the file's own clock. ``-ss`` positions and `StreamSpan` times count from it.
        streams: Every stream, in file order.
    """

    container_start_s: float
    streams: tuple[StreamSpan, ...]


def covered_span(
    times: StreamTimes,
    stream_indexes: Sequence[int],
    container_duration_s: float,
    packet_ends_s: Mapping[int, float] | None = None,
) -> tuple[float, float]:
    """The span, in seconds from the container start, that all the given streams cover.

    Starts where the last of them starts and ends where the first of them ends. A stream's end is
    where its last packet ends (`packet_ends_s`, from `probe_stream_ends`) when known; otherwise
    its start plus its metadata duration, or `container_duration_s` when it reports none, never
    after `container_duration_s`. A stream that doesn't report its start counts as starting at 0;
    a negative start (audio priming) counts as 0 too.

    Args:
        times: From `probe_stream_times`.
        stream_indexes: The streams an export uses (its video and, if any, its audio stream).
        container_duration_s: The container duration (`autoclipper.ffmpeg.MediaInfo.duration_s`).
        packet_ends_s: Measured stream ends by stream index, in seconds from the container start.

    Returns:
        ``(start_s, end_s)``; `end_s` may not be after `start_s` if the streams don't overlap.
    """
    spans = {span.index: span for span in times.streams}
    measured = {} if packet_ends_s is None else packet_ends_s
    start_s = 0.0
    ends_s: list[float] = []
    for index in stream_indexes:
        span = spans.get(index)
        stream_start_s = 0.0 if span is None or span.start_s is None else span.start_s
        start_s = max(start_s, stream_start_s)
        if index in measured:
            ends_s.append(measured[index])
        elif span is not None and span.duration_s is not None:
            ends_s.append(min(container_duration_s, stream_start_s + span.duration_s))
        else:
            ends_s.append(container_duration_s)
    return start_s, min(ends_s, default=container_duration_s)


def probe_stream_ends(
    source: Path,
    stream_indexes: Sequence[int],
    *,
    container_start_s: float,
    duration_hint_s: float,
    runner: Runner | None = None,
) -> dict[int, float]:
    """Read where each stream's last packet ends, in seconds from the container start.

    A stream ends at the largest presentation time plus duration of its packets. The packets are
    read from `END_SEARCH_WINDOW_S` before `duration_hint_s` to the end of the file; the window
    widens backwards until every stream has packets in it, finally to the whole file. So the read
    stays short on a multi-hour VOD, and a wrong hint only costs more reading.

    Args:
        source: The media file.
        stream_indexes: The streams to measure.
        container_start_s: The container's start time (`StreamTimes.container_start_s`).
        duration_hint_s: Where the file probably ends, in seconds from the container start
            (the container duration).
        runner: Executes ffprobe. Defaults to `run_subprocess`; tests pass a fake.

    Returns:
        The end of each stream that has packets. A stream without any is left out; callers fall
        back to its metadata.

    Raises:
        FFmpegError: ffprobe failed or printed something unexpected.
    """
    wanted = frozenset(stream_indexes)
    window_s = END_SEARCH_WINDOW_S
    while True:
        from_s = duration_hint_s - window_s
        # Without an interval, ffprobe reads the whole file instead of seeking.
        interval = ("-read_intervals", f"{_seconds(container_start_s + from_s)}%")
        command = ffprobe_command(
            *(interval if from_s > 0 else ()),
            "-print_format",
            "json",
            "-show_entries",
            "packet=stream_index,pts_time,duration_time",
            source,
        )
        timeout_s = PACKET_PROBE_TIMEOUT_BASE_S + PACKET_PROBE_TIMEOUT_PER_S * window_s
        result = run_command(command, timeout_s=timeout_s, runner=runner)
        # Rounded to microseconds, FFmpeg's time base, which the subtraction would blur.
        ends = {
            index: round(end_s - container_start_s, 6)
            for index, end_s in _packet_ends(result.stdout, source).items()
            if index in wanted
        }
        if wanted <= ends.keys() or from_s <= 0:
            return ends
        window_s *= _WINDOW_GROWTH


def probe_stream_times(
    path: Path, *, runner: Runner | None = None, timeout_s: float = PACKET_PROBE_TIMEOUT_BASE_S
) -> StreamTimes:
    """Read the container start time and each stream's start and duration with ffprobe.

    Raises:
        FFmpegNotFoundError, FFmpegTimeoutError, FFmpegExitError: As for `run_command`.
        FFmpegOutputError: ffprobe's output isn't the expected JSON.
    """
    command = ffprobe_command(
        "-print_format",
        "json",
        "-show_entries",
        "format=start_time:stream=index,codec_type,start_time,duration",
        path,
    )
    root = _json_object(run_command(command, timeout_s=timeout_s, runner=runner).stdout, path)
    container = root.get("format")
    container_start_s = _optional_seconds(
        container.get("start_time") if isinstance(container, dict) else None, path
    )
    origin_s = 0.0 if container_start_s is None else container_start_s
    raw_streams = root.get("streams")
    if not isinstance(raw_streams, list):
        raise FFmpegOutputError(f"unexpected ffprobe output for {path}: no stream list")
    streams: list[StreamSpan] = []
    for raw in raw_streams:
        if not isinstance(raw, dict) or not isinstance(raw.get("index"), int):
            raise FFmpegOutputError(f"unexpected ffprobe output for {path}: stream {raw!r}")
        start_s = _optional_seconds(raw.get("start_time"), path)
        streams.append(
            StreamSpan(
                index=raw["index"],
                codec_type=str(raw.get("codec_type", "")),
                start_s=None if start_s is None else start_s - origin_s,
                duration_s=_optional_seconds(raw.get("duration"), path),
            )
        )
    return StreamTimes(container_start_s=origin_s, streams=tuple(streams))


def find_keyframe(
    source: Path,
    video_index: int,
    start_s: float,
    *,
    container_start_s: float,
    runner: Runner | None = None,
) -> float:
    """Return the time of the last video keyframe at or before `start_s`.

    Reads the packet list (not the frames) of the video stream in a window that ends just after
    `start_s`, widening it backwards until a keyframe turns up, finally from the beginning.

    When the video stream begins after `start_s` (in MPEG-TS the audio often starts a few
    milliseconds earlier), there is no keyframe before it; then the first keyframe, if it lies
    within a second after `start_s`, is returned. Decoding from it is the best possible, and the
    encoder's output check decides whether the clip still starts close enough to `start_s`.

    Args:
        source: The media file.
        video_index: Index of the video stream to search.
        start_s: Seconds from the container start.
        container_start_s: The container's start time (`StreamTimes.container_start_s`); packet
            times are converted with it.
        runner: Executes ffprobe. Defaults to `run_subprocess`; tests pass a fake.

    Returns:
        The keyframe's presentation time, in seconds from the container start.

    Raises:
        KeyframeNotFoundError: No keyframe at or before `start_s` (or within a second after it)
            in the whole file.
        FFmpegError: ffprobe failed or printed something unexpected.
    """
    window_s = KEYFRAME_SEARCH_WINDOW_S
    end = _seconds(container_start_s + start_s + _READ_PAST_START_S)
    while True:
        from_s = max(0.0, start_s - window_s)
        # Without a start, ffprobe reads from the beginning of the file instead of seeking.
        interval = f"{_seconds(container_start_s + from_s)}%{end}" if from_s > 0 else f"%{end}"
        command = ffprobe_command(
            "-select_streams",
            str(video_index),
            "-read_intervals",
            interval,
            "-print_format",
            "json",
            "-show_entries",
            "packet=pts_time,flags",
            source,
        )
        timeout_s = PACKET_PROBE_TIMEOUT_BASE_S + PACKET_PROBE_TIMEOUT_PER_S * (start_s - from_s)
        result = run_command(command, timeout_s=timeout_s, runner=runner)
        keyframes = [t - container_start_s for t in _keyframe_times(result.stdout, source)]
        before = [t for t in keyframes if t <= start_s + _TIME_EPSILON_S]
        if before:
            return max(before)
        if from_s == 0.0:
            if keyframes:
                return min(keyframes)  # the video starts after `start_s`
            raise KeyframeNotFoundError(
                f"the video stream of {source} has no keyframe at or before {start_s:.3f} s, so "
                "decoding can't start there"
            )
        window_s *= _WINDOW_GROWTH


def seek_point(keyframe_s: float) -> float:
    """Where the encoder seeks to decode from the keyframe at `keyframe_s` (never before 0)."""
    return max(0.0, keyframe_s - SEEK_MARGIN_S)


def _keyframe_times(stdout: bytes, source: Path) -> list[float]:
    packets = _json_object(stdout, source).get("packets", [])
    if not isinstance(packets, list):
        raise FFmpegOutputError(f"unexpected ffprobe output for {source}: no packet list")
    times: list[float] = []
    for packet in packets:
        if not isinstance(packet, dict):
            raise FFmpegOutputError(f"unexpected ffprobe output for {source}: packet {packet!r}")
        flags = packet.get("flags")
        time_s = _optional_seconds(packet.get("pts_time"), source)
        if isinstance(flags, str) and flags.startswith("K") and time_s is not None:
            times.append(time_s)
    return times


def _packet_ends(stdout: bytes, source: Path) -> dict[int, float]:
    """The largest pts + duration of the packets of each stream, in the file's own clock."""
    packets = _json_object(stdout, source).get("packets", [])
    if not isinstance(packets, list):
        raise FFmpegOutputError(f"unexpected ffprobe output for {source}: no packet list")
    ends: dict[int, float] = {}
    for packet in packets:
        if not isinstance(packet, dict) or not isinstance(packet.get("stream_index"), int):
            raise FFmpegOutputError(f"unexpected ffprobe output for {source}: packet {packet!r}")
        time_s = _optional_seconds(packet.get("pts_time"), source)
        if time_s is None:
            continue
        duration_s = _optional_seconds(packet.get("duration_time"), source) or 0.0
        index: int = packet["stream_index"]
        ends[index] = max(ends.get(index, -math.inf), time_s + duration_s)
    return ends


def _json_object(stdout: bytes, path: Path) -> dict[str, object]:
    try:
        data: object = json.loads(stdout.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        raise FFmpegOutputError(f"unexpected ffprobe output for {path}: {exc}") from None
    if not isinstance(data, dict):
        raise FFmpegOutputError(f"unexpected ffprobe output for {path}: not a JSON object")
    return data


def _optional_seconds(value: object, path: Path) -> float | None:
    # ffprobe leaves unknown times out, or prints "N/A"; times are JSON strings.
    if value is None or value == "N/A":
        return None
    try:
        seconds = float(value) if isinstance(value, str | int | float) else math.nan
    except ValueError:
        seconds = math.nan
    if not math.isfinite(seconds):
        raise FFmpegOutputError(f"unexpected ffprobe output for {path}: time {value!r}")
    return seconds


def _seconds(value: float) -> str:
    return f"{value:.6f}"
