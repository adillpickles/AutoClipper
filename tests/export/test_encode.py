"""Unit tests for `autoclipper.export.encode`: FFmpeg commands as data, run by a fake runner."""

import errno
import itertools
import json
import math
import os
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest

from autoclipper.export.cut import ContextPadding, Interval, IntervalTooShortError
from autoclipper.export.encode import (
    EncodeSettings,
    ExportRequest,
    UnsupportedSourceError,
    build_export_command,
    encode_timeout_s,
    export_clip,
)
from autoclipper.export.errors import ExportError
from autoclipper.export.reframe import (
    CenterCrop,
    FixedRegion,
    InvalidLayoutError,
    Layout,
    plan_reframe,
)
from autoclipper.ffmpeg import (
    AudioStream,
    CommandResult,
    FFmpegCommand,
    FFmpegExitError,
    FFmpegOutputError,
    VideoStream,
)

VIDEO = {"index": 0, "codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080}
AUDIO = {
    "index": 1,
    "codec_type": "audio",
    "codec_name": "aac",
    "sample_rate": "48000",
    "channels": 2,
}
OUTPUT_VIDEO = {**VIDEO, "width": 1080, "height": 1920}


def media_json(duration_s: float, *streams: dict[str, Any], start_s: float = 0.0) -> bytes:
    """ffprobe JSON that answers both `probe` and `probe_stream_times`.

    Every stream starts at the container start `start_s` and lasts `duration_s`, unless the
    stream's dict says otherwise.
    """
    times = {"start_time": f"{start_s:.6f}", "duration": f"{duration_s:.6f}"}
    container = {"format_name": "mov,mp4", **times}
    return json.dumps({"format": container, "streams": [{**times, **s} for s in streams]}).encode()


# A keyframe every 2 s, as Twitch asks broadcasters for.
EVERY_2_S = tuple(float(t) for t in range(0, 3600, 2))


class FakeRunner:
    """Answers ffprobe with canned JSON and records every command.

    Media probes get `source` or, for the temporary export file, `output`. Packet reads
    (``-read_intervals``) get the `keyframes_s` (absolute times) inside the requested interval.
    `on_ffmpeg` runs when the encode command does, e.g. to create a competing output file.
    """

    def __init__(
        self,
        source: bytes | None = None,
        output: bytes | None = None,
        ffmpeg: CommandResult | None = None,
        keyframes_s: Sequence[float] = EVERY_2_S,
        on_ffmpeg: Callable[[], object] | None = None,
    ) -> None:
        self.source = source if source is not None else media_json(600.0, VIDEO, AUDIO)
        self.output = output
        self.ffmpeg = ffmpeg if ffmpeg is not None else CommandResult(0)
        self.keyframes_s = keyframes_s
        self.on_ffmpeg = on_ffmpeg
        self.commands: list[FFmpegCommand] = []
        self.timeouts: list[float] = []

    def __call__(self, command: FFmpegCommand, /, *, timeout_s: float) -> CommandResult:
        self.commands.append(command)
        self.timeouts.append(timeout_s)
        if command.program == "ffmpeg":
            if self.on_ffmpeg is not None:
                self.on_ffmpeg()
            return self.ffmpeg
        if "-read_intervals" in command.args:
            low, _, high = value_after(command, "-read_intervals").partition("%")
            low_s = float(low) if low else -math.inf
            packets = [
                {"pts_time": f"{t:.6f}", "flags": "K__"}
                for t in self.keyframes_s
                if low_s <= t <= float(high)
            ]
            return CommandResult(0, stdout=json.dumps({"packets": packets}).encode())
        if command.args[-1].endswith(".partial"):
            assert self.output is not None, "the test didn't expect an output probe"
            return CommandResult(0, stdout=self.output)
        return CommandResult(0, stdout=self.source)

    @property
    def ffmpeg_commands(self) -> list[FFmpegCommand]:
        return [c for c in self.commands if c.program == "ffmpeg"]


def output_for(duration_s: float) -> bytes:
    return media_json(duration_s, OUTPUT_VIDEO, AUDIO)


@pytest.fixture
def source(tmp_path: Path) -> Path:
    path = tmp_path / "vod.mp4"
    path.write_bytes(b"not read by the fake runner")
    return path


def value_after(command: FFmpegCommand, option: str) -> str:
    return command.args[command.args.index(option) + 1]


def maps(command: FFmpegCommand) -> list[str]:
    """The values of every -map option, in order."""
    return [value for flag, value in itertools.pairwise(command.args) if flag == "-map"]


def exported_interval(command: FFmpegCommand) -> tuple[float, float]:
    """The source interval a command exports: input seek + trimmed preroll, then -t."""
    first_video_filter = value_after(command, "-filter:v").split(",")[0]
    assert first_video_filter.startswith("trim=start=")
    start_s = float(value_after(command, "-ss")) + float(first_video_filter.split("=")[-1])
    return round(start_s, 6), round(start_s + float(value_after(command, "-t")), 6)


def request(
    source: Path,
    start_s: float,
    end_s: float,
    *,
    layout: Layout | None = None,
    padding: ContextPadding | None = None,
    overwrite: bool = False,
) -> ExportRequest:
    return ExportRequest(
        source,
        Interval(start_s, end_s),
        source.parent / "out.mp4",
        layout=CenterCrop() if layout is None else layout,
        padding=ContextPadding() if padding is None else padding,
        overwrite=overwrite,
    )


# --- Command construction ----------------------------------------------------------------------


def test_command_seeks_before_the_start_and_trims_exactly_to_it(tmp_path: Path) -> None:
    source, output = tmp_path / "vod.mp4", tmp_path / "out.tmp"
    command = build_export_command(
        source,
        Interval(12.345, 42.5),
        VideoStream(0, "h264", 1920, 1080),
        AudioStream(1, "aac", 48_000, 2),
        plan_reframe(CenterCrop(), 1920, 1080),
        output,
        seek_s=7.345,
    )

    args = list(command.args)
    source_at = args.index(str(source))
    # Input options (no rotation, accurate seek, seek point) come right before -i. The seek
    # lands before the start; trim/atrim drop the preroll and shift time back to 0.
    assert args[source_at - 5 : source_at + 1] == [
        "-noautorotate",
        "-accurate_seek",
        "-ss",
        "7.345000",
        "-i",
        str(source),
    ]
    assert value_after(command, "-filter:v").startswith(
        "trim=start=5.000000,setpts=PTS-5.000000/TB,fps="
    )
    assert value_after(command, "-filter:a") == "atrim=start=5.000000,asetpts=PTS-5.000000/TB"
    # The duration limit is an output option, after the input.
    assert value_after(command, "-t") == "30.155000"
    assert args.index("-t") > source_at
    assert args[-3:] == ["-f", "mp4", str(output)]
    assert exported_interval(command) == (12.345, 42.5)


@pytest.mark.parametrize("seek_s", [-0.5, 12.5])
def test_command_rejects_a_seek_point_outside_0_to_the_start(tmp_path: Path, seek_s: float) -> None:
    with pytest.raises(ValueError, match="seek_s"):
        build_export_command(
            tmp_path / "vod.mp4",
            Interval(12.0, 20.0),
            VideoStream(0, "h264", 1920, 1080),
            AudioStream(1, "aac", 48_000, 2),
            plan_reframe(CenterCrop(), 1920, 1080),
            tmp_path / "out.tmp",
            seek_s=seek_s,
        )


def test_command_encodes_h264_aac_with_faststart_and_constant_frame_rate(tmp_path: Path) -> None:
    command = build_export_command(
        tmp_path / "vod.mp4",
        Interval(0.0, 10.0),
        VideoStream(0, "h264", 1920, 1080),
        AudioStream(1, "aac", 44_100, 1),
        plan_reframe(CenterCrop(), 1920, 1080),
        tmp_path / "out.tmp",
        seek_s=0.0,
    )

    assert value_after(command, "-filter:v") == (
        "trim=start=0.000000,setpts=PTS-0.000000/TB,"
        "fps=fps=30:round=down,crop=w=606:h=1080:x=656:y=0,"
        "scale=w=1080:h=1920:flags=lanczos,setsar=1,format=yuv420p"
    )
    assert value_after(command, "-c:v") == "libx264"
    assert value_after(command, "-preset") == "veryfast"
    assert value_after(command, "-crf") == "20"
    assert value_after(command, "-c:a") == "aac"
    assert value_after(command, "-b:a") == "160k"
    assert value_after(command, "-ar") == "48000"
    assert value_after(command, "-ac") == "2"
    assert value_after(command, "-movflags") == "+faststart"
    assert value_after(command, "-map_metadata") == "-1"
    assert maps(command) == [
        "0:0",
        "0:1",
    ]


def test_command_uses_encode_settings(tmp_path: Path) -> None:
    settings = EncodeSettings(frame_rate=60, preset="medium", crf=18, audio_bitrate_kbps=192)
    command = build_export_command(
        tmp_path / "vod.mp4",
        Interval(0.0, 10.0),
        VideoStream(0, "h264", 1920, 1080),
        AudioStream(1, "aac", 48_000, 2),
        plan_reframe(CenterCrop(), 1920, 1080),
        tmp_path / "out.tmp",
        seek_s=0.0,
        settings=settings,
    )

    assert ",fps=fps=60:round=down," in value_after(command, "-filter:v")
    assert value_after(command, "-preset") == "medium"
    assert value_after(command, "-crf") == "18"
    assert value_after(command, "-b:a") == "192k"


def test_command_maps_the_probed_stream_indexes(tmp_path: Path) -> None:
    command = build_export_command(
        tmp_path / "vod.mkv",
        Interval(0.0, 10.0),
        VideoStream(2, "h264", 1920, 1080),
        AudioStream(0, "opus", 48_000, 2),
        plan_reframe(CenterCrop(), 1920, 1080),
        tmp_path / "out.tmp",
        seek_s=0.0,
    )

    assert maps(command) == [
        "0:2",
        "0:0",
    ]


def test_command_without_source_audio_adds_a_silent_track(tmp_path: Path) -> None:
    source = tmp_path / "vod.mp4"
    command = build_export_command(
        source,
        Interval(0.0, 10.0),
        VideoStream(0, "h264", 1920, 1080),
        None,
        plan_reframe(CenterCrop(), 1920, 1080),
        tmp_path / "out.tmp",
        seek_s=0.0,
    )

    args = list(command.args)
    silence_at = args.index("anullsrc=channel_layout=stereo:sample_rate=48000")
    assert args[silence_at - 3 : silence_at] == ["-f", "lavfi", "-i"]
    assert silence_at > args.index(str(source))  # the second input, after the seeked source
    assert maps(command) == ["0:0", "1:a:0"]
    assert value_after(command, "-c:a") == "aac"


def test_extra_video_filters_run_on_the_reframed_frame_before_the_pixel_format(
    tmp_path: Path,
) -> None:
    command = build_export_command(
        tmp_path / "vod.mp4",
        Interval(0.0, 10.0),
        VideoStream(0, "h264", 1920, 1080),
        AudioStream(1, "aac", 48_000, 2),
        plan_reframe(CenterCrop(), 1920, 1080),
        tmp_path / "out.tmp",
        seek_s=0.0,
        video_filters=("subtitles=filename=captions.srt",),
    )

    assert value_after(command, "-filter:v").endswith(
        ",setsar=1,subtitles=filename=captions.srt,format=yuv420p"
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"frame_rate": 0},
        {"frame_rate": 121},
        {"preset": "placebo-ish"},
        {"crf": -1},
        {"crf": 52},
        {"audio_bitrate_kbps": 0},
        {"audio_sample_rate_hz": 0},
    ],
)
def test_encode_settings_reject_out_of_range_values(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match=next(iter(kwargs)).split("_")[0]):
        EncodeSettings(**kwargs)


