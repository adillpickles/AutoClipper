"""The one export pipeline behind ``autoclipper export`` and ``POST /api/v1/exports``.

`render_clip` takes what a user asks for: a source file, an interval, a layout name from the
API contract and whether to burn in captions. It turns that into an exact, unpadded export
(docs/architecture/api-contract.md § 6.1). With captions on, it runs
`autoclipper.export.captions`; otherwise it runs plain `autoclipper.export.encode.export_clip`.
It reports its stages (§ 6.3) through `on_stage`, so the API can show progress, and it refuses
an export that couldn't cover exactly the requested interval instead of quietly delivering a
shorter clip.

The ``stacked`` layout picks its facecam region in this order (#191): an explicit region
(``--facecam-region``, ``AUTOCLIPPER_EXPORT_FACECAM_REGION``, or a `StackedLayout` the caller
passes), then the overlay `autoclipper.export.facecam` detects in the clip, then the channel
preset (`ClipSpec.stacked_fallback`). With none of them it renders ``center_crop`` and warns.
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
from autoclipper.export.facecam import FacecamDetection, detect_facecam
from autoclipper.export.reframe import (
    DEFAULT_FACECAM_FRACTION,
    CenterCrop,
    FixedRegion,
    Layout,
    StackedLayout,
    layout_from_settings,
    stacked_layout_from_region,
)
from autoclipper.ffmpeg import Runner

log = logging.getLogger(__name__)

type LayoutName = Literal["center_crop", "configured_region", "stacked"]

LAYOUT_NAMES: Final[tuple[LayoutName, ...]] = ("center_crop", "configured_region", "stacked")
# The longest clip (api-contract.md § 5 `max_duration_s`, labeling.md § 2 export preset).
MAX_CLIP_DURATION_S: Final = 90.0

# Finds the facecam overlay in a source over an interval (file times); tests pass a fake.
type FacecamDetector = Callable[[Path, Interval], FacecamDetection]


class ClipTooLongError(ExportError):
    """The interval is longer than `MAX_CLIP_DURATION_S`."""


class SourceMissingError(ExportError):
    """The source file doesn't exist (or isn't a file)."""


class NoRegionConfiguredError(ExportError):
    """``configured_region`` was asked for, but ``AUTOCLIPPER_EXPORT_REGION`` isn't set."""


class NoFacecamConfiguredError(ExportError):
    """``stacked`` was asked for, but no facecam region or channel preset is configured (#68)."""


class IntervalOutsideMediaError(ExportError):
    """The source's video and audio don't cover the whole interval, so the clip can't be exact."""


@dataclass(frozen=True, slots=True)
class ClipSpec:
    """One clip to render.

    Attributes:
        source: The video file; it must start at VOD time 0 (times are file times).
        interval: The interval to export, exactly as the user trimmed it.
        output: Where to write the MP4.
        layout: ``center_crop``, ``configured_region``, or ``stacked`` (#68).
        captions: Burn in captions of the speech.
        overwrite: Replace an existing file at `output`.
        stacked_layout: An explicit `StackedLayout` for ``stacked``, used as is (no detection).
        stacked_fallback: A `StackedLayout` for ``stacked`` used only when facecam detection
            finds no face (a channel preset, #191).
        detect_facecam: Look for the facecam in the clip for ``stacked`` when no explicit
            layout is given.
    """

    source: Path
    interval: Interval
    output: Path
    layout: LayoutName = "center_crop"
    captions: bool = False
    overwrite: bool = False
    stacked_layout: StackedLayout | None = None
    stacked_fallback: StackedLayout | None = None
    detect_facecam: bool = True


def available_layouts(
    region: tuple[float, float, float, float] | None,
    *,
    facecam_region: tuple[float, float, float, float] | None = None,
    stacked_layout: StackedLayout | None = None,
    facecam_detection: bool = False,
) -> tuple[LayoutName, ...]:
    """The layouts that can be rendered given the configured regions and channel preset.

    With `facecam_detection`, ``stacked`` is always offered: `render_clip` finds the facecam
    in the clip or falls back to a preset or ``center_crop`` (#191).
    """
    layouts: list[LayoutName] = ["center_crop"]
    if region is not None:
        layouts.append("configured_region")
    if facecam_detection or stacked_layout is not None or facecam_region is not None:
        layouts.append("stacked")
    return tuple(layouts)


def layout_named(
    name: LayoutName,
    region: tuple[float, float, float, float] | None,
    *,
    facecam_region: tuple[float, float, float, float] | None = None,
    stacked_layout: StackedLayout | None = None,
) -> Layout:
    """The `Layout` behind a contract layout name.

    Raises:
        NoRegionConfiguredError: ``configured_region`` without a configured region.
        NoFacecamConfiguredError: ``stacked`` without a facecam region or channel preset.
        InvalidLayoutError: A configured region is out of bounds.
    """
    if name == "center_crop":
        return CenterCrop()
    if name == "stacked":
        if stacked_layout is not None:
            return stacked_layout
        if facecam_region is not None:
            return stacked_layout_from_region(facecam_region)
        raise NoFacecamConfiguredError(
            "the stacked layout needs AUTOCLIPPER_EXPORT_FACECAM_REGION (x,y,width,height "
            "as fractions of the frame), --facecam-region, or a channel preset in "
            "configs/channels.yaml"
        )
    if region is None:
        raise NoRegionConfiguredError(
            "the configured_region layout needs AUTOCLIPPER_EXPORT_REGION (x,y,width,height "
            "as fractions of the frame)"
        )
    return layout_from_settings(region)


