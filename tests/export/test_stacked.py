"""Tests for the stacked (facecam over gameplay) vertical export layout (#68)."""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from autoclipper.cli import ExitCode, app
from autoclipper.config import get_settings
from autoclipper.export.clip import (
    ClipSpec,
    NoFacecamConfiguredError,
    available_layouts,
    layout_named,
    render_clip,
)
from autoclipper.export.cut import Interval
from autoclipper.export.encode import build_export_command
from autoclipper.export.facecam import (
    _padded_corner_box,  # pyright: ignore[reportPrivateUsage]
)
from autoclipper.export.reframe import (
    OUTPUT_HEIGHT,
    OUTPUT_WIDTH,
    FixedRegion,
    InvalidLayoutError,
    PixelRect,
    StackedLayout,
    StackedReframePlan,
    load_channel_stacked_layouts,
    plan_reframe,
    stacked_layout_for_channel,
    stacked_layout_from_region,
)
from autoclipper.ffmpeg import AudioStream, VideoStream, ffmpeg_command, probe, run_command

CHANNELS_PATH = Path(__file__).resolve().parents[2] / "configs" / "channels.yaml"
# Pilot channels with a facecam preset; loltyler1 (#35) has none yet.
PILOT_CHANNELS = ("caseoh_", "ishowspeed", "kaicenat", "jynxzi")
PILOT_CHANNELS_WITHOUT_PRESET = ("loltyler1",)


def test_stacked_layout_validation() -> None:
    facecam = FixedRegion(0.02, 0.04, 0.22, 0.28, fit="crop")
    with pytest.raises(InvalidLayoutError, match="facecam_fraction"):
        StackedLayout(facecam=facecam, facecam_fraction=0.05)
    with pytest.raises(InvalidLayoutError, match="facecam_fraction"):
        StackedLayout(facecam=facecam, facecam_fraction=0.9)
    with pytest.raises(InvalidLayoutError, match="facecam_fraction"):
        StackedLayout(facecam=facecam, facecam_fraction=float("nan"))


def test_plan_reframe_builds_even_stacked_panes_summing_to_1080x1920() -> None:
    layout = stacked_layout_from_region((0.02, 0.04, 0.22, 0.28), facecam_fraction=0.35)
    plan = plan_reframe(layout, 1920, 1080)

    assert isinstance(plan, StackedReframePlan)
    assert plan.output_width == OUTPUT_WIDTH
    assert plan.output_height == OUTPUT_HEIGHT
    assert plan.top.scaled_width == OUTPUT_WIDTH
    assert plan.bottom.scaled_width == OUTPUT_WIDTH
    assert plan.top.scaled_height % 2 == 0
    assert plan.bottom.scaled_height % 2 == 0
    assert plan.top.scaled_height + plan.bottom.scaled_height == OUTPUT_HEIGHT
    assert plan.top.scaled_height == 672
    assert plan.bottom.scaled_height == 1248


type Box = tuple[float, float, float, float]


def _face_visible(crop: PixelRect, face: Box, width: int, height: int) -> float:
    """The share of the normalized `face` box inside `crop` (source pixels)."""
    fx, fy = face[0] * width, face[1] * height
    fw, fh = face[2] * width, face[3] * height
    ix = max(0.0, min(crop.x + crop.width, fx + fw) - max(crop.x, fx))
    iy = max(0.0, min(crop.y + crop.height, fy + fh) - max(crop.y, fy))
    return ix * iy / (fw * fh)


def _assert_even_and_inside(crop: PixelRect, region: PixelRect) -> None:
    assert all(v % 2 == 0 for v in (crop.x, crop.y, crop.width, crop.height))
    assert region.x <= crop.x
    assert region.y <= crop.y
    assert crop.x + crop.width <= region.x + region.width
    assert crop.y + crop.height <= region.y + region.height


# A tall bottom-left overlay (0-576 x 648-1080 px of 1920x1080); the 1080x672 pane keeps 358 of
# its 432 rows, so a centered trim drops 37 px at the top and bottom.
TALL_CAM: Box = (0.0, 0.6, 0.3, 0.4)
TALL_CAM_PX = PixelRect(0, 648, 576, 432)


