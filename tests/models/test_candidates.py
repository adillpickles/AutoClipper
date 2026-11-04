"""Tests for candidate generation on synthetic window scores with planted peaks."""

import json
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import polars as pl
import pytest

from autoclipper.analysis import AnalysisResult, ScorerInfo, VodSummary
from autoclipper.labels.moments import merge_clips_into_moments
from autoclipper.labels.windows import make_windows, windows_for_vods
from autoclipper.models.candidates import CandidateParams, MomentInput, generate_candidates
from autoclipper.synthetic import generate_dataset


def scores_with_peaks(
    duration_s: float, peaks: dict[int, float], base: float = 0.0
) -> pl.DataFrame:
    """10 s windows scoring `base`, with a planted score at the given window indexes."""
    windows = make_windows("v", duration_s)
    values = np.full(windows.height, base)
    for index, value in peaks.items():
        values[index] = value
    return windows.select("window_id", "start_s", "end_s").with_columns(score=pl.Series(values))


def test_planted_peaks_become_ranked_candidates_starting_20_s_before() -> None:
    scores = scores_with_peaks(600.0, {10: 5.0, 30: 9.0, 50: 3.0})

    candidates = generate_candidates(scores, duration_s=600.0)

    assert [(c.rank, c.score) for c in candidates[:3]] == [(1, 9.0), (2, 5.0), (3, 3.0)]
    top = candidates[0]
    assert top.start_s == 300.0 - 20.0
    assert 20.0 <= top.end_s - top.start_s <= 45.0
    assert top.start_s <= 300.0
    assert top.end_s >= 310.0
    assert all(c.source == "discovered" and c.n_viewer_clips == 0 for c in candidates)
    assert [c.candidate_id for c in candidates] == [
        f"c{i:03d}" for i in range(1, len(candidates) + 1)
    ]


def test_peaks_within_60_s_of_a_higher_one_are_suppressed() -> None:
    # Peaks at 100 s and 150 s (50 s apart): only the higher survives; 300 s is far enough.
    scores = scores_with_peaks(600.0, {10: 4.0, 15: 6.0, 30: 2.0})

    candidates = generate_candidates(
        scores, duration_s=600.0, params=CandidateParams(smooth_windows=1)
    )

    assert [c.score for c in candidates[:2]] == [6.0, 2.0]
    assert all(abs(c.start_s - (100.0 - 20.0)) > 1 for c in candidates)


def test_a_long_high_stretch_extends_the_end_but_stays_under_45_s() -> None:
    scores = scores_with_peaks(600.0, dict.fromkeys(range(20, 30), 5.0))

    (first, *_) = generate_candidates(
        scores, duration_s=600.0, params=CandidateParams(smooth_windows=1)
    )

    assert first.start_s == 200.0 - 20.0
    assert first.end_s - first.start_s == 45.0


def test_a_peak_at_the_vod_start_is_moved_inside_the_vod() -> None:
    scores = scores_with_peaks(300.0, {0: 7.0})

    (first, *_) = generate_candidates(scores, duration_s=300.0)

    assert first.start_s == 0.0
    assert 20.0 <= first.end_s <= 45.0


def test_a_peak_at_the_vod_end_ends_with_the_vod() -> None:
    scores = scores_with_peaks(300.0, {29: 7.0})

    (first, *_) = generate_candidates(scores, duration_s=300.0)

    assert first.end_s == 300.0
    assert first.start_s <= 290.0


def test_shortlist_keeps_the_best_50() -> None:
    peaks = {i: float(i) for i in range(0, 600, 7)}  # 86 peaks 70 s apart
    scores = scores_with_peaks(6000.0, peaks, base=-1.0)

    candidates = generate_candidates(
        scores, duration_s=6000.0, params=CandidateParams(smooth_windows=1)
    )

    assert len(candidates) == 50
    assert candidates[0].score == 595.0
    assert min(c.score for c in candidates) == float(sorted(peaks.values())[-50])


def test_a_flat_vod_gives_one_candidate_and_an_empty_one_none() -> None:
    assert len(generate_candidates(scores_with_peaks(300.0, {}), duration_s=300.0)) == 1
    empty = scores_with_peaks(300.0, {}).clear()
    assert generate_candidates(empty, duration_s=300.0) == []


