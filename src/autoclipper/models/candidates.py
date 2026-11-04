"""Turn window scores into ranked candidate moments (docs/ml/modeling-plan.md § 3).

The same rules apply to every scorer, baselines included:

1. **Smooth** the window scores with a centred rolling mean over `CandidateParams.smooth_windows`
   windows.
2. **Peaks** are local maxima of the smoothed score (the first window of a plateau), each moved
   to the highest raw score within the smoothing span.
3. **Non-max suppression** drops a peak whose window starts within ``min_gap_s`` of a kept peak
   with a higher raw score (the same score that ranks candidates).
4. **Boundaries:** start ``pre_s`` before the peak window's start; end ``post_s`` after the last
   consecutive window whose smoothed score stays at or above half the peak's height above the
   VOD's median smoothed score (scores can be negative z-score sums, so "half the peak" is
   measured from that baseline). The length is then clamped to ``[min_len_s, max_len_s]`` with
   the peak window kept inside, and the interval moved inside the VOD.
5. **Shortlist:** the ``shortlist_k`` best peaks by score (the peak window's raw score; ties to
   the earlier start). The top 10 are rendered and evaluated.
6. **Viewer clips (product mode only):** a viewer-clip moment overlapping a discovered candidate
   turns it into ``both`` (kept even past the shortlist); other moments become ``viewer_clips``
   candidates, scored by the highest window score over their span and cut to the same length
   rules around that window. Moments never merge into each other, and a moment outside the
   scored windows is dropped with a warning.
   Independent-discovery evaluation passes no moments, so clips never influence discovery.

Each candidate's ``top_signals`` are the feature columns (from `SIGNAL_COLUMNS`) whose maximum
over the candidate's windows is at least ``signal_min``, strongest first, at most
``max_signals``.
"""

import logging
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Final

import numpy as np
import polars as pl

from autoclipper.analysis import MAX_CANDIDATES, Candidate, Source

log = logging.getLogger(__name__)

SIGNAL_COLUMNS: Final = (
    "chat_msg_rate_z",
    "chat_unique_rate_z",
    "chat_msg_rate_rz",
    "chat_msg_rate_lead1",
    "chat_msg_rate_lead2",
    "audio_rms_max_z",
    "audio_rms_rz",
)
"""Standardized feature columns that can explain a candidate (z-score scale)."""

SCORE_COLUMNS: Final = ("window_id", "start_s", "end_s", "score")


@dataclass(frozen=True, slots=True)
class CandidateParams:
    """Candidate generation settings; defaults from docs/ml/modeling-plan.md § 3."""

    smooth_windows: int = 3
    min_gap_s: float = 60.0
    pre_s: float = 20.0
    post_s: float = 10.0
    min_len_s: float = 20.0
    max_len_s: float = 45.0
    shortlist_k: int = 50
    half_peak: float = 0.5
    signal_min: float = 1.0
    max_signals: int = 3
    signal_columns: tuple[str, ...] = field(default=SIGNAL_COLUMNS)

    def __post_init__(self) -> None:
        if self.smooth_windows < 1 or self.shortlist_k < 1 or self.max_signals < 0:
            raise ValueError("smooth_windows and shortlist_k must be >= 1, max_signals >= 0")
        values = (self.min_gap_s, self.pre_s, self.post_s, self.min_len_s, self.max_len_s)
        if not all(math.isfinite(v) and v >= 0 for v in values):
            raise ValueError("gap, padding and lengths must be finite and >= 0")
        if not 0 < self.min_len_s <= self.max_len_s:
            raise ValueError("need 0 < min_len_s <= max_len_s")
        if not 0 <= self.half_peak <= 1:
            raise ValueError("half_peak must be between 0 and 1")


@dataclass(frozen=True, slots=True)
class MomentInput:
    """A viewer-clip moment of the VOD (from the ``moments`` table)."""

    moment_id: str
    start_s: float
    end_s: float
    clip_ids: tuple[str, ...]


