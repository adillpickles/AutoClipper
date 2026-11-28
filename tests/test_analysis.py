"""Tests for the AnalysisResult models and the invariants of api-contract.md § 4.5."""

from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from autoclipper.analysis import (
    Activity,
    AnalysisResult,
    AnalysisWarning,
    Candidate,
    ScorerInfo,
    SignalDetail,
    SpeechFeatures,
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
            1,
            3.42,
            118.0,
            source="both",
            n_viewer_clips=3,
            top_signals=("chat_msg_rate_z",),
            title="Loud audio & laughing chat",
            reason=(
                "Chat jumped to 6\u00d7 its usual rate with lots of laugh reactions; 3 viewer clips"
            ),
            signal_details=(
                SignalDetail(
                    kind="chat_rate", label="Chat rate", detail="6\u00d7 usual stream rate"
                ),
            ),
        ),
        candidate(2, 2.87, 251.5),
    )

    data = doc.model_dump(mode="json")

    assert data["schema_version"] == "1.1"
    assert data["generated_at"] == "2026-10-19T15:02:41Z"
    assert data["vod"]["created_at"] == "2026-10-12T18:00:00Z"
    assert data["candidates"][0]["top_signals"] == ["chat_msg_rate_z"]
    assert data["candidates"][0]["title"] == "Loud audio & laughing chat"
    assert data["candidates"][0]["reason"] == (
        "Chat jumped to 6\u00d7 its usual rate with lots of laugh reactions; 3 viewer clips"
    )
    assert data["candidates"][0]["signal_details"] == [
        {"kind": "chat_rate", "label": "Chat rate", "detail": "6\u00d7 usual stream rate"}
    ]
    assert data["candidates"][1]["title"] is None
    assert data["candidates"][1]["reason"] is None
    assert data["candidates"][1]["signal_details"] == []
    assert AnalysisResult.model_validate_json(doc.model_dump_json()) == doc


def test_old_v1_0_result_json_without_explanation_fields_still_loads() -> None:
    legacy_json = """
    {
      "schema_version": "1.0",
      "generated_at": "2026-10-19T15:02:41Z",
      "vod": {
        "video_id": "9000000001",
        "title": "Synthetic stream 1",
        "broadcaster_id": "9100000001",
        "broadcaster_display_name": "user_001",
        "duration_s": 360.0,
        "created_at": "2026-10-12T18:00:00Z"
      },
      "scorer": { "kind": "mock", "name": "mock", "run_id": null },
      "n_viewer_clips_total": 3,
      "warnings": [],
      "candidates": [
        {
          "candidate_id": "c001",
          "rank": 1,
          "score": 3.42,
          "start_s": 118.0,
          "end_s": 148.0,
          "source": "both",
          "n_viewer_clips": 3,
          "top_signals": ["chat_msg_rate_z", "audio_rms_max_z"]
        }
      ]
    }
    """

    parsed = AnalysisResult.model_validate_json(legacy_json)

    assert parsed.schema_version == "1.0"
    assert parsed.candidates[0].title is None
    assert parsed.candidates[0].reason is None
    assert parsed.candidates[0].signal_details == ()


def test_blank_explanation_fields_are_rejected() -> None:
    with pytest.raises(ValidationError, match="blank"):
        candidate(1, title="   ")
    with pytest.raises(ValidationError, match="blank"):
        candidate(1, reason="")
    with pytest.raises(ValidationError, match="blank"):
        SignalDetail(kind="chat_rate", label="Chat rate", detail="   ")


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


def test_speech_features_are_omitted_when_none_and_roundtrip_when_set() -> None:
    plain = result(candidate(1, 1.0))
    assert "speech_features" not in plain.model_dump(mode="json")["candidates"][0]

    sf = SpeechFeatures(
        speech_word_rate=3.0,
        speech_exclaim_density=0.25,
        speech_question_density=0.1,
        speech_keyword_rate=0.08,
        speech_frac=0.72,
        speech_peak_loudness_align=0.95,
    )
    enriched = result(candidate(1, 1.0, speech_features=sf))
    dumped = enriched.model_dump_json()
    assert AnalysisResult.model_validate_json(dumped) == enriched
    assert (
        enriched.model_dump(mode="json")["candidates"][0]["speech_features"]["speech_frac"] == 0.72
    )


def test_pipeline_counts_are_omitted_when_unknown_and_roundtrip_when_set() -> None:
    plain = result(candidate(1, 1.0))
    dumped = plain.model_dump(mode="json")
    assert "n_chat_messages" not in dumped
    assert "n_windows" not in dumped
    assert plain.n_chat_messages is None
    assert plain.n_windows is None

    counted = result(candidate(1, 1.0), n_chat_messages=2418, n_windows=36)
    assert AnalysisResult.model_validate_json(counted.model_dump_json()) == counted
    assert counted.model_dump(mode="json")["n_windows"] == 36
    with pytest.raises(ValidationError):
        result(n_chat_messages=-1)


# --- Activity (#213) -----------------------------------------------------------------------------


def test_activity_is_omitted_when_absent_and_old_files_load() -> None:
    plain = result(candidate(1, 1.0))
    assert "activity" not in plain.model_dump(mode="json")
    old = plain.model_dump(mode="json")
    old["schema_version"] = "1.0"
    assert AnalysisResult.model_validate(old).activity is None


def test_activity_roundtrips_with_gaps_and_a_missing_family() -> None:
    activity = Activity(step_s=10.0, chat=None, loudness=(-30.5, None, *([-20.0] * 34)))
    with_activity = result(candidate(1, 1.0), activity=activity)
    dumped = with_activity.model_dump(mode="json")
    assert dumped["activity"]["chat"] is None
    assert dumped["activity"]["loudness"][1] is None
    assert AnalysisResult.model_validate_json(with_activity.model_dump_json()) == with_activity


@pytest.mark.parametrize(
    "fields",
    [
        {"step_s": 10.0},  # neither family
        {"step_s": 0.0, "chat": (1.0,)},
        {"step_s": 10.0, "chat": (1.0, 2.0), "loudness": (-20.0,)},  # lengths differ
        {"step_s": 10.0, "chat": (-1.0,)},
        {"step_s": 10.0, "loudness": (-61.0,)},
        {"step_s": 10.0, "loudness": (1.0,)},
        {"step_s": 1.0, "chat": (0.0,) * 401},
        {"step_s": 10.0, "chat": (float("nan"),)},
    ],
)
def test_invalid_activity_is_rejected(fields: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Activity.model_validate(fields)


@pytest.mark.parametrize("n_points", [35, 37])
def test_activity_must_tile_the_vod(n_points: int) -> None:
    with pytest.raises(ValidationError, match="one point per step_s"):
        result(activity=Activity(step_s=10.0, chat=(0.0,) * n_points))
