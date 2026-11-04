"""Put export on the same timeline as analysis when a source's timestamps restart.

Analysis reads a VOD's audio the way FFmpeg reads a stream (`autoclipper.ingest.media`): in
formats whose timestamps may jump (MPEG-TS, MPEG-PS, FLV), timestamps that go back, such as two
recordings joined byte for byte or an HLS discontinuity, are moved onto the end of what came
before (`autoclipper.ffmpeg.TIMELINE_OPTIONS`). So second t of the normalized audio is FFmpeg's
corrected time t, counted from the container start. Seeking and the packet times ffprobe reports
use the raw timestamps instead, which after a reset repeat earlier values: a raw time then names
two places in the file, and the span after the reset can't be reached.

`timeline_window` resolves this for one interval. `read_reset_timeline` finds the resets: the
raw packet list shows where each stream's timestamps jump back, and an audio-only read with
FFmpeg (exactly as normalization reads it) reports the offset it adds at each one. That places
every video keyframe on the corrected timeline. The window is a stream copy (no re-encode) of
the source from the keyframe before the interval, written as Matroska with FFmpeg's corrected
timestamps, plus the shift that maps its own times onto the corrected timeline. Export and
caption extraction cut from the window, so they see what analysis saw.

Sources whose timestamps never jump back, and formats FFmpeg never corrects (MP4, Matroska,
WAV), are used as they are, at no extra cost beyond one check per file (kept for the process).
For a source with resets the check reads the whole file once (packets only, no decoding).
"""

import bisect
import contextlib
import logging
import re
import tempfile
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from autoclipper.export.cut import Interval
from autoclipper.export.errors import ExportError
from autoclipper.ffmpeg import (
    TIMELINE_OPTIONS,
    FFmpegOutputError,
    Runner,
    ffmpeg_command,
    ffprobe_command,
    probe,
    run_command,
)

log = logging.getLogger(__name__)

# Formats FFmpeg marks as having timestamp discontinuities (AVFMT_TS_DISCONT); only these are
# corrected, so only these can differ from their raw timestamps.
_DISCONTINUOUS_FORMATS: Final = frozenset({"mpegts", "mpegtsraw", "mpeg", "flv"})

# FFmpeg treats a timestamp more than this far before the previous one as a reset.
_BACKWARD_JUMP_S: Final = 0.1

# The window starts at the last keyframe at least this long before the interval, so audio
# stored ahead of the video around the start is inside it too.
WINDOW_LEAD_S: Final = 1.0
# The window runs this long past the interval's end.
WINDOW_TAIL_S: Final = 2.0

# Reading every packet of a multi-hour VOD takes a while, but decodes nothing.
FULL_READ_TIMEOUT_S: Final = 1800.0
WINDOW_TIMEOUT_S: Final = 600.0

_DISCONTINUITY = re.compile(rb"timestamp discontinuity[^\n]*new offset= *(-?\d+)")


class TimelineError(ExportError):
    """The corrected timeline of a source with timestamp resets couldn't be worked out."""


@dataclass(frozen=True, slots=True)
class ResetTimeline:
    """The video keyframes of a source whose timestamps restart, on the corrected timeline.

    Attributes:
        keyframes: ``(time_s, byte_position)`` of every video keyframe, in file order. Times
            are seconds from the container start on FFmpeg's corrected timeline, so they keep
            increasing across resets.
        n_resets: How many times the timestamps jump back.
    """

    keyframes: tuple[tuple[float, int], ...]
    n_resets: int


_cache: dict[tuple[str, int, int], ResetTimeline | None] = {}
_cache_lock = threading.Lock()


def read_reset_timeline(source: Path, *, runner: Runner | None = None) -> ResetTimeline | None:
    """The corrected keyframe times of `source`, or None when its timestamps never jump back.

    Kept per file (path, size and modification time) for the rest of the process.

    Raises:
        TimelineError: The resets FFmpeg reports don't match the ones in the packet list.
        FFmpegError: FFmpeg or FFprobe failed.
    """
    stat = source.stat()
    key = (str(source.resolve()), stat.st_size, stat.st_mtime_ns)
    with _cache_lock:
        if key in _cache:
            return _cache[key]
    info = probe(source, runner=runner)
    formats = set(info.format_name.split(","))
    timeline = None
    if formats & _DISCONTINUOUS_FORMATS and info.video_streams:
        timeline = _read(source, runner=runner, has_audio=bool(info.audio_streams))
    with _cache_lock:
        _cache[key] = timeline
    return timeline


def _read(source: Path, *, runner: Runner | None, has_audio: bool) -> ResetTimeline | None:
    # Raw packets of the first video stream, in file order.
    packets = run_command(
        ffprobe_command(
            *("-select_streams", "v:0", "-show_entries", "packet=pts_time,dts_time,pos,flags"),
            *("-of", "csv=p=0", source),
        ),
        timeout_s=FULL_READ_TIMEOUT_S,
        runner=runner,
    ).stdout
    video = _parse_packets(packets, source)
    # Each stream's timestamps jump back at its own first packet after a reset.
    resets = sum(1 for _, _, _, jump in video if jump)
    if resets == 0:
        return None
    # The offsets FFmpeg adds, read the way normalization reads the audio (the reference).
    reference = "0:a:0" if has_audio else "0:v:0"
    # Its notes on each correction are warnings; the shared runner's default level hides them.
    result = run_command(
        ffmpeg_command(
            *("-loglevel", "warning", *TIMELINE_OPTIONS),
            *("-i", source, "-map", reference, "-c", "copy", "-f", "null", "-"),
        ),
        timeout_s=FULL_READ_TIMEOUT_S,
        runner=runner,
    )
    offsets_us = [int(m) for m in _DISCONTINUITY.findall(result.stderr)]
    if len(offsets_us) != resets:
        raise TimelineError(
            f"{source.name}: the video's timestamps restart {resets} time(s) but FFmpeg "
            f"reports {len(offsets_us)} correction(s), so its timeline can't be mapped"
        )
    container_start_s = _container_start(source, runner)
    keyframes: list[tuple[float, int]] = []
    segment = 0
    for time_s, pos, key, jump in video:
        if jump:
            segment += 1
        if key:
            offset_s = 0.0 if segment == 0 else offsets_us[segment - 1] / 1_000_000
            keyframes.append((round(time_s + offset_s - container_start_s, 6), pos))
    log.info("timeline resets source=%s n=%d", source, resets)
    return ResetTimeline(keyframes=tuple(keyframes), n_resets=resets)