@pytest.mark.parametrize(
    "face",
    [
        pytest.param((0.1, 0.6, 0.1, 0.2), id="face-at-the-top"),
        pytest.param((0.1, 0.8, 0.1, 0.2), id="face-at-the-bottom"),
        pytest.param((0.02, 0.61, 0.09, 0.16), id="small-face-in-the-corner"),
        pytest.param((0.1, 0.7, 0.1, 0.2), id="face-in-the-middle"),
    ],
)
def test_the_facecam_pane_is_trimmed_around_a_known_face(face: Box) -> None:
    centered = plan_reframe(stacked_layout_from_region(TALL_CAM), 1920, 1080).top.crop
    anchored = plan_reframe(stacked_layout_from_region(TALL_CAM, face=face), 1920, 1080).top.crop

    assert _face_visible(anchored, face, 1920, 1080) == pytest.approx(1.0)
    assert (anchored.width, anchored.height) == (centered.width, centered.height)
    _assert_even_and_inside(anchored, TALL_CAM_PX)


def test_a_centered_trim_cuts_a_face_at_the_top_of_a_tall_overlay() -> None:
    """The bug this guards against: without the face, the trim is centered on the overlay."""
    face: Box = (0.1, 0.6, 0.1, 0.2)
    centered = plan_reframe(stacked_layout_from_region(TALL_CAM), 1920, 1080).top.crop

    assert centered == PixelRect(0, 684, 576, 358)
    assert _face_visible(centered, face, 1920, 1080) < 0.85


@pytest.mark.parametrize(
    "face",
    [
        pytest.param((0.6, 0.05, 0.08, 0.14), id="face-at-the-left"),
        pytest.param((0.9, 0.05, 0.08, 0.14), id="face-at-the-right"),
    ],
)
def test_a_wide_overlay_is_trimmed_at_the_sides_around_the_face(face: Box) -> None:
    wide: Box = (0.6, 0.0, 0.4, 0.2)  # 768x216 px, wider than the 1080x672 pane
    region = PixelRect(1152, 0, 768, 216)

    crop = plan_reframe(stacked_layout_from_region(wide, face=face), 1920, 1080).top.crop

    assert crop.height == region.height
    assert crop.width < region.width
    assert _face_visible(crop, face, 1920, 1080) == pytest.approx(1.0)
    _assert_even_and_inside(crop, region)


def test_a_face_reaching_past_the_overlay_keeps_the_part_inside_it() -> None:
    # The face box starts above the overlay (Haar boxes include the forehead; a webcam cropped
    # tight to the head has its border there): the pane starts at the overlay's top.
    face: Box = (0.1, 0.56, 0.1, 0.2)

    crop = plan_reframe(stacked_layout_from_region(TALL_CAM, face=face), 1920, 1080).top.crop

    assert crop.y == TALL_CAM_PX.y
    _assert_even_and_inside(crop, TALL_CAM_PX)


def test_without_a_face_the_stacked_plan_is_unchanged() -> None:
    plan = plan_reframe(stacked_layout_from_region((0.02, 0.04, 0.22, 0.28)), 1920, 1080)

    # The same crop as before faces were carried (centered on the region).
    assert plan.top.crop == PixelRect(38, 62, 422, 262)
    assert plan.top.filters()[0] == "crop=w=422:h=262:x=38:y=62"


def test_a_face_on_odd_sized_sources_still_gives_even_crops_inside_the_frame() -> None:
    face: Box = (0.05, 0.62, 0.1, 0.18)
    for width, height in ((1921, 1081), (321, 181), (1280, 720)):
        layout = stacked_layout_from_region(TALL_CAM, face=face)
        crop = plan_reframe(layout, width, height).top.crop
        assert all(v % 2 == 0 for v in (crop.x, crop.y, crop.width, crop.height))
        assert crop.x + crop.width <= width
        assert crop.y + crop.height <= height


@pytest.mark.parametrize("corner_x", [0.01, 0.04, 0.08, 0.84, 0.88])
@pytest.mark.parametrize("corner_y", [0.01, 0.05, 0.1, 0.7, 0.75])
@pytest.mark.parametrize("face_width", [0.06, 0.08, 0.1])
def test_padded_corner_boxes_keep_the_whole_face_in_the_pane(
    corner_x: float, corner_y: float, face_width: float
) -> None:
    face: Box = (corner_x, corner_y, face_width, face_width * 16 / 9)
    box = _padded_corner_box(face, 1920, 1080)
    assert isinstance(box, tuple)

    crop = plan_reframe(stacked_layout_from_region(box, face=face), 1920, 1080).top.crop

    assert _face_visible(crop, face, 1920, 1080) == pytest.approx(1.0)


