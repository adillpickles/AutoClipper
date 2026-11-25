"""Chat delay and clip alignment estimators (#36; docs/data/labeling.md § 1 and § 5).

Everything here works on one VOD's **per-second series** in VOD time (`series_frame`):

- ``chat_count``: chat messages per second, clip-link messages removed by the same rule as the
  chat features (`autoclipper.features.chat`);
- ``rms_max``: the loudest 64 ms audio frame of each second, linear full scale
  (`autoclipper.features.audio`).

From them come two standardized signals (`chat_signal`, `loudness_signal`): ``log1p`` of the
chat count and the loudness in dBFS, each minus a centred moving average (slow trends such as a
stream getting busier over hours would otherwise dominate) and z-scored over the VOD.

The estimators:

- `event_times`: the loudest separate seconds of a signal. The streamer's own loud reaction is
  the closest thing to "when it happened" that needs no clips.
- `event_profiles` / `mean_profile` / `smooth` / `profile_peak` / `half_max_span`: the average
  signal around anchor times (loud events, clip starts, ...), where it peaks and how long it
  stays up.
- `lagged_correlation`: how chat follows loudness at each lag over the whole VOD. On streams
  with constant talking loudness carries little event signal, so this is a weak estimate.
- `window_signals` / `spikes` / `near`: spikes at a window size, with the same z-scores the
  features use, and whether they fall near moments.

Nothing here reads files except `series_frame`; the study that runs this over a collection is
`autoclipper.labels.alignment_study`.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import numpy as np
import numpy.typing as npt
import polars as pl

from autoclipper.features.audio import STD_FLOOR_RMS, compute_audio_features
from autoclipper.features.chat import ZERO_STD, Lexicon, compute_chat_features
from autoclipper.labels.windows import make_windows

SERIES_STEP_S: Final = 1.0
"""Bin width of the per-second series."""

SILENCE_DB: Final = -60.0
"""Loudness floor in dBFS; quieter seconds count as this (the features' silence level)."""

SERIES_SCHEMA: Final = pl.Schema(
    {
        "video_id": pl.String,
        "start_s": pl.Float64,
        "end_s": pl.Float64,
        "chat_count": pl.Float64,
        "rms_max": pl.Float64,
    }
)
"""Columns of `series_frame`."""

WINDOW_SIGNAL_SCHEMA: Final = pl.Schema(
    {
        "start_s": pl.Float64,
        "end_s": pl.Float64,
        "chat_rate": pl.Float64,
        "chat_z": pl.Float64,
        "rms_max": pl.Float64,
        "audio_z": pl.Float64,
    }
)
"""Columns of `window_signals`."""

type FloatArray = npt.NDArray[np.float64]
type IntArray = npt.NDArray[np.int64]


def series_frame(
    video_id: str,
    duration_s: float,
    messages: pl.DataFrame,
    wav_path: Path,
    *,
    lexicon: Lexicon,
) -> pl.DataFrame:
    """One VOD's per-second chat count and loudness, with the features' own code.

    The bins are `make_windows` windows of `SERIES_STEP_S` (a final bin under half a second is
    dropped, as for any window size).

    Args:
        video_id: The VOD.
        duration_s: Its length (``vods.duration_s``).
        messages: Its ``chat_messages`` rows.
        wav_path: Its normalized 16 kHz WAV.
        lexicon: The chat lexicon; its clip-link filter removes clip-link messages.

    Returns:
        A frame with `SERIES_SCHEMA`, one row per bin in time order.
    """
    windows = make_windows(video_id, duration_s, window_s=SERIES_STEP_S, stride_s=SERIES_STEP_S)
    if windows.height == 0:
        return pl.DataFrame(schema=SERIES_SCHEMA)
    chat = compute_chat_features(windows, messages, lexicon=lexicon).select(
        "window_id", "chat_msg_rate"
    )
    audio = compute_audio_features(windows, wav_path).select("window_id", "audio_rms_max")
    return (
        windows.join(chat, on="window_id", how="left")
        .join(audio, on="window_id", how="left")
        .sort("start_s")
        .select(
            "video_id",
            "start_s",
            "end_s",
            # The rate is messages per second of the bin's real length; back to a count.
            ((pl.col("chat_msg_rate") * (pl.col("end_s") - pl.col("start_s"))).round(0)).alias(
                "chat_count"
            ),
            pl.col("audio_rms_max").alias("rms_max"),
        )
        .cast(SERIES_SCHEMA)
    )


def detrend(x: FloatArray, width: int) -> FloatArray:
    """`x` minus its centred moving average over `width` bins (fewer at the edges).

    Raises:
        ValueError: `width` < 1.
    """
    if width < 1:
        raise ValueError(f"width must be at least 1, got {width}")
    n = x.size
    if n == 0:
        return x.astype(np.float64)
    half = width // 2
    csum = np.concatenate(([0.0], np.cumsum(x, dtype=np.float64)))
    lo = np.clip(np.arange(n) - half, 0, n)
    hi = np.clip(np.arange(n) + half + 1, 0, n)
    return x - (csum[hi] - csum[lo]) / (hi - lo)


def standardize(x: FloatArray) -> FloatArray:
    """Z-score over the whole array (population std); all zeros if the std is below `ZERO_STD`."""
    if x.size == 0:
        return x.astype(np.float64)
    std = float(x.std())
    if std < ZERO_STD:
        return np.zeros_like(x, dtype=np.float64)
    return (x - x.mean()) / std


def chat_signal(chat_count: FloatArray, *, detrend_bins: int) -> FloatArray:
    """Standardized, detrended ``log1p`` chat count."""
    return standardize(detrend(np.log1p(np.asarray(chat_count, dtype=np.float64)), detrend_bins))


def loudness_db(rms_max: FloatArray) -> FloatArray:
    """Linear full-scale RMS to dBFS, floored at `SILENCE_DB`."""
    floor = 10 ** (SILENCE_DB / 20)
    return 20 * np.log10(np.maximum(np.asarray(rms_max, dtype=np.float64), floor))


def loudness_signal(rms_max: FloatArray, *, detrend_bins: int) -> FloatArray:
    """Standardized, detrended loudness in dBFS."""
    return standardize(detrend(loudness_db(rms_max), detrend_bins))


def lagged_correlation(x: FloatArray, y: FloatArray, lags: Sequence[int]) -> FloatArray:
    """Pearson correlation of ``x[t]`` with ``y[t + lag]`` for each lag, in bins.

    A positive lag means `y` follows `x`. A lag that leaves fewer than two overlapping bins, or an
    overlap where either side is constant, gives 0.

    Raises:
        ValueError: `x` and `y` differ in length.
    """
    if x.size != y.size:
        raise ValueError("x and y must have the same length")
    n = x.size
    out = np.zeros(len(lags), dtype=np.float64)
    for i, lag in enumerate(lags):
        a, b = (x[: n - lag], y[lag:]) if lag >= 0 else (x[-lag:], y[: n + lag])
        if a.size < 2:
            continue
        a = a - a.mean()
        b = b - b.mean()
        denom = float(np.sqrt((a * a).sum() * (b * b).sum()))
        if denom > 0:
            out[i] = float((a * b).sum() / denom)
    return out


def peak_lag(lags: Sequence[int], values: FloatArray) -> int:
    """The lag with the highest value (the earliest on ties).

    Raises:
        ValueError: `lags` is empty or differs in length from `values`.
    """
    if len(lags) == 0 or len(lags) != values.size:
        raise ValueError("lags must be non-empty and match values")
    return int(lags[int(np.argmax(values))])


def event_profiles(
    signal: FloatArray,
    anchors_s: Sequence[float],
    offsets: Sequence[int],
    *,
    step_s: float = SERIES_STEP_S,
) -> FloatArray:
    """The signal around each anchor: one row per anchor, one column per offset (in bins).

    The anchor's bin is the one holding it (``floor(anchor / step_s)``). Cells outside the
    series are NaN.
    """
    anchors = np.floor(np.asarray(anchors_s, dtype=np.float64) / step_s).astype(np.int64)
    idx = anchors[:, None] + np.asarray(offsets, dtype=np.int64)[None, :]
    valid = (idx >= 0) & (idx < signal.size)
    out = np.full(idx.shape, np.nan, dtype=np.float64)
    out[valid] = signal[idx[valid]]
    return out


def mean_profile(rows: FloatArray) -> FloatArray:
    """Column means ignoring NaN; NaN for a column without values (never a warning)."""
    if rows.ndim != 2:
        raise ValueError("rows must be two-dimensional")
    present = ~np.isnan(rows)
    counts = present.sum(axis=0)
    sums = np.where(present, rows, 0.0).sum(axis=0)
    out = np.full(rows.shape[1], np.nan, dtype=np.float64)
    np.divide(sums, counts, out=out, where=counts > 0)
    return out


def smooth(profile: FloatArray, width: int) -> FloatArray:
    """Centred moving average over `width` bins, ignoring NaN; NaN where a window has no value.

    Raises:
        ValueError: `width` < 1.
    """
    if width < 1:
        raise ValueError(f"width must be at least 1, got {width}")
    present = np.isfinite(profile)
    values = np.where(present, profile, 0.0)
    kernel = np.ones(width)
    sums = np.convolve(values, kernel, mode="same")
    counts = np.convolve(present.astype(np.float64), kernel, mode="same")
    out = np.full(profile.size, np.nan, dtype=np.float64)
    np.divide(sums, counts, out=out, where=counts > 0)
    return out


def event_times(signal: FloatArray, *, threshold: float, suppress_s: float) -> FloatArray:
    """Seconds where a per-second `signal` is at or above `threshold`, as separate events.

    The highest second goes first and suppresses the others within `suppress_s` (`spikes` on
    one-second windows). Returns the bin starts in time order.
    """
    starts = np.arange(signal.size, dtype=np.float64) * SERIES_STEP_S
    kept = spikes(
        signal, starts, starts + SERIES_STEP_S, threshold=threshold, suppress_s=suppress_s
    )
    return starts[kept]


@dataclass(frozen=True, slots=True)
class Peak:
    """Where a profile peaks inside a search range, and how high."""

    offset: int
    height: float


def profile_peak(
    offsets: Sequence[int], profile: FloatArray, search: tuple[int, int]
) -> Peak | None:
    """The highest value of `profile` with an offset in ``[search[0], search[1]]``.

    Ties go to the earlier offset. None when that range holds no finite value.
    """
    off = np.asarray(offsets, dtype=np.int64)
    mask = (off >= search[0]) & (off <= search[1]) & np.isfinite(profile)
    if not mask.any():
        return None
    candidates = np.flatnonzero(mask)
    best = candidates[int(np.argmax(profile[candidates]))]
    return Peak(int(off[best]), float(profile[best]))


def half_max_span(
    offsets: Sequence[int],
    profile: FloatArray,
    peak: Peak,
    baseline: tuple[int, int],
) -> tuple[int, int] | None:
    """First and last offset of the contiguous stretch around `peak` at or above half its height.

    Half height is measured from the profile's mean over the `baseline` offsets. None when the
    baseline has no finite value or the peak isn't above it.
    """
    off = np.asarray(offsets, dtype=np.int64)
    base_mask = (off >= baseline[0]) & (off <= baseline[1]) & np.isfinite(profile)
    if not base_mask.any():
        return None
    base = float(profile[base_mask].mean())
    if peak.height <= base:
        return None
    level = base + (peak.height - base) / 2
    centre = int(np.flatnonzero(off == peak.offset)[0])
    left = centre
    while left > 0 and np.isfinite(profile[left - 1]) and profile[left - 1] >= level:
        left -= 1
    right = centre
    while right < off.size - 1 and np.isfinite(profile[right + 1]) and profile[right + 1] >= level:
        right += 1
    return int(off[left]), int(off[right])


def bootstrap_statistic(
    n_items: int,
    statistic: Callable[[IntArray], float | None],
    *,
    seed: int,
    n_resamples: int,
    confidence: float = 0.95,
) -> tuple[float, float] | None:
    """Percentile interval of `statistic` over resamples of item indices, with replacement.

    `statistic` gets an index array of length `n_items` (for example VODs) and returns a value or
    None; None and non-finite values are left out. Like `autoclipper.eval.bootstrap`, the indices
    come from a NumPy generator seeded with `seed`.

    Returns:
        ``(low, high)``, or None when no resample gave a value or `n_items` is 0.
    """
    if n_items == 0:
        return None
    rng = np.random.default_rng(seed)
    picks = rng.integers(0, n_items, size=(n_resamples, n_items))
    values = [statistic(row) for row in picks]
    kept = np.asarray([v for v in values if v is not None and np.isfinite(v)], dtype=np.float64)
    if kept.size == 0:
        return None
    tail = (1 - confidence) / 2 * 100
    low, high = np.percentile(kept, [tail, 100 - tail])
    return float(low), float(high)


def window_signals(series: pl.DataFrame, duration_s: float, window_s: float) -> pl.DataFrame:
    """Chat rate and loudest frame per `window_s` window, z-scored as the features do.

    Windows come from `make_windows` (a final window under half `window_s` is dropped). Each
    one-second bin goes to the window holding its start. ``chat_rate`` is messages per second of
    the window's real length; ``chat_z`` is its VOD z-score (population std, 0 below
    `ZERO_STD`), like ``chat_msg_rate_z``. ``rms_max`` is the window's loudest frame; ``audio_z``
    divides by the std floored at `STD_FLOOR_RMS`, like ``audio_rms_max_z``.

    Returns:
        A frame with `WINDOW_SIGNAL_SCHEMA` in time order.
    """
    if series.height == 0:
        return pl.DataFrame(schema=WINDOW_SIGNAL_SCHEMA)
    video_id = str(series["video_id"][0])
    windows = make_windows(video_id, duration_s, window_s=window_s, stride_s=window_s)
    if windows.height == 0:
        return pl.DataFrame(schema=WINDOW_SIGNAL_SCHEMA)
    starts = windows["start_s"].to_numpy()
    ends = windows["end_s"].to_numpy()
    bins = series["start_s"].to_numpy()
    slot = np.searchsorted(starts, bins, side="right") - 1
    inside = (slot >= 0) & (bins < ends[np.clip(slot, 0, None)])
    counts = np.bincount(
        slot[inside], weights=series["chat_count"].to_numpy()[inside], minlength=starts.size
    )
    loud = np.zeros(starts.size, dtype=np.float64)
    np.maximum.at(loud, slot[inside], series["rms_max"].to_numpy()[inside])
    rate = counts / (ends - starts)
    chat_std = float(rate.std())
    chat_z = (rate - rate.mean()) / chat_std if chat_std >= ZERO_STD else np.zeros_like(rate)
    audio_z = (loud - loud.mean()) / max(float(loud.std()), STD_FLOOR_RMS)
    return pl.DataFrame(
        {
            "start_s": starts,
            "end_s": ends,
            "chat_rate": rate,
            "chat_z": chat_z,
            "rms_max": loud,
            "audio_z": audio_z,
        },
        schema=WINDOW_SIGNAL_SCHEMA,
    )


def spikes(
    z: FloatArray,
    starts_s: FloatArray,
    ends_s: FloatArray,
    *,
    threshold: float,
    suppress_s: float,
) -> IntArray:
    """Indices of spike windows: z at or above `threshold`, greedy suppression by centre distance.

    Windows are taken highest z first (earlier start on ties); one is kept when no kept window's
    centre is within `suppress_s` of its centre, like candidate generation's peak suppression.

    Returns:
        The kept indices in time order.
    """
    centres = (starts_s + ends_s) / 2
    order = sorted(np.flatnonzero(z >= threshold).tolist(), key=lambda i: (-z[i], starts_s[i]))
    kept: list[int] = []
    for i in order:
        if all(abs(centres[i] - centres[j]) > suppress_s for j in kept):
            kept.append(i)
    return np.asarray(sorted(kept), dtype=np.int64)


def interval_gaps(
    starts_s: FloatArray, ends_s: FloatArray, other_starts_s: FloatArray, other_ends_s: FloatArray
) -> FloatArray:
    """For each interval, the gap in seconds to the nearest `other` interval (0 if they overlap).

    Infinite when there are no other intervals.
    """
    if other_starts_s.size == 0:
        return np.full(starts_s.size, np.inf, dtype=np.float64)
    gap = np.maximum(
        np.maximum(other_starts_s[None, :] - ends_s[:, None], starts_s[:, None] - other_ends_s),
        0.0,
    )
    return gap.min(axis=1)


def near(
    starts_s: FloatArray,
    ends_s: FloatArray,
    other_starts_s: FloatArray,
    other_ends_s: FloatArray,
    *,
    tolerance_s: float,
) -> npt.NDArray[np.bool_]:
    """Whether each interval is within `tolerance_s` of any `other` interval."""
    return interval_gaps(starts_s, ends_s, other_starts_s, other_ends_s) <= tolerance_s