def test_duration_tolerance_is_one_video_frame_plus_one_aac_frame() -> None:
    assert EncodeSettings().duration_tolerance_s == pytest.approx(1 / 30 + 1024 / 48_000)


# --- export_clip with a fake runner ------------------------------------------------------------


def test_export_cuts_the_requested_source_interval_without_a_lag_shift(source: Path) -> None:
    # Pins docs/data/labeling.md §5: the source-time interval reaches FFmpeg unchanged.
    runner = FakeRunner(output=output_for(30.0))

    result = export_clip(request(source, 100.0, 130.0), runner=runner)

    (command,) = runner.ffmpeg_commands
    assert exported_interval(command) == (100.0, 130.0)
    assert (result.start_s, result.end_s) == (100.0, 130.0)
    assert result.cut.requested == Interval(100.0, 130.0)


def test_export_applies_padding_and_reports_the_requested_interval_separately(
    source: Path,
) -> None:
    runner = FakeRunner(output=output_for(38.0))

    result = export_clip(
        request(source, 100.0, 130.0, padding=ContextPadding(before_s=3.0, after_s=5.0)),
        runner=runner,
    )

    (command,) = runner.ffmpeg_commands
    assert exported_interval(command) == (97.0, 135.0)
    assert result.cut.requested == Interval(100.0, 130.0)
    assert result.cut.padding == ContextPadding(3.0, 5.0)
    assert (result.start_s, result.end_s) == (97.0, 135.0)


