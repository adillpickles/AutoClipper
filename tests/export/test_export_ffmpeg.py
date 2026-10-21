"""Integration tests: real exports of generated test media with the installed FFmpeg."""

from collections.abc import Callable
from pathlib import Path

import pytest

from autoclipper.export import encode
from autoclipper.export.cut import ContextPadding, Interval
from autoclipper.export.encode import EncodeSettings, ExportRequest, export_clip
from autoclipper.export.reframe import CenterCrop, FixedRegion, Layout
from autoclipper.export.seek import probe_stream_times
from autoclipper.ffmpeg import FFmpegOutputError, ffmpeg_command, probe, run_command

pytestmark = pytest.mark.ffmpeg

TIMEOUT_S = 120.0
TOLERANCE_S = EncodeSettings().duration_tolerance_s

# Luma ramp: the brightness (Y, 16-235) of every frame encodes its source time: 16 + 10 * t.
LUMA_AT_0 = 16
LUMA_PER_S = 10
RAMP_DURATION_S = 20.0

# Lossless output (crf 0), so frames read back with the source's exact luma.
LOSSLESS = EncodeSettings(crf=0, preset="ultrafast")


def make_ramp_video(path: Path, *, keyframe_interval: int = 1000, ts_offset_s: float = 0.0) -> Path:
    """A silent 320x180, 25 fps, 20 s video whose brightness rises with time.

    Lossless (``-qp 0``), so a frame's luma reads back exactly. A keyframe every
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
        "-sc_threshold",
        "0",  # keyframes only where -g puts them
        "-output_ts_offset",
        str(ts_offset_s),
        path,
    )
    run_command(command, timeout_s=TIMEOUT_S)
    return path


def frame_luma(video: Path, index: int) -> float:
    """The luma (Y, 16-235) of frame `index` (0 = first), averaged over a 16x16 block.

    A block of the flat frame, not a downscale of all of it: scaling 1080x1920 to a few pixels
    shifts the values by a step or two.
    """
    pixels = video.with_name(f"{video.stem}-{index}.yuv")
    command = ffmpeg_command(
        "-n",
        "-i",
        video,
        "-vf",
        f"select=eq(n\\,{index}),crop=16:16",
        "-frames:v",
        "1",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "yuv420p",  # raw Y values; a gray output would stretch them to 0-255
        pixels,
    )
    run_command(command, timeout_s=TIMEOUT_S)
    return sum(pixels.read_bytes()[:256]) / 256


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
    ("name", "keyframe_interval", "ts_offset_s", "start_s"),
    [
        # Indexed container, one keyframe at 0; 7.37 is between the 25 fps frames at 7.36 and 7.40.
        ("ramp.mp4", 1000, 0.0, 7.37),
        # MPEG-TS (no index) with timestamps from 10 s and a keyframe every 2 s.
        ("ramp.ts", 50, 10.0, 7.37),
        # MPEG-TS with a keyframe every 10 s, longer than any fixed preroll: before and after
        # the second keyframe.
        ("long_gop.ts", 250, 10.0, 5.5),
        ("long_gop.ts", 250, 10.0, 13.37),
    ],
    ids=["mp4_single_keyframe", "mpegts_offset", "mpegts_long_gop", "mpegts_long_gop_late"],
)
def test_export_starts_at_a_start_between_keyframes(
    tmp_path: Path, name: str, keyframe_interval: int, ts_offset_s: float, start_s: float
) -> None:
    source = make_ramp_video(
        tmp_path / name, keyframe_interval=keyframe_interval, ts_offset_s=ts_offset_s
    )

    result = export_clip(
        ExportRequest(source, Interval(start_s, start_s + 1.0), tmp_path / "clip.mp4"),
        settings=LOSSLESS,
    )

    # Within one luma step (0.1 s): one source frame (0.04 s) plus rounding. A clip that began
    # on another keyframe, or froze on one, would be at least 0.63 s (6 steps) off.
    assert frame_luma(result.output, 0) == pytest.approx(LUMA_AT_0 + LUMA_PER_S * start_s, abs=1)
    # Output frame 15 of 30 fps shows the source half a second later.
    assert frame_luma(result.output, 15) == pytest.approx(
        LUMA_AT_0 + LUMA_PER_S * (start_s + 0.5), abs=1
    )
    assert_vertical_h264_aac(result.output, 1.0)


def test_export_that_cannot_decode_from_the_start_fails_instead_of_freezing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Force a seek past the only keyframe before the start (0 s; the next is at 10 s), as a
    # broken keyframe search would: decoding can then only begin at 10 s.
    monkeypatch.setattr(encode, "seek_point", lambda keyframe_s: 5.0)
    source = make_ramp_video(tmp_path / "long_gop.ts", keyframe_interval=250, ts_offset_s=10.0)

    with pytest.raises(FFmpegOutputError, match="video covers 4"):
        export_clip(ExportRequest(source, Interval(5.5, 12.5), tmp_path / "clip.mp4"))

    assert sorted(p.name for p in tmp_path.iterdir()) == ["long_gop.ts"]


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


@pytest.mark.parametrize(
    ("video_s", "audio_s", "short_stream"),
    [(2.0, 5.0, "video"), (5.0, 2.0, "audio")],
    ids=["video_ends_early", "audio_ends_early"],
)
def test_export_fails_when_a_source_stream_ends_inside_the_interval(
    tmp_path: Path, video_s: float, audio_s: float, short_stream: str
) -> None:
    source = tmp_path / "uneven.mp4"
    run_command(
        ffmpeg_command(
            "-n",
            "-f",
            "lavfi",
            "-i",
            f"testsrc2=size=320x180:rate=30:duration={video_s}",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:sample_rate=48000:duration={audio_s}",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            source,
        ),
        timeout_s=TIMEOUT_S,
    )

    # The container lasts 5 s, so the interval is inside the media, but one stream stops at 2 s.
    with pytest.raises(FFmpegOutputError, match=f"{short_stream} covers 0.000-1.0"):
        export_clip(ExportRequest(source, Interval(1.0, 4.0), tmp_path / "clip.mp4"))

    assert sorted(p.name for p in tmp_path.iterdir()) == ["uneven.mp4"]


def test_export_from_the_very_start_of_an_mpegts_whose_audio_starts_first(
    make_test_media: Callable[..., Path], tmp_path: Path
) -> None:
    source = make_test_media("vod.ts", duration_s=3.0)
    times = probe_stream_times(source)
    (video,) = (s for s in times.streams if s.codec_type == "video")
    assert video.start_s is not None
    assert video.start_s > 0  # no video frame at the container start

    result = export_clip(ExportRequest(source, Interval(0.0, 2.0), tmp_path / "clip.mp4"))

    assert_vertical_h264_aac(result.output, 2.0)
