import math

import pytest

from autoclipper.export.reframe import (
    MIN_CROP_PX,
    OUTPUT_HEIGHT,
    OUTPUT_WIDTH,
    CenterCrop,
    FixedRegion,
    InvalidLayoutError,
    PixelRect,
    ReframePlan,
    plan_reframe,
)


@pytest.mark.parametrize(
    ("source", "crop"),
    [
        ((1920, 1080), PixelRect(656, 0, 606, 1080)),  # landscape: sides cut
        ((1280, 720), PixelRect(438, 0, 404, 720)),
        ((720, 1280), PixelRect(0, 0, 720, 1280)),  # already 9:16
        ((300, 700), PixelRect(0, 84, 300, 532)),  # narrower than 9:16: top and bottom cut
        ((321, 181), PixelRect(110, 0, 100, 180)),  # odd size: even crop
        ((181, 321), PixelRect(0, 0, 180, 320)),  # odd portrait, exactly 9:16 after rounding
    ],
)
def test_center_crop_takes_the_largest_centered_9_16_area(
    source: tuple[int, int], crop: PixelRect
) -> None:
    plan = plan_reframe(CenterCrop(), *source)

    assert plan == ReframePlan(crop, OUTPUT_WIDTH, OUTPUT_HEIGHT)
    assert crop.width % 2 == 0
    assert crop.height % 2 == 0
    assert crop.x % 2 == 0
    assert crop.y % 2 == 0


def test_center_crop_filters_crop_then_scale_to_1080x1920() -> None:
    plan = plan_reframe(CenterCrop(), 1920, 1080)

    assert plan.filters() == (
        "crop=w=606:h=1080:x=656:y=0",
        "scale=w=1080:h=1920:flags=lanczos",
        "setsar=1",
    )


def test_fixed_region_is_padded_to_fit_by_default() -> None:
    # The top-left quarter of a 1920x1080 frame (960x540, 16:9) fills the width.
    plan = plan_reframe(FixedRegion(0.0, 0.0, 0.5, 0.5), 1920, 1080)

    assert plan == ReframePlan(PixelRect(0, 0, 960, 540), 1080, 608, pad_x=0, pad_y=656)
    assert plan.filters() == (
        "crop=w=960:h=540:x=0:y=0",
        "scale=w=1080:h=608:flags=lanczos",
        "pad=w=1080:h=1920:x=0:y=656:color=black",
        "setsar=1",
    )


def test_tall_fixed_region_is_padded_at_the_sides() -> None:
    plan = plan_reframe(FixedRegion(0.25, 0.0, 0.125, 1.0), 1920, 1080)

    assert plan == ReframePlan(PixelRect(480, 0, 240, 1080), 426, 1920, pad_x=326, pad_y=0)


def test_fixed_region_with_crop_fit_is_cropped_to_9_16_inside_the_region() -> None:
    plan = plan_reframe(FixedRegion(0.5, 0.5, 0.5, 0.5, fit="crop"), 1920, 1080)

    # 960x540 region at (960, 540); its centered 9:16 part is 302x540.
    assert plan == ReframePlan(PixelRect(1288, 540, 302, 540), OUTPUT_WIDTH, OUTPUT_HEIGHT)
    assert not any(f.startswith("pad=") for f in plan.filters())


def test_full_frame_fixed_region_with_crop_fit_equals_center_crop() -> None:
    assert plan_reframe(FixedRegion(0.0, 0.0, 1.0, 1.0, fit="crop"), 1280, 720) == plan_reframe(
        CenterCrop(), 1280, 720
    )


def test_fixed_region_may_overshoot_the_frame_edge_by_rounding_noise() -> None:
    # A caller's arithmetic can leave x + width a hair above 1.0; that is still the right edge.
    region = FixedRegion(0.75, 0.0, 0.25 + 1e-12, 1.0)
    assert region.x + region.width > 1.0

    plan = plan_reframe(region, 1920, 1080)

    assert plan.crop == PixelRect(1440, 0, 480, 1080)


@pytest.mark.parametrize(
    ("x", "y", "width", "height"),
    [
        (-0.1, 0.0, 0.5, 0.5),
        (0.0, -0.1, 0.5, 0.5),
        (0.0, 0.0, 0.0, 0.5),
        (0.0, 0.0, 0.5, -0.5),
        (0.6, 0.0, 0.5, 0.5),  # reaches past the right edge
        (0.0, 0.6, 0.5, 0.5),  # reaches past the bottom edge
        (0.0, 0.0, math.nan, 0.5),
        (0.0, 0.0, 0.5, math.inf),
        (0.0, 0.0, 640.0, 360.0),  # pixels instead of fractions
    ],
)
def test_fixed_region_outside_the_frame_is_rejected(
    x: float, y: float, width: float, height: float
) -> None:
    with pytest.raises(InvalidLayoutError):
        FixedRegion(x, y, width, height)


def test_unknown_fit_is_rejected() -> None:
    with pytest.raises(InvalidLayoutError, match="fit"):
        FixedRegion(0.0, 0.0, 1.0, 1.0, fit="stretch")  # pyright: ignore[reportArgumentType]


def test_region_too_small_for_the_source_is_rejected() -> None:
    with pytest.raises(InvalidLayoutError, match=f"at least {MIN_CROP_PX} px"):
        plan_reframe(FixedRegion(0.0, 0.0, 0.01, 0.5), 640, 360)


def test_source_smaller_than_the_minimum_crop_is_rejected() -> None:
    with pytest.raises(InvalidLayoutError, match="smaller than"):
        plan_reframe(CenterCrop(), 8, 8)


def test_layouts_have_stable_names() -> None:
    assert CenterCrop.name == "center_crop"
    assert FixedRegion.name == "fixed_region"