def test_export_clamps_the_interval_to_the_probed_duration(source: Path) -> None:
    runner = FakeRunner(output=output_for(10.0))

    result = export_clip(request(source, 590.0, 620.0), runner=runner)

    (command,) = runner.ffmpeg_commands
    assert exported_interval(command) == (590.0, 600.0)
    assert result.end_s == 600.0
    assert result.cut.clamped_end


def test_export_seeks_before_the_last_keyframe_at_or_before_the_start(source: Path) -> None:
    # Keyframes 10 s apart: decoding must begin at the one at 20 s, not after it.
    runner = FakeRunner(output=output_for(1.0), keyframes_s=(0.0, 10.0, 20.0, 30.0))

    export_clip(request(source, 25.5, 26.5), runner=runner)

    (command,) = runner.ffmpeg_commands
    assert value_after(command, "-ss") == "19.000000"  # 1 s before the keyframe
    assert exported_interval(command) == (25.5, 26.5)


def test_export_counts_keyframe_times_from_the_container_start(source: Path) -> None:
    # An MPEG-TS whose timestamps start at 11.4 s: its keyframe at 21.4 s is 10 s into the file.
    runner = FakeRunner(
        source=media_json(600.0, VIDEO, AUDIO, start_s=11.4),
        output=output_for(1.0),
        keyframes_s=(11.4, 21.4, 31.4),
    )

    export_clip(request(source, 15.0, 16.0), runner=runner)

    (command,) = runner.ffmpeg_commands
    assert value_after(command, "-ss") == "9.000000"
    assert exported_interval(command) == (15.0, 16.0)