def test_stacked_layout_rejects_a_bad_face() -> None:
    facecam = FixedRegion(0.0, 0.6, 0.3, 0.4, fit="crop")
    with pytest.raises(InvalidLayoutError, match="face"):
        StackedLayout(facecam=facecam, face=(0.1, 0.6, float("nan"), 0.2))
    with pytest.raises(InvalidLayoutError, match="face"):
        StackedLayout(facecam=facecam, face=(0.1, 0.6, 0.0, 0.2))


def _overlap(a: PixelRect, b: PixelRect) -> int:
    w = min(a.x + a.width, b.x + b.width) - max(a.x, b.x)
    h = min(a.y + a.height, b.y + b.height) - max(a.y, b.y)
    return max(0, w) * max(0, h)


def _box_px(box: Box, width: int, height: int) -> PixelRect:
    """`box` in source pixels as the planner rounds it (`FixedRegion` crop)."""
    plan = plan_reframe(FixedRegion(*box, fit="pad"), width, height)
    return plan.crop


def _least_overlap(crop: PixelRect, bounds: PixelRect, cam: PixelRect) -> int:
    """The least `cam` area any even position of `crop` inside `bounds` can share."""
    return min(
        _overlap(PixelRect(x, y, crop.width, crop.height), cam)
        for x in range(bounds.x, bounds.x + bounds.width - crop.width + 1, 2)
        for y in range(bounds.y, bounds.y + bounds.height - crop.height + 1, 2)
    )


FULL_FRAME: Box = (0.0, 0.0, 1.0, 1.0)


@pytest.mark.parametrize(
    ("cam", "gameplay"),
    [
        # The QA clip (#229): VOD 2941757172 at 32:31, the webcam detected bottom-left.
        pytest.param((0.0, 0.606, 0.297, 0.394), FULL_FRAME, id="bottom-left"),
        pytest.param((0.72, 0.65, 0.26, 0.32), FULL_FRAME, id="bottom-right"),
        pytest.param((0.76, 0.04, 0.22, 0.28), FULL_FRAME, id="top-right"),
        pytest.param((0.02, 0.66, 0.24, 0.30), FULL_FRAME, id="bottom-left-preset"),
        pytest.param((0.0, 0.6, 0.3, 0.4), (0.1, 0.0, 0.8, 1.0), id="explicit-gameplay"),
        # A tall, narrow gameplay region: the pane is full width, so it moves up instead.
        pytest.param((0.0, 0.85, 0.2, 0.15), (0.0, 0.0, 0.4, 1.0), id="moves-vertically"),
    ],
)
def test_the_gameplay_pane_keeps_clear_of_the_facecam(cam: Box, gameplay: Box) -> None:
    plan = plan_reframe(stacked_layout_from_region(cam, gameplay_region=gameplay), 1920, 1080)
    crop = plan.bottom.crop
    bounds = _box_px(gameplay, 1920, 1080)
    cam_px = _box_px(cam, 1920, 1080)

    _assert_even_and_inside(crop, bounds)
    assert _overlap(crop, cam_px) == 0
    # Only the position moves: the pane is still the largest one the region holds.
    assert crop.width == bounds.width or crop.height == bounds.height
    assert (plan.bottom.scaled_width, plan.bottom.scaled_height) == (OUTPUT_WIDTH, 1248)


def test_the_qa_clips_gameplay_pane_moves_right_of_the_webcam() -> None:
    """#229: the centered pane (x 493-1427) included the right 77 px of the 570 px webcam."""
    layout = stacked_layout_from_region((0.0, 0.606, 0.297, 0.394))

    crop = plan_reframe(layout, 1920, 1080).bottom.crop

    # The webcam ends at x 570; the pane starts 10 px (0.5 % of the width) clear of it.
    assert crop == PixelRect(580, 0, 934, 1080)


def test_a_gameplay_pane_already_clear_of_the_facecam_stays_centered() -> None:
    clear = plan_reframe(stacked_layout_from_region((0.76, 0.04, 0.22, 0.28)), 1920, 1080)

    assert clear.bottom.crop == PixelRect(492, 0, 934, 1080)


