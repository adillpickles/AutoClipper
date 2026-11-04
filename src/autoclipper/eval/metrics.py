"""Top-k metrics against viewer-clip moments (docs/ml/evaluation.md § 3).

All functions work on one VOD at a time. Times are seconds from VOD start.

**Matching.** A candidate ``[s, e)`` matches a moment ``[ms, me)`` when ``s < me + TOL_S`` and
``e > ms - TOL_S``. Matching is one to one: candidates are taken in descending score (earlier
start, then candidate ID, breaks ties); each claims the unclaimed matching moment with the
largest overlap with its widened interval ``[ms - TOL_S, me + TOL_S)`` (then the earlier start,
then ``moment_id``).
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

# docs/ml/evaluation.md § 3.
TOL_S: Final = 15.0
TOP_K: Final = 10
TOP_CLIPS: Final = 5


@dataclass(frozen=True, slots=True)
class Candidate:
    """A scored candidate moment: ``[start_s, end_s)`` in VOD seconds."""

    candidate_id: str
    start_s: float
    end_s: float
    score: float


@dataclass(frozen=True, slots=True)
class Moment:
    """An observed viewer-clip moment: ``[start_s, end_s)`` in VOD seconds."""

    moment_id: str
    start_s: float
    end_s: float


def rank_candidates(candidates: Sequence[Candidate]) -> list[Candidate]:
    """Sort by descending score; ties go to the earlier start, then the smaller candidate ID."""
    return sorted(candidates, key=lambda c: (-c.score, c.start_s, c.candidate_id))


def match_candidates(
    candidates: Sequence[Candidate], moments: Sequence[Moment], *, tol_s: float = TOL_S
) -> dict[str, str]:
    """Match candidates to moments one to one; returns ``{candidate_id: moment_id}``.

    Candidates are processed in `rank_candidates` order. Unmatched candidates are absent.
    """
    unclaimed = {moment.moment_id: moment for moment in moments}
    matches: dict[str, str] = {}
    for candidate in rank_candidates(candidates):
        best: tuple[float, float, str] | None = None
        for moment in unclaimed.values():
            lo, hi = moment.start_s - tol_s, moment.end_s + tol_s
            if not (candidate.start_s < hi and candidate.end_s > lo):
                continue
            overlap = min(candidate.end_s, hi) - max(candidate.start_s, lo)
            key = (-overlap, moment.start_s, moment.moment_id)
            if best is None or key < best:
                best = key
        if best is not None:
            matches[candidate.candidate_id] = best[2]
            del unclaimed[best[2]]
    return matches


def hit_rate_at_k(
    candidates: Sequence[Candidate],
    moments: Sequence[Moment],
    *,
    k: int = TOP_K,
    tol_s: float = TOL_S,
) -> float | None:
    """Fraction of the top `k` candidates that match a moment.

    A VOD with fewer than `k` candidates is scored on what it has; one with none scores 0.
    Returns None for a VOD without moments: it is excluded from the metric, not scored 0.
    """
    if not moments:
        return None
    top = rank_candidates(candidates)[:k]
    if not top:
        return 0.0
    return len(match_candidates(top, moments, tol_s=tol_s)) / len(top)


def top_clip_moments(
    moment_clip_ids: Mapping[str, Sequence[str]],
    clip_views: Mapping[str, int],
    *,
    n_clips: int = TOP_CLIPS,
) -> set[str]:
    """The moments holding the VOD's `n_clips` most-viewed clips.

    The top clips are chosen among all of the VOD's clips in `clip_views` (ties in views go to
    the smaller clip ID). A top clip in no moment (it has no VOD offset) holds no moment, so the
    set can have fewer entries; it is never replaced by the next clip down.

    Args:
        moment_clip_ids: ``{moment_id: clip_ids}`` for the VOD's moments.
        clip_views: ``{clip_id: view_count}`` for every clip of the VOD.
        n_clips: How many top clips to look at.
    """
    owner = {clip_id: m for m, ids in moment_clip_ids.items() for clip_id in ids}
    top = sorted(clip_views, key=lambda c: (-clip_views[c], c))[:n_clips]
    return {owner[c] for c in top if c in owner}


def top_clip_recall_at_k(
    candidates: Sequence[Candidate],
    moments: Sequence[Moment],
    top_moment_ids: set[str],
    *,
    k: int = TOP_K,
    tol_s: float = TOL_S,
) -> float | None:
    """Fraction of `top_moment_ids` that the top `k` candidates match (None if there are none).

    The one-to-one matching runs against all moments, so a candidate claims at most one.
    """
    if not top_moment_ids:
        return None
    matched = set(match_candidates(rank_candidates(candidates)[:k], moments, tol_s=tol_s).values())
    return len(matched & top_moment_ids) / len(top_moment_ids)