def test_export_outside_the_media_fails_before_running_ffmpeg(source: Path) -> None:
    runner = FakeRunner()

    with pytest.raises(IntervalTooShortError):
        export_clip(request(source, 600.0, 630.0), runner=runner)

    assert runner.ffmpeg_commands == []
    assert sorted(p.name for p in source.parent.iterdir()) == ["vod.mp4"]


def test_export_returns_what_the_output_probe_reports(source: Path) -> None:
    runner = FakeRunner(output=output_for(30.02))

    result = export_clip(
        request(source, 0.0, 30.0, layout=FixedRegion(0, 0, 0.5, 0.5)), runner=runner
    )

    assert result.output == source.parent / "out.mp4"
    assert result.output.is_file()
    assert (result.width, result.height) == (1080, 1920)
    assert (result.video_codec, result.audio_codec) == ("h264", "aac")
    assert result.duration_s == pytest.approx(30.02)
    assert result.layout == FixedRegion(0, 0, 0.5, 0.5)
    assert not result.silent_audio


def test_export_renames_the_temporary_file_and_leaves_nothing_else(source: Path) -> None:
    runner = FakeRunner(output=output_for(30.0))

    export_clip(request(source, 0.0, 30.0), runner=runner)

    (command,) = runner.ffmpeg_commands
    temp = Path(command.args[-1])
    assert temp.parent == source.parent
    assert temp.name.startswith(".out.")
    assert temp.suffix == ".partial"
    assert sorted(p.name for p in source.parent.iterdir()) == ["out.mp4", "vod.mp4"]