def test_an_unavoidable_facecam_overlaps_the_gameplay_pane_as_little_as_possible() -> None:
    # A webcam over the left 60 % of the frame: no 934 px wide pane can miss it.
    cam: Box = (0.0, 0.0, 0.6, 1.0)
    crop = plan_reframe(stacked_layout_from_region(cam), 1920, 1080).bottom.crop
    cam_px = _box_px(cam, 1920, 1080)
    frame = PixelRect(0, 0, 1920, 1080)

    assert _overlap(crop, cam_px) > 0
    assert _overlap(crop, cam_px) == _least_overlap(crop, frame, cam_px)
    assert crop.x == 1920 - 934  # as far right as the frame allows
    _assert_even_and_inside(crop, frame)


@pytest.mark.parametrize("size", [(1921, 1081), (1280, 720), (321, 181), (1080, 1920)])
@pytest.mark.parametrize(
    "cam",
    [(0.0, 0.606, 0.297, 0.394), (0.72, 0.65, 0.26, 0.32), (0.3, 0.3, 0.4, 0.4)],
)
def test_the_moved_gameplay_pane_is_even_and_overlaps_least(
    size: tuple[int, int], cam: Box
) -> None:
    width, height = size
    crop = plan_reframe(stacked_layout_from_region(cam), width, height).bottom.crop
    frame = PixelRect(0, 0, width - width % 2, height - height % 2)
    cam_px = _box_px(cam, width, height)

    _assert_even_and_inside(crop, frame)
    assert _overlap(crop, cam_px) == _least_overlap(crop, frame, cam_px)


def test_build_export_command_uses_filter_complex_with_vstack_and_post_filters(
    tmp_path: Path,
) -> None:
    layout = stacked_layout_from_region((0.02, 0.04, 0.22, 0.28))
    plan = plan_reframe(layout, 1920, 1080)
    cmd = build_export_command(
        tmp_path / "vod.mp4",
        Interval(10.0, 20.0),
        VideoStream(index=0, codec_name="h264", width=1920, height=1080),
        AudioStream(index=1, codec_name="aac", sample_rate_hz=48_000, channels=2),
        plan,
        tmp_path / "out.mp4",
        seek_s=8.0,
        video_filters=("subtitles='subs.ass'",),
    )

    args = list(cmd.args)
    assert "-filter_complex" in args
    assert "-filter:v" not in args
    graph = args[args.index("-filter_complex") + 1]
    assert "split=2" in graph
    assert "vstack=inputs=2" in graph
    # Post filters (captions + format) run after vstack on the combined 1080x1920 frame.
    assert graph.index("vstack=inputs=2") < graph.index("subtitles='subs.ass'")
    assert "[ac_vout]" in args


def test_all_pilot_channels_have_valid_stacked_layouts_in_channels_yaml() -> None:
    presets = load_channel_stacked_layouts(CHANNELS_PATH)

    for channel in PILOT_CHANNELS:
        assert channel in presets
        layout = stacked_layout_for_channel(channel.upper(), channels_path=CHANNELS_PATH)
        assert layout is not None
        assert layout == presets[channel]
        for width, height in ((1920, 1080), (1280, 720)):
            plan = plan_reframe(layout, width, height)
            assert isinstance(plan, StackedReframePlan)
            assert plan.top.scaled_height + plan.bottom.scaled_height == OUTPUT_HEIGHT
    for channel in PILOT_CHANNELS_WITHOUT_PRESET:
        assert channel not in presets
        assert stacked_layout_for_channel(channel, channels_path=CHANNELS_PATH) is None


def test_available_layouts_and_layout_named_for_stacked() -> None:
    assert available_layouts(None) == ("center_crop",)
    assert available_layouts(None, facecam_region=(0.0, 0.0, 0.25, 0.3)) == (
        "center_crop",
        "stacked",
    )
    assert available_layouts((0.25, 0.0, 0.5, 1.0), facecam_region=(0.0, 0.0, 0.25, 0.3)) == (
        "center_crop",
        "configured_region",
        "stacked",
    )

    with pytest.raises(NoFacecamConfiguredError):
        layout_named("stacked", None)

    resolved = layout_named("stacked", None, facecam_region=(0.02, 0.04, 0.22, 0.28))
    assert isinstance(resolved, StackedLayout)
    assert resolved.facecam.x == pytest.approx(0.02)


