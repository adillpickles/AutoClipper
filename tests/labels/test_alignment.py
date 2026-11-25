import math
from pathlib import Path

import numpy as np
import polars as pl
import pytest

from autoclipper.features.audio import compute_audio_features
from autoclipper.features.chat import compute_chat_features, load_lexicon
from autoclipper.labels import alignment as al
from autoclipper.labels.windows import make_windows
from autoclipper.synthetic import generate_dataset
from autoclipper.synthetic.scenario import build_scenario

LEXICON = Path(__file__).resolve().parents[2] / "configs" / "lexicon.yaml"


@pytest.fixture(scope="module")
def synthetic(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("alignment") / "synthetic"
    generate_dataset(root, seed=1)
    return root


def _vod(root: Path, video_id: str) -> tuple[float, pl.DataFrame, Path]:
    vods = pl.read_parquet(root / "processed" / "vods.parquet")
    duration = float(vods.filter(pl.col("video_id") == video_id)["duration_s"][0])
    messages = pl.read_parquet(root / "interim" / "chat" / f"{video_id}.parquet")
    return duration, messages, root / "interim" / "audio" / f"{video_id}.wav"


def test_detrend_removes_a_constant_and_keeps_a_bump() -> None:
    x = np.full(50, 4.0)
    assert np.allclose(al.detrend(x, 11), 0.0)
    bump = x.copy()
    bump[25] = 14.0
    out = al.detrend(bump, 11)
    assert out[25] == pytest.approx(10 - 10 / 11)
    assert al.detrend(np.zeros(0), 5).size == 0
    with pytest.raises(ValueError, match="width"):
        al.detrend(x, 0)


def test_standardize_gives_zeros_for_a_constant_series() -> None:
    assert np.array_equal(al.standardize(np.full(5, 3.0)), np.zeros(5))
    z = al.standardize(np.array([1.0, 2.0, 3.0]))
    assert z.mean() == pytest.approx(0.0)
    assert z.std() == pytest.approx(1.0)


@pytest.mark.parametrize("shift", [7, -4, 0])
def test_lagged_correlation_peaks_at_the_shift(shift: int) -> None:
    rng = np.random.default_rng(0)
    x = rng.normal(size=2000)
    y = np.roll(x, shift)  # y[t + shift] == x[t]
    lags = list(range(-10, 11))
    corr = al.lagged_correlation(x, y, lags)
    assert al.peak_lag(lags, corr) == shift
    assert corr[lags.index(shift)] == pytest.approx(1.0, abs=0.02)


def test_lagged_correlation_is_zero_for_too_little_overlap_or_constants() -> None:
    assert np.array_equal(al.lagged_correlation(np.ones(3), np.ones(3), [0, 2, 5]), np.zeros(3))
    with pytest.raises(ValueError, match="same length"):
        al.lagged_correlation(np.ones(3), np.ones(4), [0])


def test_peak_lag_takes_the_earliest_tie_and_checks_lengths() -> None:
    assert al.peak_lag([-1, 0, 1], np.array([0.5, 0.9, 0.9])) == 0
    with pytest.raises(ValueError, match="match"):
        al.peak_lag([0, 1], np.array([1.0]))


def test_event_profiles_index_by_the_anchors_bin_and_pad_with_nan() -> None:
    signal = np.arange(10, dtype=np.float64)
    rows = al.event_profiles(signal, [2.7, 9.0], [-3, -1, 0, 1])
    assert np.isnan(rows[0, 0])
    assert rows[0, 1:].tolist() == [1.0, 2.0, 3.0]
    assert rows[1, :3].tolist() == [6.0, 8.0, 9.0]
    assert np.isnan(rows[1, 3])


def test_mean_profile_ignores_nan_without_a_warning() -> None:
    rows = np.array([[1.0, np.nan, np.nan], [3.0, 4.0, np.nan]])
    out = al.mean_profile(rows)
    assert out[:2].tolist() == [2.0, 4.0]
    assert np.isnan(out[2])
    assert np.isnan(al.mean_profile(np.zeros((0, 2)))).all()


def test_profile_peak_searches_its_range_and_returns_none_without_values() -> None:
    offsets = [-2, -1, 0, 1, 2]
    profile = np.array([9.0, 1.0, 3.0, 3.0, np.nan])
    assert al.profile_peak(offsets, profile, (-1, 2)) == al.Peak(0, 3.0)
    assert al.profile_peak(offsets, np.full(5, np.nan), (-2, 2)) is None


def test_half_max_span_bounds_the_stretch_above_half_height() -> None:
    offsets = list(range(-4, 5))
    profile = np.array([0.0, 0.0, 0.0, 2.0, 4.0, 3.0, 1.0, 0.0, 0.0])
    peak = al.profile_peak(offsets, profile, (-4, 4))
    assert peak is not None
    assert peak == al.Peak(0, 4.0)
    assert al.half_max_span(offsets, profile, peak, (-4, -2)) == (-1, 1)
    assert al.half_max_span(offsets, profile, al.Peak(0, 0.0), (-4, -2)) is None
    assert al.half_max_span(offsets, np.full(9, np.nan), peak, (-4, -2)) is None


def test_smooth_averages_neighbours_and_ignores_nan() -> None:
    out = al.smooth(np.array([0.0, 3.0, 0.0, np.nan, 6.0]), 3)
    assert out.tolist() == pytest.approx([1.5, 1.0, 1.5, 3.0, 6.0])
    assert np.isnan(al.smooth(np.full(3, np.nan), 3)).all()
    with pytest.raises(ValueError, match="width"):
        al.smooth(np.zeros(3), 0)


def test_event_times_keep_the_loudest_separate_seconds() -> None:
    signal = np.zeros(200)
    signal[[20, 25, 100, 180]] = [5.0, 6.0, 4.0, 2.0]
    times = al.event_times(signal, threshold=3.0, suppress_s=30.0)
    assert times.tolist() == [25.0, 100.0]


def test_bootstrap_statistic_is_repeatable_and_skips_missing_values() -> None:
    data = np.array([1.0, 2.0, 3.0, 10.0])

    def stat(idx: np.ndarray) -> float:
        return float(np.median(data[idx]))

    one = al.bootstrap_statistic(4, stat, seed=3, n_resamples=200)
    assert one == al.bootstrap_statistic(4, stat, seed=3, n_resamples=200)
    assert one is not None
    assert one[0] <= np.median(data) <= one[1]
    assert al.bootstrap_statistic(0, stat, seed=3, n_resamples=10) is None
    assert al.bootstrap_statistic(3, lambda _idx: None, seed=3, n_resamples=10) is None


def test_spikes_keep_the_highest_and_suppress_neighbours() -> None:
    starts = np.arange(0.0, 100.0, 10.0)
    ends = starts + 10
    z = np.array([0.0, 4.0, 3.5, 0.0, 0.0, 0.0, 0.0, 3.2, 3.1, 1.0])
    kept = al.spikes(z, starts, ends, threshold=3.0, suppress_s=15.0)
    assert kept.tolist() == [1, 7]
    assert al.spikes(z, starts, ends, threshold=9.0, suppress_s=15.0).size == 0


def test_interval_gaps_and_near() -> None:
    starts = np.array([0.0, 50.0, 100.0])
    ends = starts + 10
    gaps = al.interval_gaps(starts, ends, np.array([5.0, 70.0]), np.array([8.0, 80.0]))
    assert gaps.tolist() == [0.0, 10.0, 20.0]
    assert al.near(starts, ends, np.array([5.0]), np.array([8.0]), tolerance_s=0).tolist() == [
        True,
        False,
        False,
    ]
    assert np.isinf(al.interval_gaps(starts, ends, np.zeros(0), np.zeros(0))).all()


def test_series_frame_matches_the_features_and_drops_clip_links(synthetic: Path) -> None:
    video_id = "9100000001"
    duration, messages, wav = _vod(synthetic, video_id)
    lexicon = load_lexicon(LEXICON)
    link = messages.head(1).with_columns(
        text=pl.lit("clips.twitch.tv/SomeClipSlug"), offset_s=pl.lit(10.5)
    )
    series = al.series_frame(video_id, duration, pl.concat([messages, link]), wav, lexicon=lexicon)
    assert series.schema == al.SERIES_SCHEMA
    assert series["start_s"].diff().drop_nulls().unique().to_list() == [1.0]
    kept = messages.filter(~pl.col("text").str.contains(lexicon.clip_filter))
    placed = kept.filter(pl.col("offset_s") < series["end_s"].max())
    assert series["chat_count"].sum() == placed.height
    bin_10 = kept.filter((pl.col("offset_s") >= 10) & (pl.col("offset_s") < 11)).height
    assert series.filter(pl.col("start_s") == 10.0)["chat_count"][0] == bin_10


def test_window_signals_reproduce_the_feature_z_scores_at_10_s(synthetic: Path) -> None:
    video_id = "9100000001"
    duration, messages, wav = _vod(synthetic, video_id)
    lexicon = load_lexicon(LEXICON)
    series = al.series_frame(video_id, duration, messages, wav, lexicon=lexicon)
    signals = al.window_signals(series, duration, 10.0)
    windows = make_windows(video_id, duration)
    chat = compute_chat_features(windows, messages, lexicon=lexicon)
    audio = compute_audio_features(windows, wav)
    assert signals.height == windows.height
    assert signals["start_s"].to_list() == windows["start_s"].to_list()
    np.testing.assert_allclose(signals["chat_rate"], chat["chat_msg_rate"], atol=1e-9)
    np.testing.assert_allclose(signals["chat_z"], chat["chat_msg_rate_z"], atol=1e-6)
    np.testing.assert_allclose(signals["rms_max"], audio["audio_rms_max"], atol=1e-9)
    np.testing.assert_allclose(signals["audio_z"], audio["audio_rms_max_z"], atol=1e-6)


def test_window_signals_of_an_empty_series() -> None:
    empty = pl.DataFrame(schema=al.SERIES_SCHEMA)
    assert al.window_signals(empty, 100.0, 10.0).schema == al.WINDOW_SIGNAL_SCHEMA


def test_chat_follows_loudness_by_the_planted_lag(synthetic: Path) -> None:
    """The lag lands where every planted chat burst covers the loud burst it follows.

    For a short loud burst ``[a0, a1]`` and its longer chat burst ``[c0, c1]``, the correlation
    is highest for lags from ``c0 - a0`` to ``c1 - a1``; the band all of the VOD's events share
    is the intersection of those ranges (8.0-12.2 s for this VOD).
    """
    video_id = "9100000001"
    duration, messages, wav = _vod(synthetic, video_id)
    series = al.series_frame(video_id, duration, messages, wav, lexicon=load_lexicon(LEXICON))
    chat = al.chat_signal(series["chat_count"].to_numpy(), detrend_bins=61)
    loud = al.loudness_signal(series["rms_max"].to_numpy(), detrend_bins=61)
    lags = list(range(-30, 61))
    lag = al.peak_lag(lags, al.lagged_correlation(loud, chat, lags))
    events = next(v for v in build_scenario(1).vods if v.plan.video_id == video_id).events
    low = max(e.chat_burst_s[0] - e.audio_burst_s[0] for e in events)
    high = min(e.chat_burst_s[1] - e.audio_burst_s[1] for e in events)
    assert low < high
    assert math.floor(low) <= lag <= math.ceil(high)


def test_loudness_db_floors_silence() -> None:
    assert al.loudness_db(np.array([0.0, 1.0, 0.1])).tolist() == pytest.approx([-60, 0, -20])