def test_export_without_source_audio_reports_the_silent_track(source: Path) -> None:
    runner = FakeRunner(source=media_json(60.0, VIDEO), output=output_for(10.0))

    result = export_clip(request(source, 0.0, 10.0), runner=runner)

    assert result.silent_audio
    (command,) = runner.ffmpeg_commands
    assert "1:a:0" in command.args


def test_export_of_a_source_without_video_fails(source: Path) -> None:
    runner = FakeRunner(source=media_json(60.0, AUDIO))

    with pytest.raises(UnsupportedSourceError, match="no video stream"):
        export_clip(request(source, 0.0, 10.0), runner=runner)

    assert runner.ffmpeg_commands == []


def test_export_with_a_region_too_small_for_the_source_fails_before_running_ffmpeg(
    source: Path,
) -> None:
    runner = FakeRunner()

    with pytest.raises(InvalidLayoutError):
        export_clip(request(source, 0.0, 10.0, layout=FixedRegion(0, 0, 0.001, 0.5)), runner=runner)

    assert runner.ffmpeg_commands == []


def test_export_refuses_to_replace_an_existing_output(source: Path) -> None:
    existing = source.parent / "out.mp4"
    existing.write_bytes(b"earlier export")
    runner = FakeRunner(output=output_for(10.0))

    with pytest.raises(FileExistsError):
        export_clip(request(source, 0.0, 10.0), runner=runner)

    assert runner.commands == []
    assert existing.read_bytes() == b"earlier export"


def test_export_with_overwrite_replaces_an_existing_output(source: Path) -> None:
    existing = source.parent / "out.mp4"
    existing.write_bytes(b"earlier export")
    runner = FakeRunner(output=output_for(10.0))

    export_clip(request(source, 0.0, 10.0, overwrite=True), runner=runner)

    assert existing.read_bytes() != b"earlier export"


def test_export_refuses_to_write_over_its_source(source: Path) -> None:
    runner = FakeRunner()

    with pytest.raises(UnsupportedSourceError, match="source"):
        export_clip(
            ExportRequest(source, Interval(0.0, 10.0), source, overwrite=True), runner=runner
        )

    assert runner.commands == []


def test_export_creates_the_output_directory(source: Path) -> None:
    output = source.parent / "exports" / "nested" / "clip.mp4"
    runner = FakeRunner(output=output_for(10.0))

    result = export_clip(ExportRequest(source, Interval(0.0, 10.0), output), runner=runner)

    assert result.output == output
    assert output.is_file()


def test_failed_encode_leaves_no_output_and_no_temporary_file(source: Path) -> None:
    runner = FakeRunner(ffmpeg=CommandResult(1, stderr=b"Conversion failed!"))

    with pytest.raises(FFmpegExitError, match="Conversion failed"):
        export_clip(request(source, 0.0, 10.0), runner=runner)

    assert sorted(p.name for p in source.parent.iterdir()) == ["vod.mp4"]


@pytest.mark.parametrize(
    ("output", "problem"),
    [
        (media_json(10.0, VIDEO, AUDIO), "video is h264 1920x1080"),
        (media_json(10.0, {**OUTPUT_VIDEO, "codec_name": "hevc"}, AUDIO), "video is hevc"),
        (media_json(10.0, OUTPUT_VIDEO, {**AUDIO, "codec_name": "mp3"}), "audio is mp3"),
        (media_json(10.0, OUTPUT_VIDEO), "1 video and 1 audio stream"),
        (media_json(9.5, OUTPUT_VIDEO, AUDIO), "duration is 9.500 s, expected 10.000 s"),
        (media_json(10.2, OUTPUT_VIDEO, AUDIO), "duration is 10.200 s"),
    ],
)
def test_output_that_does_not_match_the_contract_is_an_error_and_is_removed(
    source: Path, output: bytes, problem: str
) -> None:
    runner = FakeRunner(output=output)

    with pytest.raises(FFmpegOutputError, match=problem):
        export_clip(request(source, 0.0, 10.0), runner=runner)

    assert sorted(p.name for p in source.parent.iterdir()) == ["vod.mp4"]


