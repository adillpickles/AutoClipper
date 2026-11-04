"""Tests for merging viewer clips into moments (docs/data/labeling.md § 2)."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import polars as pl
import pytest

from autoclipper.labels.moments import GAP_S, MOMENTS_SCHEMA, merge_clips_into_moments
from autoclipper.synthetic import generate_dataset

T0 = datetime(2026, 10, 1, 18, 0, tzinfo=UTC)
FETCHED = datetime(2026, 10, 9, 18, 0, tzinfo=UTC)


def clips_frame(
    *rows: tuple[str, str | None, float | None, float], views: list[int] | None = None
) -> pl.DataFrame:
    """Clips as (clip_id, video_id, vod_offset_s, duration_s), created a minute apart."""
    n = len(rows)
    return pl.DataFrame(
        {
            "clip_id": [r[0] for r in rows],
            "video_id": [r[1] for r in rows],
            "vod_offset_s": [r[2] for r in rows],
            "duration_s": [r[3] for r in rows],
            "view_count": views if views is not None else [10 * (i + 1) for i in range(n)],
            "created_at": [T0 + timedelta(minutes=i) for i in range(n)],
            "fetched_at": [FETCHED + timedelta(seconds=i) for i in range(n)],
        },
        schema={
            "clip_id": pl.String,
            "video_id": pl.String,
            "vod_offset_s": pl.Float64,
            "duration_s": pl.Float64,
            "view_count": pl.Int64,
            "created_at": pl.Datetime("us", "UTC"),
            "fetched_at": pl.Datetime("us", "UTC"),
        },
    )


def spans(result_moments: pl.DataFrame) -> list[tuple[float, float, list[str]]]:
    return [
        (row["start_s"], row["end_s"], row["clip_ids"])
        for row in result_moments.iter_rows(named=True)
    ]


def test_overlapping_clips_become_one_moment_with_counts_and_views() -> None:
    clips = clips_frame(("a", "v", 100.0, 30.0), ("b", "v", 110.0, 30.0), views=[5, 7])

    result = merge_clips_into_moments(clips)

    moments = result.moments
    assert moments.schema == MOMENTS_SCHEMA
    (row,) = moments.iter_rows(named=True)
    assert row["moment_id"] == "v:m0"
    assert (row["start_s"], row["end_s"]) == (100.0, 140.0)
    assert (row["n_clips"], row["total_views"], row["max_views"]) == (2, 12, 7)
    assert row["clip_ids"] == ["a", "b"]
    assert row["first_clip_created_at"] == T0
    assert row["views_observed_at"] == FETCHED + timedelta(seconds=1)


@pytest.mark.parametrize(
    ("second_start", "n_moments"),
    [
        (130.0, 1),  # touching the first clip's end: merged
        (130.0 + GAP_S, 1),  # exactly GAP_S after: merged
        (130.0 + GAP_S + 0.001, 2),  # just past GAP_S: separate
        (133.0, 1),  # a 3 s gap, like the synthetic near-touch case
    ],
    ids=["touching", "gap_equal", "gap_over", "gap_3s"],
)
def test_clips_within_the_gap_merge_and_farther_ones_dont(
    second_start: float, n_moments: int
) -> None:
    clips = clips_frame(("a", "v", 100.0, 30.0), ("b", "v", second_start, 20.0))

    assert merge_clips_into_moments(clips).moments.height == n_moments


def test_long_chain_of_overlapping_clips_stays_one_moment() -> None:
    # 20 clips, each starting 25 s after the previous: 500+ s, far longer than any clip.
    clips = clips_frame(*[(f"c{i:02d}", "v", 25.0 * i, 30.0) for i in range(20)])

    (start, end, ids) = spans(merge_clips_into_moments(clips).moments)[0]

    assert (start, end) == (0.0, 25.0 * 19 + 30.0)
    assert len(ids) == 20


def test_a_long_clip_keeps_the_moment_open_past_shorter_ones_inside_it() -> None:
    # b lies inside a; c starts after b ends but inside a: all one moment.
    clips = clips_frame(("a", "v", 0.0, 60.0), ("b", "v", 5.0, 5.0), ("c", "v", 40.0, 10.0))

    assert spans(merge_clips_into_moments(clips).moments) == [(0.0, 60.0, ["a", "b", "c"])]


def test_moment_ids_follow_start_order_per_vod() -> None:
    clips = clips_frame(
        ("late", "v1", 500.0, 30.0),
        ("early", "v1", 10.0, 30.0),
        ("other", "v2", 0.0, 30.0),
    )

    moments = merge_clips_into_moments(clips).moments

    assert moments["moment_id"].to_list() == ["v1:m0", "v1:m1", "v2:m0"]
    assert moments["clip_ids"].to_list() == [["early"], ["late"], ["other"]]


def test_clips_of_different_vods_never_merge() -> None:
    clips = clips_frame(("a", "v1", 100.0, 30.0), ("b", "v2", 100.0, 30.0))

    assert merge_clips_into_moments(clips).moments.height == 2


def test_ties_are_ordered_deterministically_whatever_the_input_order() -> None:
    clips = clips_frame(("b", "v", 100.0, 30.0), ("a", "v", 100.0, 30.0), ("c", "v", 300.0, 10.0))

    forward = merge_clips_into_moments(clips).moments
    backward = merge_clips_into_moments(clips.reverse()).moments
    shuffled = merge_clips_into_moments(clips.sample(fraction=1.0, shuffle=True, seed=3)).moments

    assert forward.equals(backward)
    assert forward.equals(shuffled)
    assert forward["clip_ids"].to_list()[0] == ["a", "b"]


def test_clips_without_an_offset_or_video_are_skipped_and_counted() -> None:
    clips = clips_frame(
        ("a", "v", 100.0, 30.0),
        ("no_offset", "v", None, 30.0),
        ("no_video", None, None, 20.0),
    )

    result = merge_clips_into_moments(clips)

    assert result.moments["clip_ids"].to_list() == [["a"]]
    assert (result.n_without_vod_offset, result.n_without_video) == (1, 1)


def test_repeated_listing_of_a_clip_counts_once_with_its_latest_views() -> None:
    clips = clips_frame(("a", "v", 100.0, 30.0), ("a", "v", 100.0, 30.0), views=[5, 9])

    result = merge_clips_into_moments(clips)

    (row,) = result.moments.iter_rows(named=True)
    assert (row["n_clips"], row["total_views"]) == (1, 9)
    assert result.n_repeated == 1


def test_one_clip_with_two_intervals_is_an_error() -> None:
    clips = clips_frame(("a", "v", 100.0, 30.0), ("a", "v", 200.0, 30.0))

    with pytest.raises(ValueError, match="different intervals"):
        merge_clips_into_moments(clips)


@pytest.mark.parametrize(
    ("row", "message"),
    [(("a", "v", -1.0, 30.0), "negative"), (("a", "v", 1.0, 0.0), "positive")],
)
def test_impossible_clip_intervals_are_rejected(
    row: tuple[str, str, float, float], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        merge_clips_into_moments(clips_frame(row))


def test_missing_columns_are_named() -> None:
    with pytest.raises(ValueError, match="missing columns: view_count"):
        merge_clips_into_moments(clips_frame(("a", "v", 1.0, 2.0)).drop("view_count"))


def test_no_clips_give_no_moments() -> None:
    result = merge_clips_into_moments(clips_frame())

    assert result.moments.height == 0
    assert result.moments.schema == MOMENTS_SCHEMA


def test_synthetic_dataset_gives_one_moment_per_clipped_highlight(tmp_path: Path) -> None:
    dataset = generate_dataset(tmp_path / "synthetic", seed=1)
    clips = pl.read_parquet(dataset.out_dir / "processed" / "clips" / "synthetic-seed-1.parquet")
    manifest = json.loads((dataset.out_dir / "manifest.json").read_text(encoding="utf-8"))

    result = merge_clips_into_moments(clips)

    # The planted overlapping, touching and near-touching (3 s gap) clips each form one moment,
    # the disjoint clip its own; the VOD without clips has none.
    expected = {
        vod["video_id"]: [sorted(e["clip_ids"]) for e in vod["planted_events"] if e["clip_ids"]]
        for vod in manifest["vods"]
    }
    for video_id, groups in expected.items():
        own = result.moments.filter(pl.col("video_id") == video_id)
        assert sorted(sorted(ids) for ids in own["clip_ids"].to_list()) == sorted(groups)
        for event in (
            e for v in manifest["vods"] if v["video_id"] == video_id for e in v["planted_events"]
        ):
            if event["clip_ids"]:
                moment = own.filter(pl.col("clip_ids").list.contains(event["clip_ids"][0]))
                assert moment["start_s"][0] <= event["time_s"] <= moment["end_s"][0]
    assert (result.n_without_vod_offset, result.n_without_video) == (1, 1)
