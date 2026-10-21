"""Unit tests for `autoclipper.export.seek`, with a fake ffprobe."""

import json
import math
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from autoclipper.export.errors import ExportError
from autoclipper.export.seek import (
    KEYFRAME_SEARCH_WINDOW_S,
    SEEK_MARGIN_S,
    KeyframeNotFoundError,
    StreamSpan,
    StreamTimes,
    covered_span,
    find_keyframe,
    probe_stream_times,
    seek_point,
)
from autoclipper.ffmpeg import CommandResult, FFmpegCommand, FFmpegOutputError, Runner


class PacketRunner:
    """Answers packet reads with the keyframes (absolute times) inside the read interval."""

    def __init__(self, keyframes_s: Sequence[float], other_s: Sequence[float] = ()) -> None:
        self.keyframes_s = keyframes_s
        self.other_s = other_s
        self.commands: list[FFmpegCommand] = []
        self.timeouts: list[float] = []

    def __call__(self, command: FFmpegCommand, /, *, timeout_s: float) -> CommandResult:
        self.commands.append(command)
        self.timeouts.append(timeout_s)
        low, _, high = self.interval(command).partition("%")
        low_s = float(low) if low else -math.inf
        packets = [{"pts_time": f"{t:.6f}", "flags": "K__"} for t in self.keyframes_s]
        packets += [{"pts_time": f"{t:.6f}", "flags": "___"} for t in self.other_s]
        inside = [p for p in packets if low_s <= float(p["pts_time"]) <= float(high)]
        return CommandResult(0, stdout=json.dumps({"packets": inside}).encode())

    @staticmethod
    def interval(command: FFmpegCommand) -> str:
        return command.args[command.args.index("-read_intervals") + 1]


def answer(stdout: dict[str, Any] | bytes) -> Runner:
    """A runner that prints `stdout` (bytes, or a dict as JSON) for every command."""
    data = stdout if isinstance(stdout, bytes) else json.dumps(stdout).encode()

    def run(command: FFmpegCommand, /, *, timeout_s: float) -> CommandResult:
        return CommandResult(0, stdout=data)

    return run


@pytest.fixture
def source(tmp_path: Path) -> Path:
    path = tmp_path / "vod.ts"
    path.write_bytes(b"not read by the fake runner")
    return path


# --- find_keyframe --------------------------------------------------------------------------


def test_finds_the_last_keyframe_at_or_before_the_start_in_one_read(source: Path) -> None:
    runner = PacketRunner(keyframes_s=[float(t) for t in range(0, 60, 2)])

    keyframe_s = find_keyframe(source, 0, 25.5, container_start_s=0.0, runner=runner)

    assert keyframe_s == 24.0
    (command,) = runner.commands
    assert command.args[command.args.index("-select_streams") + 1] == "0"
    # The window ends 1 s past the start: packets come in decoding order.
    assert runner.interval(command) == "15.500000%26.500000"


def test_a_keyframe_exactly_at_the_start_is_used(source: Path) -> None:
    runner = PacketRunner(keyframes_s=[0.0, 10.0, 20.0])

    assert find_keyframe(source, 0, 20.0, container_start_s=0.0, runner=runner) == 20.0


def test_keyframes_after_the_start_and_other_packets_are_ignored(source: Path) -> None:
    runner = PacketRunner(keyframes_s=[20.0, 26.0], other_s=[24.0, 25.0, 25.4])

    assert find_keyframe(source, 0, 25.5, container_start_s=0.0, runner=runner) == 20.0


def test_times_count_from_the_container_start(source: Path) -> None:
    # MPEG-TS timestamps starting at 11.4 s: packet times are absolute, results relative.
    runner = PacketRunner(keyframes_s=[11.4, 21.4, 31.4])

    keyframe_s = find_keyframe(source, 1, 15.0, container_start_s=11.4, runner=runner)

    assert keyframe_s == pytest.approx(10.0)
    assert runner.interval(runner.commands[0]) == "16.400000%27.400000"


def test_the_window_widens_back_to_the_beginning_of_the_file(source: Path) -> None:
    runner = PacketRunner(keyframes_s=[0.0])

    keyframe_s = find_keyframe(source, 0, 100.0, container_start_s=0.0, runner=runner)

    assert keyframe_s == 0.0
    assert KEYFRAME_SEARCH_WINDOW_S == 10.0
    # 10 s, then 40 s, then 160 s, which reaches back past 0: read from the start, no seek.
    assert [runner.interval(c) for c in runner.commands] == [
        "90.000000%101.000000",
        "60.000000%101.000000",
        "%101.000000",
    ]
    assert runner.timeouts[0] < runner.timeouts[1] < runner.timeouts[2]


def test_a_video_that_starts_just_after_the_start_is_decoded_from_its_first_keyframe(
    source: Path,
) -> None:
    # MPEG-TS whose audio starts 23 ms before the video: nothing is before a start at 0.
    runner = PacketRunner(keyframes_s=[11.4, 13.4])

    keyframe_s = find_keyframe(source, 0, 0.0, container_start_s=11.376778, runner=runner)

    assert keyframe_s == pytest.approx(0.023222)


