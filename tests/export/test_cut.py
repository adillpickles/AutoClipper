import math

import pytest

from autoclipper.export.cut import (
    MIN_CLIP_DURATION_S,
    ContextPadding,
    Interval,
    IntervalTooShortError,
    InvalidIntervalError,
    plan_cut,
)
from autoclipper.export.errors import ExportError


@pytest.mark.parametrize(
    ("start_s", "end_s"),
    [
        (math.nan, 10.0),
        (0.0, math.inf),
        (-math.inf, 1.0),
        (-0.5, 10.0),
        (10.0, 10.0),
        (10.0, 9.0),
    ],
)
def test_interval_rejects_invalid_bounds(start_s: float, end_s: float) -> None:
    with pytest.raises(InvalidIntervalError):
        Interval(start_s, end_s)


def test_invalid_interval_is_an_export_error_and_a_value_error() -> None:
    assert issubclass(InvalidIntervalError, ExportError)
    assert issubclass(InvalidIntervalError, ValueError)


@pytest.mark.parametrize(("before_s", "after_s"), [(-1.0, 0.0), (0.0, -0.1), (math.nan, 0.0)])
def test_padding_rejects_negative_or_non_finite_values(before_s: float, after_s: float) -> None:
    with pytest.raises(InvalidIntervalError):
        ContextPadding(before_s, after_s)


def test_requested_interval_is_exported_unchanged_without_any_lag_shift() -> None:
    # Pins docs/data/labeling.md §5: export applies no chat-lag shift, so a source-time interval
    # inside the media comes out exactly as requested.
    requested = Interval(100.25, 130.75)

    plan = plan_cut(requested, 3600.0)

    assert plan.actual == requested
    assert plan.requested == requested
    assert plan.padding == ContextPadding(0.0, 0.0)
    assert not plan.clamped_start
    assert not plan.clamped_end


def test_padding_widens_both_sides_and_is_reported_separately() -> None:
    requested = Interval(100.0, 130.0)

    plan = plan_cut(requested, 3600.0, padding=ContextPadding(before_s=3.0, after_s=5.0))

    assert plan.actual == Interval(97.0, 135.0)
    assert plan.requested == requested
    assert plan.padding == ContextPadding(3.0, 5.0)


def test_padding_is_applied_before_clamping_to_the_media() -> None:
    plan = plan_cut(Interval(2.0, 58.0), 60.0, padding=ContextPadding(5.0, 5.0))

    assert plan.actual == Interval(0.0, 60.0)
    assert plan.clamped_start
    assert plan.clamped_end


def test_interval_past_the_end_is_clamped_to_the_media_duration() -> None:
    plan = plan_cut(Interval(50.0, 70.0), 60.0)

    assert plan.actual == Interval(50.0, 60.0)
    assert not plan.clamped_start
    assert plan.clamped_end


@pytest.mark.parametrize(
    ("requested", "message"),
    [
        (Interval(60.0, 70.0), "starts at or after the end"),  # starts exactly at the end
        (Interval(65.0, 70.0), "starts at or after the end"),  # entirely after the end
        (Interval(59.8, 70.0), "leaves 0.2 s"),  # 0.2 s left inside the media
        (Interval(10.0, 10.2), "needs at least 0.5 s"),  # inside, but shorter than the minimum
    ],
)
def test_too_little_left_after_clamping_raises(requested: Interval, message: str) -> None:
    with pytest.raises(IntervalTooShortError, match=message):
        plan_cut(requested, 60.0)


def test_padding_does_not_rescue_an_interval_after_the_media_end() -> None:
    # Padding reaches back into the media, but the clip would hold none of the request.
    with pytest.raises(IntervalTooShortError, match="starts at or after the end"):
        plan_cut(Interval(61.0, 70.0), 60.0, padding=ContextPadding(before_s=5.0))


def test_padding_can_lengthen_a_short_remainder_at_the_media_end() -> None:
    # Only 0.2 s of the request is inside the media, but with its padding the clip is 2.2 s.
    plan = plan_cut(Interval(59.8, 70.0), 60.0, padding=ContextPadding(before_s=2.0))

    assert plan.actual == Interval(57.8, 60.0)


def test_minimum_duration_is_exactly_allowed() -> None:
    plan = plan_cut(Interval(0.0, MIN_CLIP_DURATION_S), 60.0)

    assert plan.actual.duration_s == MIN_CLIP_DURATION_S


def test_minimum_duration_is_a_parameter() -> None:
    plan = plan_cut(Interval(10.0, 10.2), 60.0, min_duration_s=0.1)

    assert plan.actual == Interval(10.0, 10.2)


@pytest.mark.parametrize("duration_s", [0.0, -1.0, math.nan, math.inf])
def test_invalid_media_duration_raises(duration_s: float) -> None:
    with pytest.raises(InvalidIntervalError, match="media duration"):
        plan_cut(Interval(0.0, 1.0), duration_s)
