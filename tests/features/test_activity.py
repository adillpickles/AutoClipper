"""Tests for the timeline's downsampled chat and loudness (#213)."""

import math

import polars as pl
import pytest

from autoclipper.analysis import MAX_ACTIVITY_POINTS
from autoclipper.features.activity import MAX_POINTS, activity_step_s, timeline_activity
from autoclipper.labels.windows import make_windows


def chat_frame(windows: pl.DataFrame, rates: list[float]) -> pl.DataFrame:
    return pl.DataFrame({"window_id": windows["window_id"], "chat_msg_rate": rates})


def audio_frame(
    windows: pl.DataFrame,
    rms: list[float],
    *,
    silence: list[float] | None = None,
    muted: list[float] | None = None,
) -> pl.DataFrame:
    n = windows.height
    return pl.DataFrame(
        {
            "window_id": windows["window_id"],
            "audio_rms_mean": rms,
            "audio_silence_frac": silence if silence is not None else [0.0] * n,
            "audio_is_muted": muted if muted is not None else [0.0] * n,
        }
    )


@pytest.mark.parametrize(
    ("duration_s", "step_s", "n_points"),
    [
        (5.0, 10.0, 1),
        (360.0, 10.0, 36),
        (3000.0, 10.0, 300),
        (3001.0, 20.0, 151),
        (7200.0, 30.0, 240),
        (6 * 3600.0, 80.0, 270),
        (12 * 3600.0, 150.0, 288),
    ],
)
def test_step_is_a_multiple_of_the_stride_within_max_points(
    duration_s: float, step_s: float, n_points: int
) -> None:
    assert activity_step_s(duration_s) == step_s
    assert math.ceil(duration_s / step_s) == n_points <= MAX_POINTS <= MAX_ACTIVITY_POINTS


def test_step_rejects_bad_inputs() -> None:
    with pytest.raises(ValueError, match="duration_s"):
        activity_step_s(0.0)
    with pytest.raises(ValueError, match="max_points"):
        activity_step_s(60.0, max_points=0)


def test_windows_average_into_points_with_rates_and_dbfs() -> None:
    windows = make_windows("v", 40.0)  # 4 windows
    activity = timeline_activity(
        windows,
        chat_frame(windows, [1.0, 3.0, 0.0, 0.5]),
        audio_frame(windows, [0.1, 0.1, 1.0, 0.001]),
        duration_s=40.0,
        max_points=2,
    )
    assert activity is not None
    assert activity.step_s == 20.0
    assert activity.chat == (2.0, 0.25)
    # mean RMS 0.1 is -20 dBFS; (1.0 + 0.001) / 2 is about -6 dBFS.
    assert activity.loudness == (-20.0, -6.0)


def test_a_missing_family_is_none_not_zeros() -> None:
    windows = make_windows("v", 30.0)
    only_audio = timeline_activity(windows, None, audio_frame(windows, [0.1] * 3), duration_s=30.0)
    assert only_audio is not None
    assert only_audio.chat is None
    assert only_audio.loudness == (-20.0, -20.0, -20.0)

    only_chat = timeline_activity(windows, chat_frame(windows, [0.0] * 3), None, duration_s=30.0)
    assert only_chat is not None
    assert only_chat.loudness is None
    assert only_chat.chat == (0.0, 0.0, 0.0)  # a quiet chat is a measured zero

    assert timeline_activity(windows, None, None, duration_s=30.0) is None


def test_muted_and_frameless_windows_are_gaps_but_silence_is_measured() -> None:
    windows = make_windows("v", 40.0)
    activity = timeline_activity(
        windows,
        None,
        audio_frame(
            windows,
            [0.1, 0.1, 0.0, 0.0],
            silence=[0.0, 0.0, 1.0, 0.0],  # window 2 is digital silence, window 3 has no frames
            muted=[0.0, 1.0, 0.0, 0.0],
        ),
        duration_s=40.0,
    )
    assert activity is not None
    assert activity.loudness == (-20.0, None, -60.0, None)


def test_a_point_without_windows_is_null() -> None:
    # A 44 s VOD: the last 4 s are shorter than half a window, so no window starts there.
    windows = make_windows("v", 44.0)
    assert windows.height == 4
    activity = timeline_activity(
        windows,
        chat_frame(windows, [1.0] * 4),
        audio_frame(windows, [0.1] * 4),
        duration_s=44.0,
    )
    assert activity is not None
    assert activity.chat == (1.0, 1.0, 1.0, 1.0, None)
    assert activity.loudness == (-20.0, -20.0, -20.0, -20.0, None)


def test_a_long_vod_stays_small() -> None:
    duration_s = 6 * 3600.0
    windows = make_windows("v", duration_s)
    activity = timeline_activity(
        windows,
        chat_frame(windows, [2.0] * windows.height),
        audio_frame(windows, [0.05] * windows.height),
        duration_s=duration_s,
    )
    assert activity is not None
    assert activity.step_s == 80.0
    assert activity.n_points == 270
    assert len(activity.model_dump_json()) < 6000