@pytest.mark.parametrize(
    ("video", "audio", "problem"),
    [
        # Decoding began on a later frame: the video starts after the audio.
        ({**OUTPUT_VIDEO, "start_time": "1.650000", "duration": "8.350000"}, AUDIO, "video covers"),
        # A source stream ended inside the interval.
        ({**OUTPUT_VIDEO, "duration": "1.000000"}, AUDIO, "video covers 0.000-1.000 s"),
        (OUTPUT_VIDEO, {**AUDIO, "duration": "2.000000"}, "audio covers 0.000-2.000 s"),
    ],
)
def test_output_stream_that_does_not_cover_the_clip_is_an_error(
    source: Path, video: dict[str, Any], audio: dict[str, Any], problem: str
) -> None:
    runner = FakeRunner(output=media_json(10.0, video, audio))

    with pytest.raises(FFmpegOutputError, match=problem):
        export_clip(request(source, 0.0, 10.0), runner=runner)

    assert sorted(p.name for p in source.parent.iterdir()) == ["vod.mp4"]


def test_output_created_during_the_encode_is_kept_and_the_export_fails(source: Path) -> None:
    output = source.parent / "out.mp4"
    runner = FakeRunner(
        output=output_for(10.0), on_ffmpeg=lambda: output.write_bytes(b"another export")
    )

    with pytest.raises(FileExistsError, match="appeared during the export"):
        export_clip(request(source, 0.0, 10.0), runner=runner)

    assert output.read_bytes() == b"another export"
    assert sorted(p.name for p in source.parent.iterdir()) == ["out.mp4", "vod.mp4"]


def test_overwrite_replaces_an_output_created_during_the_encode(source: Path) -> None:
    output = source.parent / "out.mp4"
    runner = FakeRunner(
        output=output_for(10.0), on_ffmpeg=lambda: output.write_bytes(b"another export")
    )

    export_clip(request(source, 0.0, 10.0, overwrite=True), runner=runner)

    assert output.read_bytes() != b"another export"
    assert sorted(p.name for p in source.parent.iterdir()) == ["out.mp4", "vod.mp4"]


def _no_hard_links(*_args: object, **_kwargs: object) -> None:
    raise OSError(errno.EPERM, "hard links are not supported")


def test_without_hard_links_an_output_created_during_the_encode_is_still_kept(
    source: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os, "link", _no_hard_links)
    output = source.parent / "out.mp4"
    runner = FakeRunner(
        output=output_for(10.0), on_ffmpeg=lambda: output.write_bytes(b"another export")
    )

    # Windows falls back to a rename, which never replaces; elsewhere the export fails.
    expected = FileExistsError if os.name == "nt" else ExportError
    with pytest.raises(expected):
        export_clip(request(source, 0.0, 10.0), runner=runner)

    assert output.read_bytes() == b"another export"
    assert sorted(p.name for p in source.parent.iterdir()) == ["out.mp4", "vod.mp4"]


@pytest.mark.skipif(os.name != "nt", reason="the rename fallback is Windows-only")
def test_without_hard_links_windows_publishes_by_renaming(
    source: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os, "link", _no_hard_links)
    runner = FakeRunner(output=output_for(10.0))

    result = export_clip(request(source, 0.0, 10.0), runner=runner)

    assert result.output.is_file()
    assert sorted(p.name for p in source.parent.iterdir()) == ["out.mp4", "vod.mp4"]


def test_export_uses_a_timeout_that_grows_with_the_clip(source: Path) -> None:
    runner = FakeRunner(output=output_for(30.0))

    export_clip(request(source, 0.0, 30.0), runner=runner)

    encode_timeout = runner.timeouts[runner.commands.index(runner.ffmpeg_commands[0])]
    assert encode_timeout == encode_timeout_s(30.0)
    assert encode_timeout_s(60.0) > encode_timeout_s(30.0)


def test_export_passes_an_explicit_timeout_to_the_encode(source: Path) -> None:
    runner = FakeRunner(output=output_for(30.0))

    export_clip(request(source, 0.0, 30.0), runner=runner, timeout_s=5.0)

    assert runner.timeouts[runner.commands.index(runner.ffmpeg_commands[0])] == 5.0


def test_missing_source_raises_file_not_found(tmp_path: Path) -> None:
    runner = FakeRunner()

    with pytest.raises(FileNotFoundError):
        export_clip(request(tmp_path / "missing.mp4", 0.0, 10.0), runner=runner)

    assert runner.commands == []