def test_export_facecam_region_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTOCLIPPER_EXPORT_FACECAM_REGION", "0.02,0.04,0.22,0.28")
    get_settings.cache_clear()
    try:
        assert get_settings().export_facecam_region == (0.02, 0.04, 0.22, 0.28)
    finally:
        get_settings.cache_clear()


def _make_facecam_over_gameplay_source(path: Path, facecam: FixedRegion) -> Path:
    """Generate a 16:9 640x360 clip: blue gameplay with a bright red facecam at `facecam`."""
    x = round(facecam.x * 640)
    y = round(facecam.y * 360)
    w = round(facecam.width * 640)
    h = round(facecam.height * 360)
    run_command(
        ffmpeg_command(
            "-y",
            *("-f", "lavfi", "-i", "color=c=blue:s=640x360:r=10:d=3"),
            *("-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=3"),
            *(
                "-vf",
                f"drawbox=x={x}:y={y}:w={w}:h={h}:color=red:t=fill",
            ),
            *("-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"),
            *("-c:a", "aac", "-shortest", path),
        ),
        timeout_s=60,
    )
    return path


def _sample_rgb(video: Path, x: int, y: int) -> tuple[int, int, int]:
    """Read one pixel's (R, G, B) from frame 0 of `video` via FFmpeg rawvideo."""
    result = run_command(
        ffmpeg_command(
            "-i",
            video,
            "-frames:v",
            "1",
            "-vf",
            f"format=rgb24,crop=1:1:{x}:{y}",
            "-f",
            "rawvideo",
            "-",
        ),
        timeout_s=30,
    )
    assert len(result.stdout) >= 3
    return (result.stdout[0], result.stdout[1], result.stdout[2])


@pytest.mark.ffmpeg
def test_stacked_export_puts_facecam_on_top_and_gameplay_below(tmp_path: Path) -> None:
    """End-to-end CLI export with --layout stacked --channel jynxzi places red facecam on top
    and blue gameplay below inside a 1080x1920 H.264/AAC MP4 (#68)."""
    preset = stacked_layout_for_channel("jynxzi", channels_path=CHANNELS_PATH)
    assert preset is not None
    source = _make_facecam_over_gameplay_source(tmp_path / "stream.mp4", preset.facecam)
    out = tmp_path / "stacked.mp4"

    cli = CliRunner()
    res = cli.invoke(
        app,
        [
            "export",
            str(source),
            "--start",
            "0.5",
            "--end",
            "2.0",
            "--layout",
            "stacked",
            "--channel",
            "jynxzi",
            "--channels",
            str(CHANNELS_PATH),
            "--output",
            str(out),
        ],
    )
    assert res.exit_code == ExitCode.SUCCESS, res.output
    info = probe(out)
    assert info.video_streams[0].codec_name == "h264"
    assert (info.video_streams[0].width, info.video_streams[0].height) == (1080, 1920)
    assert info.video_streams[0].pix_fmt == "yuv420p"
    assert info.audio_streams[0].codec_name == "aac"
    assert info.duration_s == pytest.approx(1.5, abs=0.15)

    # Top pane center (y=336) comes from the red facecam box; bottom center (y=1300) is blue.
    top_r, _top_g, top_b = _sample_rgb(out, 540, 336)
    bot_r, _bot_g, bot_b = _sample_rgb(out, 540, 1300)
    assert top_r > 180
    assert top_b < 80
    assert bot_b > 180
    assert bot_r < 80


@pytest.mark.ffmpeg
@pytest.mark.parametrize("channel", PILOT_CHANNELS)
def test_each_pilot_channel_stacked_preset_renders_1080x1920(tmp_path: Path, channel: str) -> None:
    """Every pilot channel preset crops its own facecam corner onto the top pane and gameplay
    onto the bottom pane inside 1080x1920 (#68)."""
    preset = stacked_layout_for_channel(channel, channels_path=CHANNELS_PATH)
    assert preset is not None
    source = _make_facecam_over_gameplay_source(tmp_path / f"{channel}_src.mp4", preset.facecam)
    out = tmp_path / f"{channel}_stacked.mp4"

    rendered = render_clip(
        ClipSpec(
            source=source,
            interval=Interval(0.0, 1.0),
            output=out,
            layout="stacked",
            captions=False,
            stacked_layout=preset,
        )
    )
    assert rendered.output == out
    info = probe(out)
    assert (info.video_streams[0].width, info.video_streams[0].height) == (1080, 1920)
    plan = plan_reframe(preset, 640, 360)
    top_y = plan.top.output_height // 2
    bot_y = plan.top.output_height + plan.bottom.output_height // 2
    top_r, _top_g, top_b = _sample_rgb(out, 540, top_y)
    bot_r, _bot_g, bot_b = _sample_rgb(out, 540, bot_y)
    assert top_r > 180
    assert top_b < 80
    assert bot_b > 180
    assert bot_r < 80


