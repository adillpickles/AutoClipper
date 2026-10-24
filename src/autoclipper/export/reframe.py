"""Turn a source frame into a 1080x1920 (9:16) frame: the export layouts.

A layout says which part of the source frame to show. `plan_reframe` turns it into exact pixel
geometry for one source size (computed here, not with FFmpeg expressions, so the numbers can be
tested and reported), and `ReframePlan.filters` turns that into FFmpeg video filters.

Layouts:

- `CenterCrop` (``"center_crop"``): the largest 9:16 area around the center of the frame.
  Landscape sources lose their sides; sources narrower than 9:16 lose their top and bottom.
- `FixedRegion` (``"fixed_region"``): a rectangle given in normalized source coordinates (0-1 of
  the width and height), e.g. a facecam corner or the game area. A region that isn't 9:16 is
  scaled to fit and padded with black bars by default (``fit="pad"``), so everything the caller
  chose stays visible; ``fit="crop"`` instead crops it to 9:16 around its center.

Pixel geometry uses the stored frame size from `probe`. Crop offsets and sizes are rounded down to
even numbers, which yuv420p needs, so odd source sizes (e.g. 321x181) lose at most one pixel row or
column. Sources are assumed to have square pixels and no rotation metadata, as Twitch VODs do;
the encoder tells FFmpeg not to apply rotation so its frames match the probed size.
"""

import math
from dataclasses import dataclass
from typing import ClassVar, Literal

from autoclipper.export.errors import ExportError

OUTPUT_WIDTH = 1080
OUTPUT_HEIGHT = 1920

# The smallest crop, in source pixels, on either side. Smaller regions are almost certainly a
# mistake (a region given in pixels instead of 0-1, or a tiny source) and would scale to mush.
MIN_CROP_PX = 16

# Allows normalized regions that end at 1.0 after floating-point arithmetic (0.7 + 0.3).
_EDGE_EPSILON = 1e-9

type Fit = Literal["pad", "crop"]
_FITS: frozenset[str] = frozenset({"pad", "crop"})


class InvalidLayoutError(ExportError, ValueError):
    """A layout's region is out of bounds, too small for the source, or has an unknown fit."""


@dataclass(frozen=True, slots=True)
class CenterCrop:
    """Show the largest 9:16 area around the center of the source frame."""

    name: ClassVar[str] = "center_crop"


@dataclass(frozen=True, slots=True)
class FixedRegion:
    """Show a fixed rectangle of the source frame.

    Coordinates are fractions of the source width (`x`, `width`) and height (`y`, `height`),
    measured from the top-left corner, so one region works for any source resolution.

    Attributes:
        x: Left edge, 0-1.
        y: Top edge, 0-1.
        width: Width, > 0; ``x + width`` must be <= 1.
        height: Height, > 0; ``y + height`` must be <= 1.
        fit: What happens when the region isn't 9:16. ``"pad"`` (default) scales the whole
            region to fit 1080x1920 and fills the rest with black; ``"crop"`` crops the region
            to 9:16 around its center, then scales it to fill the frame.

    Raises:
        InvalidLayoutError: A value is not finite, the region reaches outside 0-1, or `fit` is
            not ``"pad"`` or ``"crop"``.
    """

    name: ClassVar[str] = "fixed_region"

    x: float
    y: float
    width: float
    height: float
    fit: Fit = "pad"

    def __post_init__(self) -> None:
        values = {"x": self.x, "y": self.y, "width": self.width, "height": self.height}
        for key, value in values.items():
            if not math.isfinite(value):
                raise InvalidLayoutError(f"region {key} must be finite, got {value!r}")
        if self.x < 0 or self.y < 0 or self.width <= 0 or self.height <= 0:
            raise InvalidLayoutError(
                f"region needs x, y >= 0 and width, height > 0, got {self._describe()}"
            )
        if self.x + self.width > 1 + _EDGE_EPSILON or self.y + self.height > 1 + _EDGE_EPSILON:
            raise InvalidLayoutError(
                f"region must lie inside the frame (x + width <= 1, y + height <= 1), "
                f"got {self._describe()}"
            )
        if self.fit not in _FITS:
            raise InvalidLayoutError(f"fit must be 'pad' or 'crop', got {self.fit!r}")

    def _describe(self) -> str:
        return f"x={self.x!r} y={self.y!r} width={self.width!r} height={self.height!r}"


type Layout = CenterCrop | FixedRegion


@dataclass(frozen=True, slots=True)
class PixelRect:
    """A rectangle in source pixels, from the top-left corner."""

    x: int
    y: int
    width: int
    height: int


