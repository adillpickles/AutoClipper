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
    probe_stream_ends,
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


def test_covered_span_prefers_measured_stream_ends_over_metadata() -> None:
    # MPEG-TS metadata estimates the audio end at 9.579 s; its packets run to 9.835 s.
    times = spans(StreamSpan(0, "video", 0.021, 10.0), StreamSpan(1, "audio", 0.0, 9.579))

    span = covered_span(times, [0, 1], 10.021, {0: 10.021, 1: 9.835})

    assert span == (0.021, 9.835)


def test_covered_span_uses_measured_ends_even_past_the_container_duration() -> None:
    # MKV reports no stream durations, and its container duration counts from 0, not from its
    # start; measured ends are in seconds from the container start and are used as they are.
    times = spans(StreamSpan(0, "video", 0.0, None), StreamSpan(1, "audio", 0.0, None))

    assert covered_span(times, [0, 1], 18.021, {0: 8.0, 1: 8.0}) == (0.0, 8.0)
    assert covered_span(times, [0, 1], 10.0, {1: 9.8}) == (0.0, 9.8)


# --- probe_stream_ends ---------------------------------------------------------------------------


class EndRunner:
    """Answers packet reads with the packets (absolute pts, duration) at or after the read start.

    A packet whose duration is None is printed without ``duration_time``, as ffprobe does.
    """

    def __init__(self, packets: Sequence[tuple[int, float, float | None]]) -> None:
        self.packets = packets
        self.commands: list[FFmpegCommand] = []

    def __call__(self, command: FFmpegCommand, /, *, timeout_s: float) -> CommandResult:
        self.commands.append(command)
        interval = self.interval(command)
        low_s = -math.inf if interval is None else float(interval.removesuffix("%"))
        packets: list[dict[str, object]] = []
        for index, pts_s, dur_s in self.packets:
            if pts_s < low_s:
                continue
            packet: dict[str, object] = {"stream_index": index, "pts_time": f"{pts_s:.6f}"}
            if dur_s is not None:
                packet["duration_time"] = f"{dur_s:.6f}"
            packets.append(packet)
        return CommandResult(0, stdout=json.dumps({"packets": packets}).encode())

    @staticmethod
    def interval(command: FFmpegCommand) -> str | None:
        if "-read_intervals" not in command.args:
            return None
        return command.args[command.args.index("-read_intervals") + 1]


def test_stream_ends_come_from_the_last_packets_near_the_end(source: Path) -> None:
    # Timestamps from 1.4 s, as in MPEG-TS; a third stream isn't asked for.
    runner = EndRunner([(0, 11.388, 0.033333), (1, 11.213333, 0.021333), (2, 5.0, 0.1)])

    ends = probe_stream_ends(
        source, [0, 1], container_start_s=1.4, duration_hint_s=10.021333, runner=runner
    )

    assert ends == {0: pytest.approx(10.021333), 1: pytest.approx(9.834666)}
    (command,) = runner.commands
    assert EndRunner.interval(command) == "1.421333%"  # 10 s before the expected end


def test_stream_ends_widen_the_read_until_every_stream_has_packets(source: Path) -> None:
    # The audio stopped long before the video.
    runner = EndRunner([(1, 100.0, 0.02), (0, 3599.0, 0.04)])

    ends = probe_stream_ends(
        source, [0, 1], container_start_s=0.0, duration_hint_s=3600.0, runner=runner
    )

    assert ends == {0: pytest.approx(3599.04), 1: pytest.approx(100.02)}
    assert [EndRunner.interval(c) for c in runner.commands] == [
        "3590.000000%",
        "3560.000000%",
        "3440.000000%",
        "2960.000000%",
        "1040.000000%",
        None,  # the whole file
    ]


def test_stream_ends_with_a_wrong_hint_fall_back_to_reading_the_whole_file(source: Path) -> None:
    # An MKV whose duration counts from 0 although its timestamps start at 10 s: the first read
    # starts after the last packet and finds nothing.
    runner = EndRunner([(0, 17.967, 0.033), (1, 17.979, 0.021)])

    ends = probe_stream_ends(
        source, [0, 1], container_start_s=10.0, duration_hint_s=18.021, runner=runner
    )

    assert ends == {0: pytest.approx(8.0), 1: pytest.approx(8.0)}
    assert [EndRunner.interval(c) for c in runner.commands] == ["18.021000%", None]