def test_viewer_clip_moments_mark_overlaps_as_both_and_add_the_rest() -> None:
    scores = scores_with_peaks(600.0, {30: 9.0, 45: 2.5})
    moments = [
        MomentInput("v:m0", 290.0, 320.0, ("a", "b")),  # overlaps the 300 s candidate
        MomentInput("v:m1", 500.0, 530.0, ("c",)),  # nothing discovered there
    ]

    candidates = generate_candidates(scores, duration_s=600.0, moments=moments)

    by_source = {c.source: c for c in candidates}
    assert by_source["both"].n_viewer_clips == 2
    assert by_source["both"].score == 9.0
    clipped = by_source["viewer_clips"]
    assert clipped.n_viewer_clips == 1
    assert clipped.start_s >= 500.0 - 20.0
    assert clipped.end_s <= 600.0
    assert 20.0 <= clipped.end_s - clipped.start_s <= 45.0


def test_a_vod_with_zero_viewer_clips_gives_only_discovered_candidates() -> None:
    candidates = generate_candidates(
        scores_with_peaks(600.0, {20: 4.0}), duration_s=600.0, moments=[]
    )

    assert candidates
    assert {c.source for c in candidates} == {"discovered"}


def test_top_signals_name_the_strong_features_strongest_first() -> None:
    scores = scores_with_peaks(300.0, {10: 5.0})
    features = scores.select("window_id").with_columns(
        chat_msg_rate_z=pl.Series([3.0 if i == 10 else 0.0 for i in range(30)]),
        audio_rms_max_z=pl.Series([1.5 if i == 11 else 0.0 for i in range(30)]),
        chat_unique_rate_z=pl.Series([0.5] * 30),  # below the 1.0 threshold
    )

    candidates = generate_candidates(scores, duration_s=300.0, features=features)

    assert candidates[0].top_signals == ("chat_msg_rate_z", "audio_rms_max_z")


def test_without_features_top_signals_are_empty() -> None:
    candidates = generate_candidates(scores_with_peaks(300.0, {10: 5.0}), duration_s=300.0)

    assert all(c.top_signals == () for c in candidates)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda s: s.drop("score"), "missing columns: score"),
        (lambda s: s.with_columns(score=pl.lit(float("nan"))), "finite"),
        (lambda s: pl.concat([s, s]), "overlap"),
    ],
)
def test_bad_scores_are_rejected(change: object, message: str) -> None:
    scores = scores_with_peaks(100.0, {})

    with pytest.raises(ValueError, match=message):
        generate_candidates(change(scores), duration_s=100.0)  # type: ignore[operator]


def test_candidates_make_a_valid_analysis_result_on_the_synthetic_dataset(tmp_path: Path) -> None:
    # Score = chat burst + loudness, like the chat_loudness baseline, from planted signals.
    dataset = generate_dataset(tmp_path / "synthetic", seed=1)
    root = dataset.out_dir
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    vods = pl.read_parquet(root / "processed" / "vods.parquet")
    clips = pl.read_parquet(root / "processed" / "clips" / "synthetic-seed-1.parquet")
    moments = merge_clips_into_moments(clips).moments

    for vod in manifest["vods"]:
        video_id = vod["video_id"]
        windows = windows_for_vods(vods.filter(pl.col("video_id") == video_id))
        planted = [e["time_s"] for e in vod["planted_events"]]
        scores = windows.select(
            "window_id",
            "start_s",
            "end_s",
            score=pl.sum_horizontal(
                *[
                    ((pl.col("start_s") <= t) & (pl.col("end_s") > t)).cast(pl.Float64) * 3
                    for t in planted
                ]
            ),
        )
        own = [
            MomentInput(r["moment_id"], r["start_s"], r["end_s"], tuple(r["clip_ids"]))
            for r in moments.filter(pl.col("video_id") == video_id).iter_rows(named=True)
        ]
        row = vods.filter(pl.col("video_id") == video_id).row(0, named=True)
        candidates = generate_candidates(scores, duration_s=row["duration_s"], moments=own)

        result = AnalysisResult(
            generated_at=datetime(2026, 11, 4, tzinfo=UTC),
            vod=VodSummary(
                video_id=video_id,
                title=row["title"],
                broadcaster_id=row["broadcaster_id"],
                broadcaster_display_name="user_001",
                duration_s=row["duration_s"],
                created_at=row["created_at"],
            ),
            scorer=ScorerInfo(kind="heuristic", name="chat_loudness"),
            n_viewer_clips_total=sum(len(m.clip_ids) for m in own),
            candidates=tuple(candidates),
        )
        # Every planted highlight is inside one of the top candidates.
        for t in planted:
            assert any(c.start_s <= t < c.end_s for c in result.candidates[: len(planted)]), (
                video_id,
                t,
            )
        if not own:
            assert {c.source for c in result.candidates} == {"discovered"}
        json.loads(result.model_dump_json())  # serializes
