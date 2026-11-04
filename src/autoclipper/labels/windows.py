"""Cut a VOD into fixed time windows: the timeline that chat, audio and labels share.

Windows follow docs/data/labeling.md § 3 and the ``windows`` table in
docs/architecture/data-model.md. They are built from the VOD's duration alone, with no labels or
split, so analyzing a new VOD uses exactly the same windows as training.

- Window ``index`` starts at ``index * stride_s`` and lasts ``window_s`` (10 s by default,
  non-overlapping), cut short at the end of the VOD.
- A window cut short to less than ``window_s / 2`` at the end is dropped; a longer one is kept
  with its real (shorter) end.
- ``is_muted`` flags windows that overlap a muted segment (half-open intervals: touching edges
  don't overlap). Their audio features are unreliable, but the windows are kept.
"""

import logging
import math
from collections.abc import Sequence
from typing import Final

import numpy as np
import polars as pl

log = logging.getLogger(__name__)

# docs/data/labeling.md § 3.
WINDOW_S: Final = 10.0
STRIDE_S: Final = 10.0

WINDOWS_SCHEMA: Final = pl.Schema(
    {
        "window_id": pl.String,
        "video_id": pl.String,
        "index": pl.Int64,
        "start_s": pl.Float64,
        "end_s": pl.Float64,
        "is_muted": pl.Boolean,
    }
)
"""The ``windows`` table (data-model.md)."""


def make_windows(
    video_id: str,
    duration_s: float,
    muted_segments: Sequence[tuple[float, float]] = (),
    *,
    window_s: float = WINDOW_S,
    stride_s: float = STRIDE_S,
) -> pl.DataFrame:
    """Return the windows of one VOD, in time order.

    Args:
        video_id: The Twitch video ID.
        duration_s: The VOD's length in seconds.
        muted_segments: ``(offset_s, duration_s)`` of each muted part, as in ``vods``.
        window_s: Window length in seconds.
        stride_s: Seconds between window starts.

    Returns:
        A frame with `WINDOWS_SCHEMA`; empty for a VOD shorter than half a window.

    Raises:
        ValueError: `video_id` is blank, a length or offset is negative or not finite, or
            `window_s`/`stride_s` is not positive.
    """
    if not video_id.strip():
        raise ValueError("video_id is blank")
    for name, value in (("window_s", window_s), ("stride_s", stride_s)):
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"{name} must be a positive number of seconds, got {value!r}")
    if not math.isfinite(duration_s) or duration_s < 0:
        raise ValueError(f"duration_s must be a non-negative number of seconds, got {duration_s!r}")
    muted = np.asarray(muted_segments, dtype=np.float64).reshape(-1, 2)
    if not np.isfinite(muted).all() or (muted < 0).any():
        raise ValueError("muted segments must have non-negative, finite offsets and durations")

    # Integer window numbers avoid float drift in the starts (0.1 * 3 != 0.3).
    n_starts = math.ceil(duration_s / stride_s) if duration_s > 0 else 0
    starts = np.arange(n_starts, dtype=np.float64) * stride_s
    ends = np.minimum(starts + window_s, duration_s)
    keep = ends - starts >= window_s / 2
    dropped = int((~keep).sum())
    if dropped:
        log.debug("dropped short final windows video_id=%s n=%d", video_id, dropped)
    starts, ends = starts[keep], ends[keep]
    indexes = np.flatnonzero(keep).astype(np.int64)

    mute_start = muted[:, 0]
    mute_end = muted[:, 0] + muted[:, 1]
    # Half-open overlap: window [s, e) and segment [ms, me) overlap iff s < me and ms < e.
    # Windows x segments; a VOD has a handful of muted segments, so this stays small.
    is_muted = (
        ((starts[:, None] < mute_end[None, :]) & (mute_start[None, :] < ends[:, None])).any(axis=1)
        if len(muted)
        else np.zeros(len(starts), dtype=bool)
    )
    return pl.DataFrame(
        {
            "window_id": [f"{video_id}:{i}" for i in indexes.tolist()],
            "video_id": [video_id] * len(indexes),
            "index": indexes,
            "start_s": starts,
            "end_s": ends,
            "is_muted": is_muted,
        },
        schema=WINDOWS_SCHEMA,
    )


def windows_for_vods(
    vods: pl.DataFrame, *, window_s: float = WINDOW_S, stride_s: float = STRIDE_S
) -> pl.DataFrame:
    """Return the windows of every VOD in a ``vods`` table, sorted by ``video_id`` and index.

    Args:
        vods: Rows of the ``vods`` table; needs ``video_id``, ``duration_s`` and
            ``muted_segments`` (a list of ``{offset_s, duration_s}`` structs).
        window_s: Window length in seconds.
        stride_s: Seconds between window starts.

    Raises:
        ValueError: A column is missing, a ``video_id`` repeats, or a value is invalid (see
            `make_windows`).
    """
    missing = {"video_id", "duration_s", "muted_segments"} - set(vods.columns)
    if missing:
        raise ValueError(f"vods is missing columns: {', '.join(sorted(missing))}")
    if vods["video_id"].null_count() or vods["duration_s"].null_count():
        raise ValueError("vods has a null video_id or duration_s")
    if vods["video_id"].n_unique() != vods.height:
        raise ValueError("vods lists a video_id more than once")
    frames = [
        make_windows(
            row["video_id"],
            float(row["duration_s"]),
            [(s["offset_s"], s["duration_s"]) for s in row["muted_segments"] or ()],
            window_s=window_s,
            stride_s=stride_s,
        )
        for row in vods.sort("video_id").iter_rows(named=True)  # one row per VOD, not per window
    ]
    if not frames:
        return WINDOWS_SCHEMA.to_frame()
    return pl.concat(frames)