def test_a_last_packet_without_a_duration_lasts_the_median_frame_spacing(source: Path) -> None:
    # A 5 fps Matroska video without DefaultDuration: no packet has a duration, and they are
    # listed in decoding order, not in time order.
    times_s = [2.0, 2.8, 2.4, 2.2, 2.6]
    runner = EndRunner([(0, t, None) for t in times_s])

    ends = probe_stream_ends(source, [0], container_start_s=0.0, duration_hint_s=3.0, runner=runner)

    assert ends == {0: pytest.approx(3.0)}  # 2.8 s + 0.2 s, not 2.8 s


def test_only_packets_without_a_duration_use_the_estimate(source: Path) -> None:
    # The audio's last packet lacks a duration; the others say 21.333 ms.
    frame_s = 1024 / 48_000
    packets: list[tuple[int, float, float | None]] = [
        (1, 9.0 + n * frame_s, frame_s) for n in range(5)
    ]
    packets.append((1, 9.0 + 5 * frame_s, None))
    runner = EndRunner(packets)

    ends = probe_stream_ends(source, [1], container_start_s=0.0, duration_hint_s=9.2, runner=runner)

    assert ends == {1: pytest.approx(9.0 + 6 * frame_s)}


def test_an_earlier_packet_without_a_duration_lasts_until_the_next_one(source: Path) -> None:
    # Irregular frames, in decoding order: the inner ones lack durations, the last says 0.1 s.
    # The median gap is 0.9 s; giving it to the frame at 2.8 s would end the stream at 3.7 s,
    # past the known end of the last frame.
    runner = EndRunner(
        [(0, 0.0, 0.1), (0, 2.0, None), (0, 1.0, None), (0, 2.9, 0.1), (0, 2.8, None)]
    )

    ends = probe_stream_ends(source, [0], container_start_s=0.0, duration_hint_s=3.0, runner=runner)

    assert ends == {0: pytest.approx(3.0)}


def test_known_durations_are_used_as_they_are(source: Path) -> None:
    # Durations shorter than the spacing (a 0.5 s frame every 1 s) are not replaced.
    runner = EndRunner([(0, 0.0, 0.5), (0, 1.0, 0.5)])

    ends = probe_stream_ends(source, [0], container_start_s=0.0, duration_hint_s=2.0, runner=runner)

    assert ends == {0: pytest.approx(1.5)}


def test_a_lone_packet_without_a_duration_leaves_its_stream_to_the_metadata(source: Path) -> None:
    # One packet and no duration: its end can't be told, so the stream is left out ...
    runner = EndRunner([(0, 2.8, None), (1, 2.9, 0.1)])

    ends = probe_stream_ends(
        source, [0, 1], container_start_s=0.0, duration_hint_s=3.0, runner=runner
    )

    assert ends == {1: pytest.approx(3.0)}
    # ... and covered_span falls back to the stream's metadata for it.
    times = spans(StreamSpan(0, "video", 0.0, 3.0), StreamSpan(1, "audio", 0.0, None))
    assert covered_span(times, [0, 1], 3.0, ends) == (0.0, pytest.approx(3.0))


def test_a_stream_without_packets_is_left_out(source: Path) -> None:
    runner = EndRunner([(0, 9.9, 0.1)])

    ends = probe_stream_ends(
        source, [0, 1], container_start_s=0.0, duration_hint_s=10.0, runner=runner
    )

    assert ends == {0: pytest.approx(10.0)}


@pytest.mark.parametrize(
    "stdout",
    [b"not json", b'{"packets": {}}', b'{"packets": [{"pts_time": "1.0"}]}'],
)
def test_unexpected_packet_ends_output_raises(source: Path, stdout: bytes) -> None:
    with pytest.raises(FFmpegOutputError):
        probe_stream_ends(
            source, [0], container_start_s=0.0, duration_hint_s=5.0, runner=answer(stdout)
        )