def test_no_keyframe_before_the_start_raises(source: Path) -> None:
    runner = PacketRunner(keyframes_s=[50.0])

    with pytest.raises(KeyframeNotFoundError, match=r"no keyframe at or before 30\.000 s"):
        find_keyframe(source, 0, 30.0, container_start_s=0.0, runner=runner)

    assert runner.interval(runner.commands[-1]) == "%31.000000"
    assert issubclass(KeyframeNotFoundError, ExportError)


@pytest.mark.parametrize(
    "stdout",
    [
        b"not json",
        b"[]",
        b'{"packets": {}}',
        b'{"packets": [1]}',
        b'{"packets": [{"pts_time": "x"}]}',
    ],
)
def test_unexpected_packet_output_raises(source: Path, stdout: bytes) -> None:
    with pytest.raises(FFmpegOutputError):
        find_keyframe(source, 0, 5.0, container_start_s=0.0, runner=answer(stdout))


def test_seek_point_is_a_margin_before_the_keyframe_and_never_negative() -> None:
    assert seek_point(20.0) == 20.0 - SEEK_MARGIN_S
    assert seek_point(0.5) == 0.0


# --- probe_stream_times ---------------------------------------------------------------------


def test_stream_times_count_from_the_container_start(source: Path) -> None:
    runner = answer(
        {
            "format": {"start_time": "11.376778"},
            "streams": [
                {"index": 0, "codec_type": "video", "start_time": "11.400000", "duration": "20.0"},
                {"index": 1, "codec_type": "audio", "start_time": "11.376778", "duration": "N/A"},
                {"index": 2, "codec_type": "data"},
            ],
        }
    )

    times = probe_stream_times(source, runner=runner)

    assert times.container_start_s == pytest.approx(11.376778)
    video, audio, data = times.streams
    assert video.start_s == pytest.approx(0.023222)
    assert video.duration_s == 20.0
    assert audio == StreamSpan(1, "audio", 0.0, None)
    assert data == StreamSpan(2, "data", None, None)


def test_stream_times_without_a_container_start_count_from_0(source: Path) -> None:
    runner = answer({"format": {}, "streams": [{"index": 0, "start_time": "0.5"}]})

    times = probe_stream_times(source, runner=runner)

    assert times.container_start_s == 0.0
    assert times.streams[0].start_s == 0.5


@pytest.mark.parametrize(
    "stdout",
    [
        b"not json",
        b'{"format": {}}',
        b'{"streams": [{"codec_type": "video"}]}',
        b'{"streams": [{"index": 0, "duration": "inf"}]}',
    ],
)
def test_unexpected_stream_times_output_raises(source: Path, stdout: bytes) -> None:
    with pytest.raises(FFmpegOutputError):
        probe_stream_times(source, runner=answer(stdout))


# --- covered_span --------------------------------------------------------------------------------


def spans(*streams: StreamSpan) -> StreamTimes:
    return StreamTimes(container_start_s=0.0, streams=streams)


def test_covered_span_ends_where_the_first_used_stream_ends() -> None:
    # The end of a VOD: video 10.0 s, audio 9.8 s.
    times = spans(StreamSpan(0, "video", 0.0, 10.0), StreamSpan(1, "audio", 0.0, 9.8))

    assert covered_span(times, [0, 1], 10.0) == (0.0, 9.8)
    assert covered_span(times, [0], 10.0) == (0.0, 10.0)  # a silent track replaces the audio


def test_covered_span_starts_where_the_last_used_stream_starts() -> None:
    times = spans(StreamSpan(0, "video", 0.023, 4.977), StreamSpan(1, "audio", 0.0, 5.0))

    assert covered_span(times, [0, 1], 5.0) == (0.023, 5.0)


def test_covered_span_ignores_streams_the_export_does_not_use() -> None:
    times = spans(
        StreamSpan(0, "video", 0.0, 10.0),
        StreamSpan(1, "audio", 0.0, 9.8),
        StreamSpan(2, "audio", 0.0, 3.0),
    )

    assert covered_span(times, [0, 1], 10.0) == (0.0, 9.8)


def test_covered_span_falls_back_to_the_container_for_missing_times() -> None:
    # MKV streams report no duration; a stream may report no start; a negative start (audio
    # priming) counts as 0.
    times = spans(StreamSpan(0, "video", None, None), StreamSpan(1, "audio", -0.021, None))

    assert covered_span(times, [0, 1], 12.5) == (0.0, 12.5)
    assert covered_span(spans(), [0, 1], 12.5) == (0.0, 12.5)


def test_covered_span_never_ends_after_the_container() -> None:
    times = spans(StreamSpan(0, "video", 0.0, 10.2))

    assert covered_span(times, [0], 10.0) == (0.0, 10.0)
