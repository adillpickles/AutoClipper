"""Chat and loudness over the whole stream, for the results timeline (#213).

`timeline_activity` downsamples features the pipeline already computed — ``chat_msg_rate``
(`autoclipper.features.chat`) and ``audio_rms_mean`` (`autoclipper.features.audio`) — into an
`Activity` of at most `MAX_POINTS` points, so the timeline can draw why the top moments stand
out. No new signal is measured here.

- ``step_s`` is the smallest multiple of the window stride that keeps the series within
  `MAX_POINTS` points: 10 s for VODs up to 50 minutes, 80 s for a 6-hour VOD. Point ``i``
  covers ``[i * step_s, (i + 1) * step_s)``; a window belongs to the point holding its start.
- ``chat``: the mean ``chat_msg_rate`` (messages per second) of the point's windows. A window
  with no messages is a measured 0.
- ``loudness``: the mean ``audio_rms_mean`` of the point's measured windows in dBFS, clamped
  to [-60, 0]. A window is unmeasured when Twitch muted it (``audio_is_muted``) or it had no
  audio frames (the audio ended early: ``audio_rms_mean`` and ``audio_silence_frac`` both 0,
  which real silence never gives, since silent frames count toward ``audio_silence_frac``).
- A point with no measured window is ``None``; a family that didn't arrive is ``None`` as a
  whole. Neither is ever filled with zeros.
"""

import math
from typing import Final

import numpy as np
import numpy.typing as npt
import polars as pl

from autoclipper.analysis import LOUDNESS_FLOOR_DBFS, Activity
from autoclipper.labels.windows import STRIDE_S

MAX_POINTS: Final = 300
CHAT_DECIMALS: Final = 3
LOUDNESS_DECIMALS: Final = 1

type _Floats = npt.NDArray[np.float64]


def activity_step_s(duration_s: float, *, max_points: int = MAX_POINTS) -> float:
    """The point spacing: the smallest multiple of the window stride giving <= `max_points`."""
    if not math.isfinite(duration_s) or duration_s <= 0:
        raise ValueError(f"duration_s must be a positive number of seconds, got {duration_s!r}")
    if max_points < 1:
        raise ValueError(f"max_points must be at least 1, got {max_points}")
    return STRIDE_S * max(1, math.ceil(duration_s / (max_points * STRIDE_S)))


def timeline_activity(
    windows: pl.DataFrame,
    chat_features: pl.DataFrame | None,
    audio_features: pl.DataFrame | None,
    *,
    duration_s: float,
    max_points: int = MAX_POINTS,
) -> Activity | None:
    """Downsample one VOD's per-window chat rate and loudness for the timeline.

    Args:
        windows: The VOD's ``windows`` (at least ``window_id`` and ``start_s``).
        chat_features: Its chat features (``window_id``, ``chat_msg_rate``), or None when no
            chat replay arrived.
        audio_features: Its audio features (``window_id``, ``audio_rms_mean``,
            ``audio_silence_frac``, ``audio_is_muted``), or None when no audio arrived.
        duration_s: The VOD's length in seconds.
        max_points: Most points per series.

    Returns:
        The `Activity`, or None when neither family arrived.
    """
    if chat_features is None and audio_features is None:
        return None
    step_s = activity_step_s(duration_s, max_points=max_points)
    n_points = math.ceil(duration_s / step_s - 1e-9)
    starts = windows.select("window_id", "start_s")
    chat = None
    if chat_features is not None:
        frame = starts.join(chat_features.select("window_id", "chat_msg_rate"), on="window_id")
        chat = _binned(
            frame["start_s"].to_numpy(),
            frame["chat_msg_rate"].to_numpy(),
            step_s,
            n_points,
            decimals=CHAT_DECIMALS,
        )
    loudness = None
    if audio_features is not None:
        frame = starts.join(
            audio_features.select(
                "window_id", "audio_rms_mean", "audio_silence_frac", "audio_is_muted"
            ),
            on="window_id",
        ).filter(
            (pl.col("audio_is_muted") == 0)
            & ~((pl.col("audio_rms_mean") == 0) & (pl.col("audio_silence_frac") == 0))
        )
        mean_rms = _bin_means(
            frame["start_s"].to_numpy(), frame["audio_rms_mean"].to_numpy(), step_s, n_points
        )
        floor = 10 ** (LOUDNESS_FLOOR_DBFS / 20)
        # NaN (no measured window) stays NaN through clip and log10, and becomes None.
        loudness = _rounded(20 * np.log10(np.clip(mean_rms, floor, 1.0)), LOUDNESS_DECIMALS)
    return Activity(step_s=step_s, chat=chat, loudness=loudness)


def _bin_means(starts_s: _Floats, values: _Floats, step_s: float, n_points: int) -> _Floats:
    """Mean of `values` per point; NaN where a point has no value."""
    slot = np.clip(np.floor(starts_s / step_s + 1e-9).astype(np.int64), 0, n_points - 1)
    total = np.bincount(slot, weights=values, minlength=n_points)
    count = np.bincount(slot, minlength=n_points)
    return np.divide(total, count, out=np.full(n_points, np.nan), where=count > 0)


def _binned(
    starts_s: _Floats, values: _Floats, step_s: float, n_points: int, *, decimals: int
) -> tuple[float | None, ...]:
    return _rounded(_bin_means(starts_s, values, step_s, n_points), decimals)


def _rounded(values: _Floats, decimals: int) -> tuple[float | None, ...]:
    return tuple(None if math.isnan(v) else round(float(v), decimals) for v in values)
