"""Choose the exact source interval an export covers.

Times are float seconds from VOD start (docs/development/conventions.md §3), and the source media
file is assumed to start at VOD offset 0. The requested interval is already in source time: chat
lag was handled once when candidates were made, and export never shifts it again
(docs/data/labeling.md §5). Context padding is a separate, explicit parameter. `plan_cut` widens
the requested interval by the padding, then clamps the result to the media, and keeps all three
(requested, padding, actual) so a caller can report them separately.
"""

import math
from dataclasses import dataclass

from autoclipper.export.errors import ExportError

# The shortest clip an export produces, in seconds. Anything shorter isn't a watchable clip and is
# almost certainly a bounds mistake (an interval that starts just before the media ends, times in
# the wrong unit), so it fails with `IntervalTooShortError` instead of producing a few frames.
MIN_CLIP_DURATION_S = 0.5


class InvalidIntervalError(ExportError, ValueError):
    """An interval or padding value is not finite, negative, or ends before it starts."""


class IntervalTooShortError(ExportError):
    """After padding and clamping to the media, less than the minimum duration remains.

    Raised when the requested interval starts at or after the media's end (padding doesn't
    change that), or when less than `MIN_CLIP_DURATION_S` of the padded interval lies inside
    the media.
    """


@dataclass(frozen=True, slots=True)
class Interval:
    """A span of source time, in seconds from VOD start.

    Attributes:
        start_s: Start, inclusive. Finite and >= 0.
        end_s: End, exclusive. Finite and > `start_s`.

    Raises:
        InvalidIntervalError: A bound is not finite, `start_s` is negative, or `end_s` is not
            after `start_s`.
    """

    start_s: float
    end_s: float

    def __post_init__(self) -> None:
        if not (math.isfinite(self.start_s) and math.isfinite(self.end_s)):
            raise InvalidIntervalError(
                f"interval bounds must be finite, got start_s={self.start_s!r} end_s={self.end_s!r}"
            )
        if self.start_s < 0:
            raise InvalidIntervalError(f"start_s must be >= 0, got {self.start_s!r}")
        if self.end_s <= self.start_s:
            raise InvalidIntervalError(
                f"end_s must be after start_s, got start_s={self.start_s!r} end_s={self.end_s!r}"
            )

    @property
    def duration_s(self) -> float:
        """Length of the interval in seconds."""
        return self.end_s - self.start_s


@dataclass(frozen=True, slots=True)
class ContextPadding:
    """Extra context added around the requested interval before clamping.

    Padding is never a lag correction: it widens both sides independently and is reported
    separately from the requested (trimmed) bounds.

    Attributes:
        before_s: Seconds added before the start. Finite and >= 0.
        after_s: Seconds added after the end. Finite and >= 0.

    Raises:
        InvalidIntervalError: A value is negative or not finite.
    """

    before_s: float = 0.0
    after_s: float = 0.0

    def __post_init__(self) -> None:
        for name, value in (("before_s", self.before_s), ("after_s", self.after_s)):
            if not (math.isfinite(value) and value >= 0):
                raise InvalidIntervalError(f"padding {name} must be finite and >= 0, got {value!r}")


@dataclass(frozen=True, slots=True)
class CutPlan:
    """The source interval an export covers, and how it was derived.

    Attributes:
        requested: The interval the caller asked for (the trim), unchanged.
        padding: The context padding that was applied.
        actual: The interval that is exported: `requested` widened by `padding`, then clamped
            to ``[0, media_duration_s]``.
        clamped_start: True when padding or the request reached before 0 and was clamped.
        clamped_end: True when padding or the request reached past the media's end and was
            clamped.
    """

    requested: Interval
    padding: ContextPadding
    actual: Interval
    clamped_start: bool
    clamped_end: bool


def plan_cut(
    requested: Interval,
    media_duration_s: float,
    *,
    padding: ContextPadding | None = None,
    min_duration_s: float = MIN_CLIP_DURATION_S,
) -> CutPlan:
    """Pad the requested interval, then clamp it to the media.

    No chat-lag shift is applied: `requested` is source time already (docs/data/labeling.md §5).

    Args:
        requested: The trimmed interval in source time.
        media_duration_s: Duration of the source media in seconds (from `autoclipper.ffmpeg.probe`).
        padding: Context to add on each side before clamping. None means no padding.
        min_duration_s: The shortest interval that may remain after clamping.

    Returns:
        The requested, padding and actual intervals.

    Raises:
        InvalidIntervalError: `media_duration_s` is not finite and positive.
        IntervalTooShortError: The requested interval starts at or after the media's end, or less
            than `min_duration_s` remains after padding and clamping.
    """
    if not (math.isfinite(media_duration_s) and media_duration_s > 0):
        raise InvalidIntervalError(
            f"media duration must be finite and > 0, got {media_duration_s!r}"
        )
    pad = ContextPadding() if padding is None else padding
    padded_start_s = requested.start_s - pad.before_s
    padded_end_s = requested.end_s + pad.after_s
    if requested.start_s >= media_duration_s:
        # Padding could reach back into the media, but the clip would hold none of the request.
        raise IntervalTooShortError(
            f"the interval {requested.start_s:g}-{requested.end_s:g} s starts at or after the end "
            f"of the media (0-{media_duration_s:g} s)"
        )
    start_s = max(0.0, padded_start_s)
    end_s = min(media_duration_s, padded_end_s)
    if end_s - start_s < min_duration_s:
        raise IntervalTooShortError(
            f"the interval {requested.start_s:g}-{requested.end_s:g} s (padded "
            f"{padded_start_s:g}-{padded_end_s:g} s) leaves {max(0.0, end_s - start_s):g} s inside "
            f"the media (0-{media_duration_s:g} s); an export needs at least {min_duration_s:g} s"
        )
    return CutPlan(
        requested=requested,
        padding=pad,
        actual=Interval(start_s, end_s),
        clamped_start=padded_start_s < 0.0,
        clamped_end=padded_end_s > media_duration_s,
    )
