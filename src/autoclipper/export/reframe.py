"""Turn a source frame into a 1080x1920 (9:16) frame: the export layouts.

A layout says which part of the source frame to show. `plan_reframe` turns it into exact pixel
geometry for one source size (computed here, not with FFmpeg expressions, so the numbers can be
tested and reported), and `ReframePlan.filters` / `StackedReframePlan.filter_complex` turn that
into FFmpeg video filters.

Layouts:

- `CenterCrop` (``"center_crop"``): the largest 9:16 area around the center of the frame.
  Landscape sources lose their sides; sources narrower than 9:16 lose their top and bottom.
- `FixedRegion` (``"configured_region"`` in the API contract): a rectangle given in normalized
  source coordinates (0-1 of the width and height), e.g. a facecam corner or the game area. A
  region that isn't 9:16 is scaled to fit and padded with black bars by default (``fit="pad"``),
  so everything the caller chose stays visible; ``fit="crop"`` instead crops it to 9:16 around
  its center, which is what the configured region (`layout_from_settings`) uses.
- `StackedLayout` (``"stacked"`` in the API contract, #68): stacks a facecam region on top and
  a gameplay region below inside 1080x1920, cropping each pane to fill its share of the frame.

Pixel geometry uses the stored frame size from `probe`. Crop offsets and sizes are rounded down to
even numbers, which yuv420p needs, so odd source sizes (e.g. 321x181) lose at most one pixel row or
column. Sources are assumed to have square pixels and no rotation metadata, as Twitch VODs do;
the encoder tells FFmpeg not to apply rotation so its frames match the probed size.
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar, Final, Literal, overload

import yaml

from autoclipper.export.errors import ExportError

OUTPUT_WIDTH = 1080
OUTPUT_HEIGHT = 1920

# The smallest crop, in source pixels, on either side. Smaller regions are almost certainly a
# mistake (a region given in pixels instead of 0-1, or a tiny source) and would scale to mush.
MIN_CROP_PX = 16

DEFAULT_FACECAM_FRACTION: Final = 0.35
MIN_FACECAM_FRACTION: Final = 0.15
MAX_FACECAM_FRACTION: Final = 0.70

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

    name: ClassVar[str] = "configured_region"

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


def _default_gameplay() -> FixedRegion:
    return FixedRegion(0.0, 0.0, 1.0, 1.0, fit="crop")


@dataclass(frozen=True, slots=True)
class StackedLayout:
    """Stack a facecam region above a gameplay region inside a 1080x1920 frame (#68).

    Attributes:
        facecam: Normalized region of the streamer's facecam (placed in the top pane).
        gameplay: Normalized region of the gameplay area (placed in the bottom pane). Defaults
            to the full frame cropped to fill the bottom pane.
        facecam_fraction: Fraction of the 1920 px output height reserved for the top facecam
            pane (default 0.35 -> 672 px facecam over 1248 px gameplay).
    """

    name: ClassVar[str] = "stacked"

    facecam: FixedRegion
    gameplay: FixedRegion = field(default_factory=_default_gameplay)
    facecam_fraction: float = DEFAULT_FACECAM_FRACTION

    def __post_init__(self) -> None:
        if not math.isfinite(self.facecam_fraction) or not (
            MIN_FACECAM_FRACTION <= self.facecam_fraction <= MAX_FACECAM_FRACTION
        ):
            raise InvalidLayoutError(
                f"facecam_fraction must be between {MIN_FACECAM_FRACTION} and "
                f"{MAX_FACECAM_FRACTION}, got {self.facecam_fraction!r}"
            )


type Layout = CenterCrop | FixedRegion | StackedLayout


def layout_from_settings(region: tuple[float, float, float, float] | None) -> Layout:
    """The layout set in config: `CenterCrop` for None, otherwise `FixedRegion` cropped to 9:16.

    The API contract's ``configured_region`` is a 9:16 source region, so a configured region of
    another shape is cropped to 9:16 around its center (``fit="crop"``), never letterboxed.

    Args:
        region: ``Settings.export_region`` (``AUTOCLIPPER_EXPORT_REGION``): x, y, width and
            height as fractions of the source frame.

    Raises:
        InvalidLayoutError: The region is out of bounds (see `FixedRegion`).
    """
    if region is None:
        return CenterCrop()
    x, y, width, height = region
    return FixedRegion(x, y, width, height, fit="crop")


def stacked_layout_from_region(
    facecam_region: tuple[float, float, float, float],
    *,
    gameplay_region: tuple[float, float, float, float] | None = None,
    facecam_fraction: float = DEFAULT_FACECAM_FRACTION,
) -> StackedLayout:
    """Build a `StackedLayout` from normalized ``(x, y, width, height)`` tuples (#68)."""
    fx, fy, fw, fh = facecam_region
    facecam = FixedRegion(fx, fy, fw, fh, fit="crop")
    gameplay = (
        _default_gameplay()
        if gameplay_region is None
        else FixedRegion(*gameplay_region, fit="crop")
    )
    return StackedLayout(facecam=facecam, gameplay=gameplay, facecam_fraction=facecam_fraction)


def load_channel_stacked_layouts(channels_path: Path) -> dict[str, StackedLayout]:
    """Read per-channel `stacked_layout` presets from a ``channels.yaml`` file (#68).

    Keys are lowercased channel logins (and optional ``broadcaster_id`` values when present).
    Channels without a ``stacked_layout`` block are skipped. Returns an empty dict if
    `channels_path` does not exist.
    """
    if not channels_path.is_file():
        return {}
    raw: object = yaml.safe_load(channels_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise InvalidLayoutError(f"{channels_path} must be a YAML mapping")
    entries = raw.get("channels")
    if not isinstance(entries, list):
        return {}
    layouts: dict[str, StackedLayout] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        login = entry.get("login")
        cfg = entry.get("stacked_layout")
        if not isinstance(login, str) or not login.strip() or cfg is None:
            continue
        if not isinstance(cfg, dict):
            raise InvalidLayoutError(f"{channels_path}: stacked_layout for {login!r} must be a map")
        facecam = _parse_yaml_region(cfg.get("facecam"), f"{login}.stacked_layout.facecam")
        gameplay_raw = cfg.get("gameplay")
        gameplay = (
            None
            if gameplay_raw is None
            else _parse_yaml_region(gameplay_raw, f"{login}.stacked_layout.gameplay")
        )
        fraction_raw = cfg.get("facecam_fraction", DEFAULT_FACECAM_FRACTION)
        if isinstance(fraction_raw, bool) or not isinstance(fraction_raw, int | float):
            raise InvalidLayoutError(
                f"{channels_path}: facecam_fraction for {login!r} must be a number"
            )
        stacked = stacked_layout_from_region(
            facecam, gameplay_region=gameplay, facecam_fraction=float(fraction_raw)
        )
        layouts[login.strip().lower()] = stacked
        broadcaster_id = entry.get("broadcaster_id")
        if isinstance(broadcaster_id, str) and broadcaster_id.strip():
            layouts[broadcaster_id.strip()] = stacked
    return layouts


def stacked_layout_for_channel(channel: str, *, channels_path: Path) -> StackedLayout | None:
    """Return the configured `StackedLayout` for `channel` (case-insensitive), or None."""
    return load_channel_stacked_layouts(channels_path).get(channel.strip().lower())


def _parse_yaml_region(value: object, where: str) -> tuple[float, float, float, float]:
    if not isinstance(value, list) or len(value) != 4:
        raise InvalidLayoutError(f"{where} must be [x, y, width, height]")
    nums: list[float] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int | float):
            raise InvalidLayoutError(f"{where} entries must be numbers")
        nums.append(float(item))
    return (nums[0], nums[1], nums[2], nums[3])


@dataclass(frozen=True, slots=True)
class PixelRect:
    """A rectangle in source pixels, from the top-left corner."""

    x: int
    y: int
    width: int
    height: int


@dataclass(frozen=True, slots=True)
class ReframePlan:
    """Exact geometry that turns a source frame into a fixed-size output frame.

    Attributes:
        crop: The part of the source frame that is shown, in source pixels.
        scaled_width: Width the crop is scaled to, in output pixels.
        scaled_height: Height the crop is scaled to, in output pixels.
        pad_x: Black columns left of the scaled crop (0 unless the plan pads).
        pad_y: Black rows above the scaled crop (0 unless the plan pads).
        output_width: The output frame width the plan targets.
        output_height: The output frame height the plan targets.
    """

    crop: PixelRect
    scaled_width: int
    scaled_height: int
    pad_x: int = 0
    pad_y: int = 0
    output_width: int = OUTPUT_WIDTH
    output_height: int = OUTPUT_HEIGHT

    def filters(self) -> tuple[str, ...]:
        """The FFmpeg video filters for this plan, in order, ending with square pixels."""
        c = self.crop
        filters = [
            f"crop=w={c.width}:h={c.height}:x={c.x}:y={c.y}",
            f"scale=w={self.scaled_width}:h={self.scaled_height}:flags=lanczos",
        ]
        if (self.scaled_width, self.scaled_height) != (self.output_width, self.output_height):
            filters.append(
                f"pad=w={self.output_width}:h={self.output_height}:x={self.pad_x}:y="
                f"{self.pad_y}:color=black"
            )
        filters.append("setsar=1")
        return tuple(filters)


@dataclass(frozen=True, slots=True)
class StackedReframePlan:
    """Exact geometry that stacks a facecam pane above a gameplay pane in 1080x1920 (#68)."""

    top: ReframePlan
    bottom: ReframePlan
    output_width: int = OUTPUT_WIDTH
    output_height: int = OUTPUT_HEIGHT

    def filter_complex(
        self,
        input_stream: str,
        pre_filters: Sequence[str],
        post_filters: Sequence[str],
        output_label: str = "ac_vout",
    ) -> str:
        """Build an FFmpeg ``-filter_complex`` graph that splits, crops, scales, and vstacks."""
        pre = ",".join((*pre_filters, "split=2"))
        top_chain = ",".join(self.top.filters())
        bot_chain = ",".join(self.bottom.filters())
        tail = ",".join(("vstack=inputs=2", *post_filters))
        return (
            f"[{input_stream}]{pre}[ac_top_in][ac_bot_in];"
            f"[ac_top_in]{top_chain}[ac_top];"
            f"[ac_bot_in]{bot_chain}[ac_bot];"
            f"[ac_top][ac_bot]{tail}[{output_label}]"
        )


type AnyReframePlan = ReframePlan | StackedReframePlan


@overload
def plan_reframe(
    layout: CenterCrop | FixedRegion, source_width: int, source_height: int
) -> ReframePlan: ...


@overload
def plan_reframe(
    layout: StackedLayout, source_width: int, source_height: int
) -> StackedReframePlan: ...


@overload
def plan_reframe(layout: Layout, source_width: int, source_height: int) -> AnyReframePlan: ...


def plan_reframe(layout: Layout, source_width: int, source_height: int) -> AnyReframePlan:
    """Compute the crop, scale and padding that show `layout` in a 1080x1920 frame.

    Args:
        layout: What part of the source to show.
        source_width: Source frame width in pixels, as `probe` reports it.
        source_height: Source frame height in pixels, as `probe` reports it.

    Returns:
        `ReframePlan` for single-region layouts, or `StackedReframePlan` for `StackedLayout`.

    Raises:
        InvalidLayoutError: The source, or a layout region of it, is smaller than
            `MIN_CROP_PX` on a side.
    """
    if source_width < MIN_CROP_PX or source_height < MIN_CROP_PX:
        raise InvalidLayoutError(
            f"the source frame {source_width}x{source_height} is smaller than "
            f"{MIN_CROP_PX} px on a side"
        )
    if isinstance(layout, StackedLayout):
        top_h = _even(round(OUTPUT_HEIGHT * layout.facecam_fraction))
        bot_h = OUTPUT_HEIGHT - top_h
        top = _plan_region(layout.facecam, source_width, source_height, OUTPUT_WIDTH, top_h)
        bottom = _plan_region(layout.gameplay, source_width, source_height, OUTPUT_WIDTH, bot_h)
        return StackedReframePlan(
            top=top, bottom=bottom, output_width=OUTPUT_WIDTH, output_height=OUTPUT_HEIGHT
        )
    if isinstance(layout, CenterCrop):
        region = PixelRect(0, 0, _even(source_width), _even(source_height))
        return ReframePlan(
            _crop_to_aspect(region, OUTPUT_WIDTH, OUTPUT_HEIGHT), OUTPUT_WIDTH, OUTPUT_HEIGHT
        )
    return _plan_region(layout, source_width, source_height, OUTPUT_WIDTH, OUTPUT_HEIGHT)


def _plan_region(
    region: FixedRegion, source_width: int, source_height: int, out_width: int, out_height: int
) -> ReframePlan:
    rect = _region_pixels(region, source_width, source_height)
    if region.fit == "crop":
        return ReframePlan(
            _crop_to_aspect(rect, out_width, out_height),
            out_width,
            out_height,
            output_width=out_width,
            output_height=out_height,
        )
    return _fit_with_padding(rect, out_width, out_height)


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


def _crop_to_aspect(rect: PixelRect, target_width: int, target_height: int) -> PixelRect:
    """The largest even-sized rectangle with ``target_width:target_height`` centered in `rect`."""
    if rect.width * target_height > rect.height * target_width:
        width = max(2, _even(rect.height * target_width // target_height))
        x = rect.x + _even((rect.width - width) // 2)
        return PixelRect(x, rect.y, width, rect.height)
    height = max(2, _even(rect.width * target_height // target_width))
    y = rect.y + _even((rect.height - height) // 2)
    return PixelRect(rect.x, y, rect.width, height)


def _fit_with_padding(
    rect: PixelRect, out_width: int = OUTPUT_WIDTH, out_height: int = OUTPUT_HEIGHT
) -> ReframePlan:
    """Scale `rect` to fit inside ``out_width x out_height`` and center it on black."""
    if rect.width * out_height >= rect.height * out_width:
        width = out_width
        height = min(out_height, max(2, _even(round(out_width * rect.height / rect.width))))
    else:
        height = out_height
        width = min(out_width, max(2, _even(round(out_height * rect.width / rect.height))))
    return ReframePlan(
        rect,
        width,
        height,
        pad_x=_even((out_width - width) // 2),
        pad_y=_even((out_height - height) // 2),
        output_width=out_width,
        output_height=out_height,
    )