def generate_candidates(
    scores: pl.DataFrame,
    *,
    duration_s: float,
    features: pl.DataFrame | None = None,
    moments: Sequence[MomentInput] = (),
    params: CandidateParams | None = None,
) -> list[Candidate]:
    """Build the ranked candidates of one VOD.

    Args:
        scores: One row per window of the VOD: ``window_id``, ``start_s``, ``end_s`` and the
            scorer's ``score`` (higher = more clip-worthy).
        duration_s: The VOD's length in seconds; candidates stay inside ``[0, duration_s]``.
        features: Optional feature rows keyed by ``window_id`` (any of `SIGNAL_COLUMNS`), for
            ``top_signals``. Without it every ``top_signals`` is empty.
        moments: The VOD's viewer-clip moments (product mode). Empty for independent discovery.
        params: Generation settings.

    Returns:
        Candidates in rank order (score descending, then start, then ID), ``rank`` 1..N and IDs
        ``c001``, ``c002``, ..., at most `MAX_CANDIDATES`.

    Raises:
        ValueError: A column is missing or null, a score or bound isn't finite, windows overlap,
            or `duration_s` isn't positive.
    """
    params = params or CandidateParams()
    if not (math.isfinite(duration_s) and duration_s > 0):
        raise ValueError(f"duration_s must be positive, got {duration_s!r}")
    windows = _checked_scores(scores)
    if windows.height == 0:
        return []
    starts = windows["start_s"].to_numpy()
    ends = windows["end_s"].to_numpy()
    raw = windows["score"].to_numpy()
    smoothed = (
        windows["score"].rolling_mean(params.smooth_windows, min_samples=1, center=True).to_numpy()
    )
    baseline = float(np.median(smoothed))
    signals = _signal_matrix(windows, features, params.signal_columns)

    # Smoothing spreads a one-window spike over its neighbours, so each smoothed peak is moved
    # to the highest raw score within the smoothing span: the candidate's score is the scorer's
    # window score at its peak (docs/ml/features.md § Context features).
    half = params.smooth_windows // 2
    peaks = sorted(
        {
            lo + int(np.argmax(raw[lo : hi + 1]))
            for i in _peaks(smoothed)
            for lo, hi in [(max(0, i - half), min(len(raw) - 1, i + half))]
        }
    )
    # Suppression and ranking use the same score: the peak window's raw score.
    kept: list[int] = []
    for index in sorted(peaks, key=lambda i: (-raw[i], starts[i])):
        if all(abs(starts[index] - starts[k]) >= params.min_gap_s for k in kept):
            kept.append(index)

    # Every surviving peak gets a draft, so a viewer-clip moment over a peak just outside the
    # shortlist is still recognized as discovered too (`both`), not mislabelled `viewer_clips`.
    discovered: list[_Draft] = []
    for index in kept:
        threshold = baseline + params.half_peak * (smoothed[index] - baseline)
        last = index
        while last + 1 < len(smoothed) and smoothed[last + 1] >= threshold:
            last += 1
        start, end = _bounds(
            starts[index] - params.pre_s,
            ends[last] + params.post_s,
            (starts[index], ends[index]),
            duration_s,
            params,
        )
        discovered.append(_Draft(float(raw[index]), start, end, "discovered", set()))

    clipped: list[_Draft] = []
    for moment in moments:
        if not moment.clip_ids:
            continue
        # Moments merge into discovered candidates only, never into each other.
        overlapping = [d for d in discovered if d.start < moment.end_s and moment.start_s < d.end]
        if overlapping:
            for draft in overlapping:
                draft.source = "both"
                draft.clip_ids.update(moment.clip_ids)
            continue
        inside = np.flatnonzero((starts < moment.end_s) & (ends > moment.start_s))
        if not len(inside):
            log.warning(
                "viewer-clip moment outside the scored windows moment_id=%s start_s=%.1f",
                moment.moment_id,
                moment.start_s,
            )
            continue
        best = int(inside[np.argmax(raw[inside])])
        peak = (float(starts[best]), float(ends[best]))
        start = max(moment.start_s, peak[0] - params.pre_s)
        start, end = _bounds(start, moment.end_s, peak, duration_s, params)
        clipped.append(_Draft(float(raw[best]), start, end, "viewer_clips", set(moment.clip_ids)))

    # The shortlist: the best `shortlist_k` discovered peaks (merged or not), then every
    # clip-backed candidate beyond it, so viewer clips are never dropped by the cut.
    discovered.sort(key=lambda d: (-d.score, d.start))
    drafts = discovered[: params.shortlist_k]
    drafts += [d for d in discovered[params.shortlist_k :] if d.source == "both"]
    drafts += clipped
    drafts.sort(key=lambda d: (-d.score, d.start))
    drafts = drafts[:MAX_CANDIDATES]
    candidates: list[Candidate] = []
    for rank, draft in enumerate(drafts, start=1):
        candidates.append(
            Candidate(
                candidate_id=f"c{rank:03d}",
                rank=rank,
                score=draft.score,
                start_s=draft.start,
                end_s=draft.end,
                source=draft.source,
                n_viewer_clips=len(draft.clip_ids),
                top_signals=_top_signals(signals, starts, ends, draft, params),
            )
        )
    return candidates


@dataclass(slots=True)
class _Draft:
    score: float
    start: float
    end: float
    source: Source
    clip_ids: set[str]


