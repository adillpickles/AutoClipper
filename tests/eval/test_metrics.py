"""Hand-worked examples for the top-10 metrics and the bootstrap (docs/ml/evaluation.md § 3)."""

import pytest

from autoclipper.eval.bootstrap import bootstrap_mean
from autoclipper.eval.metrics import (
    Candidate,
    Moment,
    hit_rate_at_k,
    match_candidates,
    rank_candidates,
    top_clip_moments,
    top_clip_recall_at_k,
)


def c(cid: str, start: float, end: float, score: float) -> Candidate:
    return Candidate(cid, start, end, score)


def m(mid: str, start: float, end: float) -> Moment:
    return Moment(mid, start, end)


@pytest.mark.parametrize(
    ("candidate", "matches"),
    [
        ((100.0, 130.0), True),  # overlaps the moment
        ((60.0, 85.1), True),  # ends 14.9 s before the moment: within ±15 s
        ((60.0, 85.0), False),  # ends exactly 15 s before: the widened edges only touch
        ((214.9, 240.0), True),  # starts 14.9 s after the moment's end
        ((215.0, 240.0), False),  # starts exactly 15 s after
    ],
)
def test_matching_tolerance_is_15_seconds_on_each_side(
    candidate: tuple[float, float], matches: bool
) -> None:
    moments = [m("v:m0", 100.0, 200.0)]

    result = match_candidates([c("a", *candidate, 1.0)], moments)

    assert (result == {"a": "v:m0"}) is matches


def test_a_moment_is_matched_at_most_once_by_the_higher_scored_candidate() -> None:
    moments = [m("v:m0", 100.0, 130.0)]
    candidates = [c("low", 100.0, 130.0, 0.2), c("high", 105.0, 125.0, 0.9)]

    assert match_candidates(candidates, moments) == {"high": "v:m0"}
    assert hit_rate_at_k(candidates, moments) == 0.5


def test_a_candidate_claims_the_moment_it_overlaps_most() -> None:
    # The candidate touches both moments' widened spans; it overlaps m1 more.
    moments = [m("v:m0", 0.0, 30.0), m("v:m1", 60.0, 120.0)]
    candidates = [c("a", 40.0, 80.0, 1.0), c("b", 0.0, 20.0, 0.5)]

    assert match_candidates(candidates, moments) == {"a": "v:m1", "b": "v:m0"}


def test_equal_overlaps_go_to_the_earlier_moment() -> None:
    moments = [m("v:m1", 60.0, 70.0), m("v:m0", 20.0, 30.0)]

    # [40, 50) overlaps both widened spans ([5, 45) and [45, 85)) by 5 s.
    assert match_candidates([c("a", 40.0, 50.0, 1.0)], moments) == {"a": "v:m0"}


def test_score_ties_rank_the_earlier_start_then_the_smaller_id_first() -> None:
    candidates = [c("b", 50.0, 60.0, 1.0), c("a", 50.0, 60.0, 1.0), c("z", 10.0, 20.0, 1.0)]

    assert [x.candidate_id for x in rank_candidates(candidates)] == ["z", "a", "b"]


def test_hit_rate_uses_only_the_top_10_by_score() -> None:
    moments = [m(f"v:m{i}", 1000.0 * i, 1000.0 * i + 30) for i in range(11)]
    # 10 misses with high scores, then a hit ranked 11th.
    candidates = [c(f"miss{i}", 500.0 + 1000 * i, 520.0 + 1000 * i, 10.0 - i) for i in range(10)]
    candidates.append(c("hit", 0.0, 30.0, 0.1))

    assert hit_rate_at_k(candidates, moments) == 0.0
    assert hit_rate_at_k(candidates[-1:] + candidates[:3], moments) == 0.25


def test_fewer_than_10_candidates_divide_by_what_was_returned() -> None:
    moments = [m("v:m0", 0.0, 30.0)]

    assert hit_rate_at_k([c("a", 0.0, 30.0, 1.0), c("b", 900.0, 930.0, 0.5)], moments) == 0.5


def test_no_candidates_score_zero_and_no_moments_are_excluded() -> None:
    assert hit_rate_at_k([], [m("v:m0", 0.0, 30.0)]) == 0.0
    assert hit_rate_at_k([c("a", 0.0, 30.0, 1.0)], []) is None


def test_top_clip_moments_hold_the_5_most_viewed_clips() -> None:
    members = {"m0": ["a", "b"], "m1": ["c"], "m2": ["d"], "m3": ["e", "f"], "m4": ["g"]}
    views = {"a": 100, "b": 90, "c": 5, "d": 80, "e": 70, "f": 60, "g": 60, "x": 999}

    # Top 5 clips: a, b, d, e, then f and g tie at 60 and f wins on ID. "x" is in no moment.
    assert top_clip_moments(members, views) == {"m0", "m2", "m3"}


def test_top_clip_recall_counts_matched_top_moments() -> None:
    moments = [m("m0", 0.0, 30.0), m("m1", 100.0, 130.0), m("m2", 500.0, 530.0)]
    candidates = [c("a", 0.0, 30.0, 1.0), c("b", 505.0, 520.0, 0.5)]

    assert top_clip_recall_at_k(candidates, moments, {"m0", "m1"}) == 0.5
    assert top_clip_recall_at_k(candidates, moments, set()) is None


def test_bootstrap_is_deterministic_and_brackets_the_mean() -> None:
    values = [0.0, 0.1, 0.5, 0.9, 1.0, 0.4]

    first = bootstrap_mean(values, seed=7)
    second = bootstrap_mean(values, seed=7)
    other = bootstrap_mean(values, seed=8)

    assert first == second
    assert first is not None
    assert other is not None
    assert first.mean == pytest.approx(sum(values) / len(values))
    assert 0.0 <= first.low < first.mean < first.high <= 1.0
    assert (first.low, first.high) != (other.low, other.high)


def test_bootstrap_of_one_vod_is_a_point_and_of_none_is_none() -> None:
    interval = bootstrap_mean([0.3], seed=0)

    assert interval is not None
    assert (interval.mean, interval.low, interval.high) == pytest.approx((0.3, 0.3, 0.3))
    assert bootstrap_mean([], seed=0) is None


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [({"n_resamples": 0}, "n_resamples"), ({"confidence": 1.0}, "confidence")],
)
def test_bootstrap_rejects_bad_settings(kwargs: dict[str, float], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        bootstrap_mean([0.5], seed=0, **kwargs)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="finite"):
        bootstrap_mean([float("nan")], seed=0)