def _parse_packets(stdout: bytes, source: Path) -> list[tuple[float, int, bool, bool]]:
    """``(pts_s, pos, is_keyframe, starts_after_a_reset)`` of each packet with a timestamp."""
    packets: list[tuple[float, int, bool, bool]] = []
    last_dts_s: float | None = None
    for line in stdout.decode("utf-8", errors="replace").splitlines():
        fields = line.strip().split(",")
        if len(fields) < 4 or not fields[0] or fields[0] == "N/A":
            continue
        try:
            pts_s = float(fields[0])
            dts_s = float(fields[1]) if fields[1] not in ("", "N/A") else pts_s
            pos = int(fields[2])
        except ValueError:
            raise FFmpegOutputError(
                f"unexpected ffprobe packet line for {source}: {line!r}"
            ) from None
        jump = last_dts_s is not None and dts_s + _BACKWARD_JUMP_S < last_dts_s
        last_dts_s = dts_s
        packets.append((pts_s, pos, fields[3].startswith("K"), jump))
    return packets


def _container_start(source: Path, runner: Runner | None) -> float:
    result = run_command(
        ffprobe_command("-show_entries", "format=start_time", "-of", "csv=p=0", source),
        timeout_s=60,
        runner=runner,
    )
    text = result.stdout.decode("utf-8", errors="replace").strip()
    try:
        return float(text)
    except ValueError:
        return 0.0


@dataclass(frozen=True, slots=True)
class Window:
    """Where to cut an interval from.

    Attributes:
        path: The file to cut from: the source itself, or a corrected copy of part of it.
        offset_s: Corrected time minus the file's own time (seconds from its container start);
            0 for the source itself.
    """

    path: Path
    offset_s: float

    def local(self, interval: Interval) -> Interval:
        """`interval` (corrected timeline) in this file's own time."""
        return Interval(interval.start_s - self.offset_s, interval.end_s - self.offset_s)

    def corrected(self, interval: Interval) -> Interval:
        """`interval` (this file's own time) on the corrected timeline."""
        return Interval(interval.start_s + self.offset_s, interval.end_s + self.offset_s)


@contextlib.contextmanager
def timeline_window(
    source: Path, interval: Interval, *, runner: Runner | None = None
) -> Iterator[Window]:
    """A file to cut `interval` (corrected timeline) from, valid inside the ``with`` block.

    For a source without timestamp resets this is the source itself. Otherwise it is a
    temporary stream copy of the part of the source around `interval`, deleted afterwards.

    Raises:
        TimelineError: The timeline can't be mapped, or the window came out empty.
        FFmpegError: FFmpeg or FFprobe failed.
    """
    timeline = read_reset_timeline(source, runner=runner)
    if timeline is None:
        yield Window(source, 0.0)
        return
    times = [time_s for time_s, _ in timeline.keyframes]
    # The last keyframe at least WINDOW_LEAD_S before the start, else the last one before it,
    # else the first.
    index = bisect.bisect_right(times, interval.start_s - WINDOW_LEAD_S) - 1
    if index < 0:
        index = max(0, bisect.bisect_right(times, interval.start_s) - 1)
    keyframe_s, position = timeline.keyframes[index]
    with tempfile.TemporaryDirectory(prefix="autoclipper-timeline-") as temp:
        path = Path(temp) / "window.mkv"
        duration_s = interval.end_s - keyframe_s + WINDOW_TAIL_S
        run_command(
            ffmpeg_command(
                "-y",
                *("-skip_initial_bytes", str(position)),
                *TIMELINE_OPTIONS,
                *("-i", source, "-map", "0:v:0", "-map", "0:a:0?"),
                # -copyinkf keeps the keyframe the window starts with as its first packet.
                *("-c", "copy", "-copyinkf", "-t", f"{duration_s:.6f}"),
                *("-f", "matroska", path),
            ),
            timeout_s=WINDOW_TIMEOUT_S,
            runner=runner,
        )
        first_s = _first_video_time(path, runner)
        window = Window(path, round(keyframe_s - first_s, 6))
        log.info(
            "timeline window source=%s keyframe_s=%.3f offset_s=%.3f",
            source,
            keyframe_s,
            window.offset_s,
        )
        yield window


def _first_video_time(path: Path, runner: Runner | None) -> float:
    """The first video packet's time in `path`, in seconds from its container start."""
    start = _container_start(path, runner)
    result = run_command(
        ffprobe_command(
            *("-select_streams", "v:0", "-read_intervals", "%+#1"),
            *("-show_entries", "packet=pts_time", "-of", "csv=p=0", path),
        ),
        timeout_s=60,
        runner=runner,
    )
    for line in result.stdout.decode("utf-8", errors="replace").splitlines():
        text = line.strip().strip(",")
        if text and text != "N/A":
            return float(text) - start
    raise TimelineError(f"the corrected copy of part of the source has no video ({path.name})")