def _detected_stacked_layout(
    spec: ClipSpec,
    detector: FacecamDetector,
    on_warning: Callable[[str], None] | None,
) -> Layout:
    """The stacked layout from facecam detection, else the fallback preset, else center crop.

    A detected overlay (``found``) beats the preset. A box estimated around a borderless
    webcam (``found_padded``) loses to it when the face's center is inside the preset's facecam
    region, and wins (with a warning) when the face is elsewhere. A full-screen camera renders
    ``center_crop`` even with a preset. Any error while detecting (FFmpeg, OpenCV missing or
    failing, the file vanishing) is logged and treated as no detection.

    Raises:
        NoFacecamConfiguredError: Detection is off and there is no fallback preset.
    """

    def warn(message: str) -> None:
        log.warning("stacked export source=%s: %s", spec.source, message)
        if on_warning is not None:
            on_warning(message)

    if not spec.detect_facecam:
        if spec.stacked_fallback is not None:
            return spec.stacked_fallback
        return layout_named("stacked", None)
    preset = spec.stacked_fallback
    try:
        detection = detector(spec.source, spec.interval)
    except Exception as exc:  # detection is best effort; it must never fail the render
        log.warning("facecam detection failed source=%s", spec.source, exc_info=True)
        reason = f"facecam detection failed ({type(exc).__name__}: {exc})"
        if preset is not None:
            warn(f"{reason}; using the channel's stacked_layout preset")
            return preset
        warn(f"{reason}; rendering center_crop instead of stacked")
        return CenterCrop()
    if detection.status == "found" and detection.region is not None:
        return _stacked_from_detection(detection.region, preset)
    if detection.status == "found_padded" and detection.region is not None:
        # A box estimated around a borderless webcam is a guess; a measured preset is better,
        # but only when the face is in it. Streamers move the camera between scenes.
        if preset is not None and _face_in(detection.face, preset.facecam):
            return preset
        if preset is not None:
            message = (
                f"{detection.message}; the face isn't in the channel's stacked_layout preset, "
                "so using that estimate"
            )
        else:
            message = f"{detection.message}; using that estimate"
        warn(message)
        return _stacked_from_detection(detection.region, None)
    reason = f"no facecam detected: {detection.message}"
    if preset is not None and detection.status != "full_camera":
        warn(f"{reason}; using the channel's stacked_layout preset")
        return preset
    warn(f"{reason}; rendering center_crop instead of stacked")
    return CenterCrop()


def _face_in(face: tuple[float, float, float, float] | None, region: FixedRegion) -> bool:
    """Whether the center of `face` (normalized) lies inside `region`."""
    if face is None:
        return False
    cx, cy = face[0] + face[2] / 2, face[1] + face[3] / 2
    return region.x <= cx <= region.x + region.width and region.y <= cy <= region.y + region.height


def _stacked_from_detection(
    region: tuple[float, float, float, float], preset: StackedLayout | None
) -> StackedLayout:
    """The detected facecam over the preset's gameplay region and pane split, if any."""
    return stacked_layout_from_region(
        region,
        gameplay_region=None if preset is None else _region_tuple(preset),
        facecam_fraction=DEFAULT_FACECAM_FRACTION if preset is None else preset.facecam_fraction,
    )


def _region_tuple(layout: StackedLayout) -> tuple[float, float, float, float]:
    g = layout.gameplay
    return (g.x, g.y, g.width, g.height)


def render_clip(
    spec: ClipSpec,
    *,
    region: tuple[float, float, float, float] | None = None,
    facecam_region: tuple[float, float, float, float] | None = None,
    stacked_layout: StackedLayout | None = None,
    transcriber: Transcriber | None = None,
    on_stage: Callable[[Stage], None] | None = None,
    max_duration_s: float = MAX_CLIP_DURATION_S,
    settings: EncodeSettings | None = None,
    runner: Runner | None = None,
    facecam_detector: FacecamDetector | None = None,
    on_warning: Callable[[str], None] | None = None,
) -> ExportResult:
    """Render `spec` as a 1080x1920 H.264/AAC MP4 covering exactly its interval.

    Args:
        spec: What to render.
        region: ``Settings.export_region``, for the ``configured_region`` layout.
        facecam_region: ``Settings.export_facecam_region``, for the ``stacked`` layout (#68).
        stacked_layout: An explicit `StackedLayout` for ``stacked``, used as is.
        transcriber: For captions; defaults to faster-whisper with the configured model.
        on_stage: Called with ``transcribing`` when transcription starts (captions only) and
            ``rendering`` when cutting and encoding starts.
        max_duration_s: The longest interval allowed.
        settings: Encoder settings.
        runner: Executes FFmpeg and FFprobe.
        facecam_detector: Finds the facecam for ``stacked`` (module docs); defaults to
            `detect_facecam` with the process-wide cache.
        on_warning: Called with a sentence when ``stacked`` falls back to the channel preset or
            to ``center_crop``; the fallback is also logged.

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
    explicit = spec.stacked_layout or stacked_layout
    if spec.layout == "stacked" and explicit is None and facecam_region is None:
        detector = facecam_detector or (
            lambda source, interval: detect_facecam(source, interval, runner=runner)
        )
        layout = _detected_stacked_layout(spec, detector, on_warning)
    else:
        layout = layout_named(
            spec.layout, region, facecam_region=facecam_region, stacked_layout=explicit
        )
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
