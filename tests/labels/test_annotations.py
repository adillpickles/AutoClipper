import math
from datetime import UTC, datetime

import polars as pl
import pytest
from polars.testing import assert_frame_equal

from autoclipper.labels.annotations import LABELS_SCHEMA, label_windows
from autoclipper.labels.moments import merge_clips_into_moments
from autoclipper.labels.windows import make_windows


def clips_frame(*rows: tuple[str, float, float, int]) -> pl.DataFrame:
    return pl.DataFrame(
        [
            {
                "clip_id": cid,
                "video_id": "v",
                "broadcaster_id": "b",
                "vod_offset_s": start,
                "duration_s": duration,
                "view_count": views,
                "created_at": datetime(2026, 1, 1, tzinfo=UTC),
                "fetched_at": datetime(2026, 1, 10, tzinfo=UTC),
            }
            for cid, start, duration, views in rows
        ],
        schema={
            "clip_id": pl.String,
            "video_id": pl.String,
            "broadcaster_id": pl.String,
            "vod_offset_s": pl.Float64,
            "duration_s": pl.Float64,
            "view_count": pl.Int64,
            "created_at": pl.Datetime("us", "UTC"),
            "fetched_at": pl.Datetime("us", "UTC"),
        },
    )


def labels(clips: pl.DataFrame, *, mature: bool = True) -> pl.DataFrame:
    return label_windows(
        make_windows("v", 300),
        merge_clips_into_moments(clips.filter(pl.col("video_id") == "v")).moments,
        clips,
        broadcaster_id="b",
        split="train",
        mature=mature,
    )


def test_half_open_overlap_and_exact_60_second_negative_buffer() -> None:
    frame = labels(clips_frame(("a", 100, 20, 10)))
    assert frame.schema == LABELS_SCHEMA
    assert frame.filter(pl.col("label_pos"))["window_id"].to_list() == ["v:10", "v:11"]
    eligible = set(frame.filter(pl.col("train_eligible"))["window_id"].to_list())
    assert {"v:2", "v:3", "v:18", "v:19", "v:10", "v:11"} <= eligible
    assert not {"v:4", "v:9", "v:12", "v:17"} & eligible
    # Both touching windows remain labeled negative, but aren't training examples.
    assert frame.filter(pl.col("window_id").is_in(["v:9", "v:12"]))["label_pos"].to_list() == [
        False,
        False,
    ]
    assert frame.height == 30


def test_clip_counts_are_actual_overlaps_not_whole_moment_counts() -> None:
    frame = labels(clips_frame(("a", 100, 10, 10), ("b", 113, 7, 30)))
    assert frame.filter(pl.col("label_pos"))["label_n_clips"].to_list() == [1, 1]
    # Windows overlapping the small gap in a connected moment are still positive.
    win = (
        make_windows("v", 120)
        .filter(pl.col("index") == 11)
        .with_columns(start_s=pl.lit(110.0), end_s=pl.lit(113.0))
    )
    clips = clips_frame(("a", 100, 10, 10), ("b", 113, 7, 30))
    result = label_windows(
        win,
        merge_clips_into_moments(clips).moments,
        clips,
        broadcaster_id="b",
        split="val",
        mature=True,
    )
    assert result["label_pos"].to_list() == [True]
    assert result["label_n_clips"].to_list() == [0]


@pytest.mark.parametrize("third_clip", [False, True])
def test_selects_most_clips_then_earliest_moment(third_clip: bool) -> None:
    clips = clips_frame(
        ("a", 100, 1, 10), ("b", 107, 1, 20), *(("c", 107.2, 0.5, 30),) if third_clip else ()
    )
    frame = labels(clips)
    positive = frame.filter(pl.col("window_id") == "v:10").row(0, named=True)
    assert positive["label_moment_id"] == ("v:m1" if third_clip else "v:m0")
    assert positive["label_n_clips"] == (3 if third_clip else 2)


def test_strength_uses_own_snapshot_channel_median_including_unplaced_clips() -> None:
    clips = clips_frame(("a", 100, 20, 10), ("b", 105, 10, 30))
    unplaced = clips.head(1).with_columns(
        clip_id=pl.lit("unplaced"),
        vod_offset_s=pl.lit(None, dtype=pl.Float64),
        view_count=pl.lit(50, dtype=pl.Int64),
    )
    other = clips.head(1).with_columns(
        clip_id=pl.lit("other"),
        broadcaster_id=pl.lit("other"),
        video_id=pl.lit("other-v"),
        view_count=pl.lit(999999, dtype=pl.Int64),
    )
    result = labels(pl.concat([clips, unplaced, other]))
    row = result.filter(pl.col("window_id") == "v:10").row(0, named=True)
    assert row["moment_strength"] == 1
    assert row["sample_weight"] == pytest.approx(1 + math.log1p(1))
    assert result["sample_weight"][0] == 1
    assert result["sample_weight"][9] is None


def test_zero_views_still_produce_finite_positive_weights() -> None:
    frame = labels(clips_frame(("a", 100, 20, 0)))
    row = frame.filter(pl.col("label_pos")).row(0, named=True)
    assert row["moment_strength"] == 0
    assert row["sample_weight"] == 1


def test_immature_capture_has_binary_labels_but_no_training_weights() -> None:
    frame = labels(clips_frame(("a", 100, 20, 10)), mature=False)
    assert frame["label_pos"].sum() == 2
    assert not frame["train_eligible"].any()
    assert frame["sample_weight"].null_count() == frame.height
    assert frame["moment_strength"].null_count() == frame.height


def test_covered_empty_snapshot_is_negative_and_order_is_deterministic() -> None:
    empty = labels(clips_frame())
    assert not empty["label_pos"].any()
    assert empty["train_eligible"].all()
    clips = clips_frame(("a", 100, 10, 10), ("b", 107, 20, 20))
    assert_frame_equal(labels(clips), labels(clips.reverse()))


def test_bad_keys_and_intervals_are_rejected() -> None:
    clips = clips_frame(("a", 100, 20, 10))
    with pytest.raises(ValueError, match="unique"):
        labels(pl.concat([clips, clips]))
    with pytest.raises(ValueError, match="invalid"):
        label_windows(
            make_windows("v", 30).with_columns(end_s=pl.lit(float("nan"))),
            merge_clips_into_moments(clips).moments,
            clips,
            broadcaster_id="b",
            split="train",
            mature=True,
        )
