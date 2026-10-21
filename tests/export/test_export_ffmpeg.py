"""Integration tests: real exports of generated test media with the installed FFmpeg."""

from collections.abc import Callable
from pathlib import Path

import pytest

from autoclipper.export.cut import ContextPadding, Interval
from autoclipper.export.encode import EncodeSettings, ExportRequest, export_clip
from autoclipper.export.reframe import CenterCrop, FixedRegion, Layout
from autoclipper.ffmpeg import ffmpeg_command, probe, run_command

pytestmark = pytest.mark.ffmpeg

TIMEOUT_S = 120.0
TOLERANCE_S = EncodeSettings().duration_tolerance_s

# Luma ramp: the brightness (Y, 16-235) of every frame encodes its source time: 16 + 20 * t.
LUMA_AT_0 = 16
LUMA_PER_S = 20
RAMP_DURATION_S = 10.0


def make_ramp_video(path: Path, *, keyframe_interval: int = 1000, ts_offset_s: float = 0.0) -> Path:
    """A silent 320x180, 25 fps video whose brightness rises with time.

    Lossless (``-qp 0``), so a frame's luma reads back almost exactly. A keyframe every
    `keyframe_interval` frames (by default only one, at 0). `ts_offset_s` shifts the container's
    timestamps, like a VOD whose MPEG-TS timestamps don't start at 0.
    """
    ramp = (
        f"nullsrc=size=320x180:rate=25:duration={RAMP_DURATION_S},"
        f"geq=lum='{LUMA_AT_0}+{LUMA_PER_S}*T':cb=128:cr=128"
    )
    command = ffmpeg_command(
        "-n",
        "-f",
        "lavfi",
        "-i",
        ramp,
        "-c:v",
        "libx264",
        "-qp",
        "0",
        "-g",
        str(keyframe_interval),
        "-output_ts_offset",
        str(ts_offset_s),
        path,
    )
    run_command(command, timeout_s=TIMEOUT_S)
    return path


def first_frame_luma(video: Path) -> int:
    """The luma (Y, 16-235) of the video's first frame, averaged over the frame."""
    pixels = video.with_suffix(".yuv")
    command = ffmpeg_command(
        "-n",
        "-i",
        video,
        "-frames:v",
        "1",
        "-vf",
        "scale=2:2:flags=area",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "yuv420p",  # raw Y values; a gray output would stretch them to 0-255
        pixels,
    )
    run_command(command, timeout_s=TIMEOUT_S)
    return round(sum(pixels.read_bytes()[:4]) / 4)


def assert_vertical_h264_aac(output: Path, duration_s: float) -> None:
    info = probe(output)
    (video,) = info.video_streams
    (audio,) = info.audio_streams
    assert (video.codec_name, video.width, video.height) == ("h264", 1080, 1920)
    assert audio.codec_name == "aac"
    assert info.duration_s == pytest.approx(duration_s, abs=TOLERANCE_S)


@pytest.mark.parametrize(
    "layout",
    [
        CenterCrop(),
        FixedRegion(0.5, 0.0, 0.5, 1.0),  # right half, padded
        FixedRegion(0.0, 0.5, 0.5, 0.5, fit="crop"),  # bottom-left quarter, cropped
    ],
    ids=["center_crop", "fixed_region_pad", "fixed_region_crop"],
)
def test_export_of_a_5_second_video_is_a_vertical_h264_aac_clip(
    make_test_media: Callable[..., Path], tmp_path: Path, layout: Layout
) -> None:
    source = make_test_media("vod.mp4", duration_s=5.0, video_size=(640, 360))

    result = export_clip(ExportRequest(source, Interval(1.0, 3.5), tmp_path / "clip.mp4", layout))

    assert_vertical_h264_aac(result.output, 2.5)
    assert (result.width, result.height) == (1080, 1920)
    assert (result.video_codec, result.audio_codec) == ("h264", "aac")
    assert result.duration_s == pytest.approx(2.5, abs=TOLERANCE_S)
    assert not result.silent_audio
    assert sorted(p.name for p in tmp_path.iterdir()) == ["clip.mp4", "vod.mp4"]


@pytest.mark.parametrize(
    ("name", "keyframe_interval", "ts_offset_s"),
    [
        ("ramp.mp4", 1000, 0.0),  # indexed container, one keyframe at 0
        ("ramp.ts", 50, 10.0),  # MPEG-TS (no index), keyframes every 2 s, timestamps from 10 s
    ],
    ids=["mp4_single_keyframe", "mpegts_offset_timestamps"],
)
def test_export_starts_at_a_start_between_keyframes(
    tmp_path: Path, name: str, keyframe_interval: int, ts_offset_s: float
) -> None:
    source = make_ramp_video(
        tmp_path / name, keyframe_interval=keyframe_interval, ts_offset_s=ts_offset_s
    )
    start_s = 7.37  # between the 25 fps frames at 7.36 and 7.40, and between keyframes

    result = export_clip(ExportRequest(source, Interval(start_s, 8.37), tmp_path / "clip.mp4"))

    # Within about one source frame (0.04 s = 0.8 luma steps) plus encoding noise. A cut that
    # started on the keyframe before would be at least 1.37 s (27 steps) early, one that started
    # on the next keyframe 0.63 s (13 steps) late.
    assert first_frame_luma(result.output) == pytest.approx(LUMA_AT_0 + LUMA_PER_S * start_s, abs=3)
    assert_vertical_h264_aac(result.output, 1.0)


def test_export_of_a_silent_source_adds_a_silent_aac_track(tmp_path: Path) -> None:
    source = make_ramp_video(tmp_path / "silent.mp4")
    assert probe(source).audio_streams == ()

    result = export_clip(ExportRequest(source, Interval(0.0, 2.0), tmp_path / "clip.mp4"))

    assert result.silent_audio
    assert_vertical_h264_aac(result.output, 2.0)


def test_export_with_padding_clamped_at_the_media_end(
    make_test_media: Callable[..., Path], tmp_path: Path
) -> None:
    source = make_test_media("vod.mp4", duration_s=5.0)

    result = export_clip(
        ExportRequest(
            source,
            Interval(3.0, 4.5),
            tmp_path / "clip.mp4",
            padding=ContextPadding(before_s=1.0, after_s=2.0),
        )
    )

    assert (result.start_s, result.end_s) == (2.0, pytest.approx(5.0))
    assert result.cut.clamped_end
    assert_vertical_h264_aac(result.output, 3.0)


@pytest.mark.parametrize(
    "layout",
    [CenterCrop(), FixedRegion(0.1, 0.1, 0.8, 0.3)],
    ids=["center_crop", "fixed_region_pad"],
)
def test_export_of_an_odd_sized_portrait_source(tmp_path: Path, layout: Layout) -> None:
    # 301x541 is narrower than 9:16 and odd in both directions. testsrc keeps odd sizes (testsrc2
    # rounds them to even), and yuv444p lets x264 store them (yuv420p can't).
    source = tmp_path / "portrait.mkv"
    run_command(
        ffmpeg_command(
            "-n",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=301x541:rate=25:duration=3",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=44100:duration=3",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv444p",
            source,
        ),
        timeout_s=TIMEOUT_S,
    )
    (video,) = probe(source).video_streams
    assert (video.width, video.height) == (301, 541)

    result = export_clip(ExportRequest(source, Interval(0.5, 2.5), tmp_path / "clip.mp4", layout))

    assert_vertical_h264_aac(result.output, 2.0)
