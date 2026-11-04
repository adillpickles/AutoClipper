"""The one export pipeline behind ``autoclipper export`` and ``POST /api/v1/exports``.

`render_clip` takes what a user asks for: a source file, an interval, a layout name from the
API contract and whether to burn in captions. It turns that into an exact, unpadded export
(docs/architecture/api-contract.md § 6.1). With captions on, it runs
`autoclipper.export.captions`; otherwise it runs plain `autoclipper.export.encode.export_clip`.
It reports its stages (§ 6.3) through `on_stage`, so the API can show progress, and it refuses
an export that couldn't cover exactly the requested interval instead of quietly delivering a
shorter clip.
"""

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from autoclipper.export.captions import Stage, Transcriber, export_clip_with_captions
from autoclipper.export.cut import Interval
from autoclipper.export.encode import EncodeSettings, ExportRequest, ExportResult, export_clip
from autoclipper.export.errors import ExportError
from autoclipper.export.reframe import CenterCrop, Layout, layout_from_settings
from autoclipper.ffmpeg import Runner

log = logging.getLogger(__name__)

type LayoutName = Literal["center_crop", "configured_region"]

LAYOUT_NAMES: Final[tuple[LayoutName, ...]] = ("center_crop", "configured_region")
# The longest clip (api-contract.md § 5 `max_duration_s`, labeling.md § 2 export preset).
MAX_CLIP_DURATION_S: Final = 90.0


class ClipTooLongError(ExportError):
    """The interval is longer than `MAX_CLIP_DURATION_S`."""


class SourceMissingError(ExportError):
    """The source file doesn't exist (or isn't a file)."""


class NoRegionConfiguredError(ExportError):
    """``configured_region`` was asked for, but ``AUTOCLIPPER_EXPORT_REGION`` isn't set."""


class IntervalOutsideMediaError(ExportError):
    """The source's video and audio don't cover the whole interval, so the clip can't be exact."""


@dataclass(frozen=True, slots=True)
class ClipSpec:
    """One clip to render.

    Attributes:
        source: The video file; it must start at VOD time 0 (times are file times).
        interval: The interval to export, exactly as the user trimmed it.
        output: Where to write the MP4.
        layout: ``center_crop`` or ``configured_region`` (the region set in config).
        captions: Burn in captions of the speech.
        overwrite: Replace an existing file at `output`.
    """

    source: Path
    interval: Interval
    output: Path
    layout: LayoutName = "center_crop"
    captions: bool = False
    overwrite: bool = False


def available_layouts(region: tuple[float, float, float, float] | None) -> tuple[LayoutName, ...]:
    """The layouts that can be rendered: ``configured_region`` only when a region is set."""
    return LAYOUT_NAMES if region is not None else ("center_crop",)


def layout_named(name: LayoutName, region: tuple[float, float, float, float] | None) -> Layout:
    """The `Layout` behind a contract layout name.

    Raises:
        NoRegionConfiguredError: ``configured_region`` without a configured region.
        InvalidLayoutError: The configured region is out of bounds.
    """
    if name == "center_crop":
        return CenterCrop()
    if region is None:
        raise NoRegionConfiguredError(
            "the configured_region layout needs AUTOCLIPPER_EXPORT_REGION (x,y,width,height "
            "as fractions of the frame)"
        )
    return layout_from_settings(region)


def render_clip(
    spec: ClipSpec,
    *,
    region: tuple[float, float, float, float] | None = None,
    transcriber: Transcriber | None = None,
    on_stage: Callable[[Stage], None] | None = None,
    max_duration_s: float = MAX_CLIP_DURATION_S,
    settings: EncodeSettings | None = None,
    runner: Runner | None = None,
) -> ExportResult:
    """Render `spec` as a 1080x1920 H.264/AAC MP4 covering exactly its interval.

    Args:
        spec: What to render.
        region: ``Settings.export_region``, for the ``configured_region`` layout.
        transcriber: For captions; defaults to faster-whisper with the configured model.
        on_stage: Called with ``transcribing`` when transcription starts (captions only) and
            ``rendering`` when cutting and encoding starts.
        max_duration_s: The longest interval allowed.
        settings: Encoder settings.
        runner: Executes FFmpeg and FFprobe.

    Raises:
        ClipTooLongError: The interval is longer than `max_duration_s`.
        SourceMissingError: `spec.source` isn't a file.
        NoRegionConfiguredError: ``configured_region`` without a region.
        IntervalOutsideMediaError: The media doesn't cover the interval to within one frame.
        CaptioningError: Captions were asked for and failed (nothing is written).
        ExportError, FFmpegError: As raised by `export_clip`.
    """
    interval = spec.interval
    if interval.duration_s > max_duration_s:
        raise ClipTooLongError(
            f"clips can be at most {max_duration_s:g} s long; this one is {interval.duration_s:g} s"
        )
    if not spec.source.is_file():
        raise SourceMissingError(f"video file not found: {spec.source}")
    layout = layout_named(spec.layout, region)
    request = ExportRequest(spec.source, interval, spec.output, layout, overwrite=spec.overwrite)
    tolerance_s = 1.0 / (settings or EncodeSettings()).frame_rate

    def report(stage: Stage) -> None:
        if on_stage is not None:
            on_stage(stage)

    def check_exact(actual: Interval) -> None:
        if (
            abs(actual.start_s - interval.start_s) > tolerance_s
            or abs(actual.end_s - interval.end_s) > tolerance_s
        ):
            raise IntervalOutsideMediaError(
                f"the video covers only {actual.start_s:.3f}-{actual.end_s:.3f} s of the "
                f"requested {interval.start_s:.3f}-{interval.end_s:.3f} s"
            )

    log.info(
        "render clip source=%s start_s=%.3f end_s=%.3f layout=%s captions=%s",
        spec.source,
        interval.start_s,
        interval.end_s,
        spec.layout,
        spec.captions,
    )
    if not spec.captions:

        def exact_only(actual: Interval) -> Sequence[str]:
            check_exact(actual)
            return ()

        report("rendering")
        return export_clip(request, settings=settings, filters_for_cut=exact_only, runner=runner)

    return export_clip_with_captions(
        request,
        transcriber=transcriber,
        settings=settings,
        runner=runner,
        check_cut=check_exact,
        on_stage=report,
    )