def test_load_channel_stacked_layouts_handles_missing_and_partial_entries(tmp_path: Path) -> None:
    assert load_channel_stacked_layouts(tmp_path / "does-not-exist.yaml") == {}

    no_list = tmp_path / "no_list.yaml"
    no_list.write_text("channels: null\n", encoding="utf-8")
    assert load_channel_stacked_layouts(no_list) == {}

    mixed = tmp_path / "mixed.yaml"
    mixed.write_text(
        "\n".join(
            [
                "channels:",
                "  - not-a-mapping",
                "  - login: '   '",
                "    stacked_layout:",
                "      facecam: [0.0, 0.0, 0.25, 0.3]",
                "  - login: nocam",
                "  - login: ' StreamerOne '",
                "    broadcaster_id: ' 424242 '",
                "    stacked_layout:",
                "      facecam: [0.05, 0.05, 0.25, 0.3]",
                "      gameplay: [0.1, 0.1, 0.8, 0.8]",
                "      facecam_fraction: 0.4",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    presets = load_channel_stacked_layouts(mixed)

    assert set(presets) == {"streamerone", "424242"}
    layout = presets["streamerone"]
    assert presets["424242"] == layout
    assert layout.facecam == FixedRegion(0.05, 0.05, 0.25, 0.3, fit="crop")
    assert layout.gameplay == FixedRegion(0.1, 0.1, 0.8, 0.8, fit="crop")
    assert layout.facecam_fraction == pytest.approx(0.4)
    assert stacked_layout_for_channel("424242", channels_path=mixed) == layout


@pytest.mark.parametrize(
    ("content", "match"),
    [
        pytest.param("- just\n- a\n- list\n", "YAML mapping", id="root-not-mapping"),
        pytest.param(
            "channels:\n  - login: demo\n    stacked_layout: invalid\n",
            "must be a map",
            id="stacked-layout-not-map",
        ),
        pytest.param(
            "channels:\n  - login: demo\n    stacked_layout:\n      facecam: [0.0, 0.0, 0.2]\n",
            r"\[x, y, width, height\]",
            id="facecam-wrong-length",
        ),
        pytest.param(
            "channels:\n  - login: demo\n    stacked_layout:\n"
            "      facecam: [0.0, 0.0, true, 0.3]\n",
            "entries must be numbers",
            id="facecam-bool-entry",
        ),
        pytest.param(
            "channels:\n  - login: demo\n    stacked_layout:\n"
            "      facecam: [0.0, 0.0, 0.25, 0.3]\n"
            "      gameplay: [0.0, 0.0, 'bad', 1.0]\n",
            "entries must be numbers",
            id="gameplay-string-entry",
        ),
        pytest.param(
            "channels:\n  - login: demo\n    stacked_layout:\n"
            "      facecam: [0.0, 0.0, 0.25, 0.3]\n"
            "      facecam_fraction: true\n",
            "facecam_fraction.*must be a number",
            id="facecam-fraction-bool",
        ),
        pytest.param(
            "channels:\n  - login: demo\n    stacked_layout:\n"
            "      facecam: [0.0, 0.0, 0.25, 0.3]\n"
            "      facecam_fraction: '0.35'\n",
            "facecam_fraction.*must be a number",
            id="facecam-fraction-string",
        ),
    ],
)
def test_load_channel_stacked_layouts_rejects_invalid_yaml(
    tmp_path: Path, content: str, match: str
) -> None:
    path = tmp_path / "channels.yaml"
    path.write_text(content, encoding="utf-8")

    with pytest.raises(InvalidLayoutError, match=match):
        load_channel_stacked_layouts(path)
