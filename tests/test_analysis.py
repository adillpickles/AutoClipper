"""Tests for the AnalysisResult models and the invariants of api-contract.md § 4.5."""

from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from autoclipper.analysis import (
    AnalysisResult,
    AnalysisWarning,
    Candidate,
    ScorerInfo,
    VodSummary,
)

VOD = VodSummary(
    video_id="9000000001",
    title="Synthetic stream 1",
    broadcaster_id="9100000001",
    broadcaster_display_name="user_001",
    duration_s=360.0,
    created_at=datetime(2026, 10, 12, 18, tzinfo=UTC),
)


def candidate(rank: int, score: float = 1.0, start: float = 10.0, **changes: object) -> Candidate:
    data: dict[str, object] = {
        "candidate_id": f"c{rank:03d}",
        "rank": rank,
        "score": score,
        "start_s": start,
        "end_s": start + 30.0,
        "source": "discovered",
        "n_viewer_clips": 0,
        "top_signals": (),
    }
    data.update(changes)
    return Candidate(**data)  # type: ignore[arg-type]


def result(*candidates: Candidate, **changes: object) -> AnalysisResult:
    data: dict[str, object] = {
        "generated_at": datetime(2026, 10, 19, 15, 2, 41, tzinfo=UTC),
        "vod": VOD,
        "scorer": ScorerInfo(kind="mock", name="mock"),
        "n_viewer_clips_total": 4,
        "candidates": candidates,
    }
    data.update(changes)
    return AnalysisResult(**data)  # type: ignore[arg-type]


def test_a_valid_result_serializes_like_the_contract_example() -> None:
    doc = result(
        candidate(
            1, 3.42, 118.0, source="both", n_viewer_clips=3, top_signals=("chat_msg_rate_z",)
        ),
        candidate(2, 2.87, 251.5),
    )

    data = doc.model_dump(mode="json")

    assert data["schema_version"] == "1.0"
    assert data["generated_at"] == "2026-10-19T15:02:41Z"
    assert data["vod"]["created_at"] == "2026-10-12T18:00:00Z"
    assert data["candidates"][0]["top_signals"] == ["chat_msg_rate_z"]
    assert AnalysisResult.model_validate_json(doc.model_dump_json()) == doc


def test_timestamps_are_normalized_to_utc_and_naive_ones_rejected() -> None:
    eastern = datetime(2026, 10, 19, 11, 2, 41, tzinfo=timezone(timedelta(hours=-4)))

    assert (
        result(generated_at=eastern).model_dump(mode="json")["generated_at"]
        == "2026-10-19T15:02:41Z"
    )
    with pytest.raises(ValidationError, match="timezone-aware"):
        result(generated_at=datetime(2026, 10, 19))  # noqa: DTZ001


@pytest.mark.parametrize(
    ("candidates", "message"),
    [
        ((candidate(1, 1.0), candidate(1, 0.5, candidate_id="c009")), "rank"),
        ((candidate(1, 1.0), candidate(2, 2.0)), "ordered"),
        ((candidate(1, 1.0, 50.0), candidate(2, 1.0, 10.0)), "ordered"),
        ((candidate(1, 1.0), candidate(2, 0.5, candidate_id="c001")), "unique"),
        ((candidate(1, 1.0, 340.0),), "ends after the VOD"),
        ((candidate(1, 1.0, source="both", n_viewer_clips=5),), "more viewer clips"),
    ],
)
def test_result_invariants_are_enforced(candidates: tuple[Candidate, ...], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        result(*candidates)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"end_s": 10.0}, "end_s must be after"),
        ({"source": "both"}, "exactly when"),
        ({"n_viewer_clips": 2}, "exactly when"),
        ({"top_signals": ("a", "a")}, "distinct"),
        ({"score": float("inf")}, "finite"),
        ({"start_s": -1.0}, "greater than or equal"),
    ],
)
def test_candidate_invariants_are_enforced(changes: dict[str, object], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        candidate(1, **changes)  # type: ignore[arg-type]


def test_missing_viewer_clips_need_the_warning_and_only_discovered_candidates() -> None:
    warning = AnalysisWarning(code="viewer_clips_missing", message="Clips couldn't be fetched.")

    ok = result(candidate(1, 1.0), n_viewer_clips_total=None, warnings=(warning,))
    assert ok.n_viewer_clips_total is None
    with pytest.raises(ValidationError, match="exactly when warnings"):
        result(candidate(1, 1.0), n_viewer_clips_total=None)
    with pytest.raises(ValidationError, match="must be 'discovered'"):
        result(
            candidate(1, 1.0, source="viewer_clips", n_viewer_clips=1),
            n_viewer_clips_total=None,
            warnings=(warning,),
        )


def test_zero_candidates_are_valid_and_more_than_200_are_not() -> None:
    assert result().candidates == ()
    many = [candidate(i, 1000.0 - i, start=0.0) for i in range(1, 202)]
    with pytest.raises(ValidationError, match="200"):
        result(*many)