def _peaks(smoothed: np.ndarray) -> list[int]:
    """Local maxima of the smoothed scores.

    A peak is the first window of a run of equal values that is higher than the run before it
    and the run after it (the VOD's edges count as lower). A step on the way up is not a peak; a
    VOD with a single value has one peak, at its start.
    """
    n = len(smoothed)
    peaks: list[int] = []
    i = 0
    while i < n:  # one pass over the windows of one VOD (a few thousand)
        j = i
        while j + 1 < n and smoothed[j + 1] == smoothed[i]:
            j += 1
        higher_before = i == 0 or smoothed[i - 1] < smoothed[i]
        higher_after = j == n - 1 or smoothed[j + 1] < smoothed[i]
        if higher_before and higher_after:
            peaks.append(i)
        i = j + 1
    return peaks


def _bounds(
    start: float,
    end: float,
    peak: tuple[float, float],
    duration_s: float,
    params: CandidateParams,
) -> tuple[float, float]:
    """Clamp ``[start, end)`` to the length limits, keeping `peak` inside, then into the VOD."""
    if end - start > params.max_len_s:
        end = start + params.max_len_s
        if end < peak[1]:  # keep the peak window inside
            end = peak[1]
            start = end - params.max_len_s
    if end - start < params.min_len_s:
        end = start + params.min_len_s
    length = min(end - start, duration_s)
    if start < 0:
        start, end = 0.0, length
    if end > duration_s:
        start, end = max(0.0, duration_s - length), duration_s
    return float(start), float(end)


def _signal_matrix(
    windows: pl.DataFrame, features: pl.DataFrame | None, columns: Sequence[str]
) -> tuple[list[str], np.ndarray]:
    if features is None or "window_id" not in features.columns:
        return [], np.zeros((windows.height, 0))
    if features["window_id"].n_unique() != features.height:
        raise ValueError("features lists a window_id more than once")
    present = [c for c in columns if c in features.columns]
    joined = windows.select("window_id").join(
        features.select("window_id", *present), on="window_id", how="left", maintain_order="left"
    )
    matrix = joined.select(present).fill_null(0.0).fill_nan(0.0).to_numpy().astype(np.float64)
    return present, matrix


def _top_signals(
    signals: tuple[list[str], np.ndarray],
    starts: np.ndarray,
    ends: np.ndarray,
    draft: _Draft,
    params: CandidateParams,
) -> tuple[str, ...]:
    names, matrix = signals
    if not names or params.max_signals == 0:
        return ()
    inside = (starts < draft.end) & (ends > draft.start)
    if not inside.any():
        return ()
    strongest = matrix[inside].max(axis=0)
    order = sorted(range(len(names)), key=lambda j: (-strongest[j], names[j]))
    return tuple(names[j] for j in order if strongest[j] >= params.signal_min)[: params.max_signals]


def _checked_scores(scores: pl.DataFrame) -> pl.DataFrame:
    missing = sorted(set(SCORE_COLUMNS) - set(scores.columns))
    if missing:
        raise ValueError(f"scores is missing columns: {', '.join(missing)}")
    nulls = [c for c in SCORE_COLUMNS if scores[c].null_count()]
    if nulls:
        raise ValueError(f"scores has nulls in: {', '.join(nulls)}")
    windows = scores.select(SCORE_COLUMNS).sort("start_s")
    finite = windows.select(pl.col("start_s", "end_s", "score").is_finite().all()).row(0)
    if not all(finite):
        raise ValueError("scores has a start_s, end_s or score that isn't finite")
    if windows.filter(
        ~(pl.col("end_s") > pl.col("start_s")) | (pl.col("start_s") < pl.col("end_s").shift(1))
    ).height:
        raise ValueError("score windows overlap or have no length")
    return windows


def candidates_frame(video_id: str, candidates: Sequence[Candidate]) -> pl.DataFrame:
    """The evaluation's input rows (`autoclipper.eval.harness`) for one VOD's candidates.

    ``candidate_id`` is prefixed with the VOD (``<video_id>:c001``) so IDs stay unique when the
    frames of several VODs are concatenated.
    """
    return pl.DataFrame(
        {
            "video_id": [video_id] * len(candidates),
            "candidate_id": [f"{video_id}:{c.candidate_id}" for c in candidates],
            "start_s": [c.start_s for c in candidates],
            "end_s": [c.end_s for c in candidates],
            "score": [c.score for c in candidates],
        },
        schema={
            "video_id": pl.String,
            "candidate_id": pl.String,
            "start_s": pl.Float64,
            "end_s": pl.Float64,
            "score": pl.Float64,
        },
    )