@dataclass(frozen=True, slots=True)
class ReframePlan:
    """Exact geometry that turns a source frame into an `OUTPUT_WIDTH` x `OUTPUT_HEIGHT` frame.

    Attributes:
        crop: The part of the source frame that is shown, in source pixels.
        scaled_width: Width the crop is scaled to, in output pixels.
        scaled_height: Height the crop is scaled to, in output pixels.
        pad_x: Black columns left of the scaled crop (0 unless the layout pads).
        pad_y: Black rows above the scaled crop (0 unless the layout pads).
    """

    crop: PixelRect
    scaled_width: int
    scaled_height: int
    pad_x: int = 0
    pad_y: int = 0

    def filters(self) -> tuple[str, ...]:
        """The FFmpeg video filters for this plan, in order, ending with square pixels."""
        c = self.crop
        filters = [
            f"crop=w={c.width}:h={c.height}:x={c.x}:y={c.y}",
            f"scale=w={self.scaled_width}:h={self.scaled_height}:flags=lanczos",
        ]
        if (self.scaled_width, self.scaled_height) != (OUTPUT_WIDTH, OUTPUT_HEIGHT):
            filters.append(
                f"pad=w={OUTPUT_WIDTH}:h={OUTPUT_HEIGHT}:x={self.pad_x}:y={self.pad_y}:color=black"
            )
        filters.append("setsar=1")
        return tuple(filters)


def plan_reframe(layout: Layout, source_width: int, source_height: int) -> ReframePlan:
    """Compute the crop, scale and padding that show `layout` in a 1080x1920 frame.

    Args:
        layout: What part of the source to show.
        source_width: Source frame width in pixels, as `probe` reports it.
        source_height: Source frame height in pixels, as `probe` reports it.

    Returns:
        The pixel geometry; pass `ReframePlan.filters` to FFmpeg.

    Raises:
        InvalidLayoutError: The source, or the layout's region of it, is smaller than
            `MIN_CROP_PX` on a side.
    """
    if source_width < MIN_CROP_PX or source_height < MIN_CROP_PX:
        raise InvalidLayoutError(
            f"the source frame {source_width}x{source_height} is smaller than "
            f"{MIN_CROP_PX} px on a side"
        )
    if isinstance(layout, CenterCrop):
        region = PixelRect(0, 0, _even(source_width), _even(source_height))
        fit: Fit = "crop"
    else:
        region = _region_pixels(layout, source_width, source_height)
        fit = layout.fit
    if fit == "crop":
        return ReframePlan(_crop_to_9_16(region), OUTPUT_WIDTH, OUTPUT_HEIGHT)
    return _fit_with_padding(region)


def _even(value: int) -> int:
    return value - value % 2


def _region_pixels(region: FixedRegion, source_width: int, source_height: int) -> PixelRect:
    left = _even(round(region.x * source_width))
    top = _even(round(region.y * source_height))
    right = min(source_width, round((region.x + region.width) * source_width))
    bottom = min(source_height, round((region.y + region.height) * source_height))
    rect = PixelRect(left, top, _even(right - left), _even(bottom - top))
    if rect.width < MIN_CROP_PX or rect.height < MIN_CROP_PX:
        raise InvalidLayoutError(
            f"region {region._describe()} is {max(rect.width, 0)}x{max(rect.height, 0)} px of the "
            f"{source_width}x{source_height} source; it needs at least {MIN_CROP_PX} px on a side"
        )
    return rect


def _crop_to_9_16(rect: PixelRect) -> PixelRect:
    """The largest even-sized 9:16 rectangle centered in `rect`."""
    if rect.width * OUTPUT_HEIGHT > rect.height * OUTPUT_WIDTH:  # wider than 9:16
        width = _even(rect.height * OUTPUT_WIDTH // OUTPUT_HEIGHT)
        x = rect.x + _even((rect.width - width) // 2)
        return PixelRect(x, rect.y, width, rect.height)
    height = _even(rect.width * OUTPUT_HEIGHT // OUTPUT_WIDTH)
    y = rect.y + _even((rect.height - height) // 2)
    return PixelRect(rect.x, y, rect.width, height)


def _fit_with_padding(rect: PixelRect) -> ReframePlan:
    """Scale `rect` to fit inside the output frame and center it on black."""
    if rect.width * OUTPUT_HEIGHT >= rect.height * OUTPUT_WIDTH:  # as wide as 9:16 or wider
        width = OUTPUT_WIDTH
        height = min(OUTPUT_HEIGHT, max(2, _even(round(OUTPUT_WIDTH * rect.height / rect.width))))
    else:
        height = OUTPUT_HEIGHT
        width = min(OUTPUT_WIDTH, max(2, _even(round(OUTPUT_HEIGHT * rect.width / rect.height))))
    return ReframePlan(
        rect,
        width,
        height,
        pad_x=_even((OUTPUT_WIDTH - width) // 2),
        pad_y=_even((OUTPUT_HEIGHT - height) // 2),
    )
