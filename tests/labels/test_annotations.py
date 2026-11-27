import math
from datetime import UTC, datetime

import polars as pl
import pytest
from polars.testing import assert_frame_equal

from autoclipper.labels.annotations import (
    LABEL_RULES,
    LABEL_RULES_VERSION,
    LABELS_SCHEMA,
    label_windows,
)
from autoclipper.labels.moments import (
    CLIP_END,
    CLIP_START,
    ClipPositionRule,
    merge_clips_into_moments,
)
from autoclipper.labels.windows import make_windows


def clips_frame(*rows: tuple[str, float, float, int], reported: bool = False) -> pl.DataFrame:
    """Clips as (clip_id, start_s, duration_s, view_count) of VOD ``v``.

    ``vod_offset_s`` is written as the clip's end, as Helix reports it; with `reported`, the
    second value is taken as ``vod_offset_s`` itself.
    """
    return pl.DataFrame(
        [
            {
                "clip_id": cid,
                "video_id": "v",
                "broadcaster_id": "b",
                "vod_offset_s": position if reported else position + duration,
                "duration_s": duration,
                "view_count": views,
                "created_at": datetime(2026, 1, 1, tzinfo=UTC),
                "fetched_at": datetime(2026, 1, 10, tzinfo=UTC),
            }
            for cid, position, duration, views in rows
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


def test_label_windows_handles_empty_windows_and_rejects_invalid_inputs() -> None:
    clips = clips_frame(("a", 100, 20, 10))
    moments = merge_clips_into_moments(clips).moments
    win = make_windows("v", 30)

    # Empty windows frame returns an empty frame with the exact labels schema.
    empty = label_windows(
        win.head(0),
        moments.head(0),
        clips,
        broadcaster_id="b",
        split="train",
        mature=True,
    )
    assert empty.is_empty()
    assert empty.schema == LABELS_SCHEMA

    # Multiple VODs in one call are rejected.
    two_vods = pl.concat([win, make_windows("v2", 30)])
    with pytest.raises(ValueError, match="one VOD at a time"):
        label_windows(two_vods, moments, clips, broadcaster_id="b", split="train", mature=True)

    # Moments from another VOD are rejected.
    other_moments = moments.with_columns(video_id=pl.lit("other"))
    with pytest.raises(ValueError, match="moments must belong"):
        label_windows(win, other_moments, clips, broadcaster_id="b", split="train", mature=True)

    # Overlapping moments are rejected.
    overlapping = pl.concat(
        [
            moments,
            moments.with_columns(
                moment_id=pl.lit("v:m1"),
                start_s=pl.lit(110.0),
                end_s=pl.lit(130.0),
            ),
        ]
    )
    with pytest.raises(ValueError, match="must not overlap"):
        label_windows(win, overlapping, clips, broadcaster_id="b", split="train", mature=True)

    # Negative view counts are rejected.
    neg_views = clips_frame(("a", 100, 20, -1))
    with pytest.raises(ValueError, match="non-negative"):
        label_windows(win, moments, neg_views, broadcaster_id="b", split="train", mature=True)

    # Invalid placed clip intervals are rejected: a clip reported to end before the VOD starts.
    bad_clip = clips_frame(("a", -5, 20, 10), reported=True)
    with pytest.raises(ValueError, match="invalid clip interval"):
        label_windows(win, moments, bad_clip, broadcaster_id="b", split="train", mature=True)

    # Missing required columns and unexpected nulls are rejected.
    with pytest.raises(ValueError, match="missing columns"):
        label_windows(
            win.drop("end_s"), moments, clips, broadcaster_id="b", split="train", mature=True
        )
    with pytest.raises(ValueError, match="must not be null"):
        label_windows(
            win.with_columns(start_s=pl.lit(None, dtype=pl.Float64)),
            moments,
            clips,
            broadcaster_id="b",
            split="train",
            mature=True,
        )


# Clip positions (#126): label rules v1 read Helix's vod_offset as the clip's end.


def counts_by_window(frame: pl.DataFrame) -> dict[str, int]:
    return {
        row["window_id"]: row["label_n_clips"]
        for row in frame.iter_rows(named=True)
        if row["label_n_clips"]
    }


def test_label_rules_v1_is_the_default_and_reads_clip_ends() -> None:
    assert LABEL_RULES_VERSION == "v1"
    assert (LABEL_RULES["v1"], LABEL_RULES["v0"]) == (CLIP_END, CLIP_START)
    clips = clips_frame(("late", 100, 30, 10), ("early", 10, 30, 10), reported=True)

    frame = labels(clips)

    # [70, 100) overlaps windows 7-9 (half-open: window 10 only touches it); [0, 10) window 0.
    assert counts_by_window(frame) == {"v:0": 1, "v:7": 1, "v:8": 1, "v:9": 1}
    assert frame.filter(pl.col("label_pos"))["window_id"].to_list() == ["v:0", "v:7", "v:8", "v:9"]


def test_label_rules_v0_can_still_read_clip_starts() -> None:
    clips = clips_frame(("a", 100, 30, 10), reported=True)

    frame = label_windows(
        make_windows("v", 300),
        merge_clips_into_moments(clips, rule=CLIP_START).moments,
        clips,
        broadcaster_id="b",
        split="train",
        mature=True,
        rule=CLIP_START,
    )

    assert counts_by_window(frame) == {"v:10": 1, "v:11": 1, "v:12": 1}


def test_clip_counts_take_clip_positions_only_from_the_rule() -> None:
    # A rule no real reading would use places the clip at [200, 250): the counts must follow
    # it, not vod_offset_s arithmetic of their own (which would give [70, 100) or [100, 130)).
    fake = ClipPositionRule(
        "fake", start=lambda offset, duration: offset * 2, end=lambda offset, duration: offset * 2.5
    )
    clips = clips_frame(("a", 100, 30, 10), reported=True)

    frame = label_windows(
        make_windows("v", 300),
        merge_clips_into_moments(clips, rule=fake).moments,
        clips,
        broadcaster_id="b",
        split="train",
        mature=True,
        rule=fake,
    )

    assert counts_by_window(frame) == {f"v:{i}": 1 for i in range(20, 25)}


def test_a_clip_with_an_empty_interval_is_not_counted() -> None:
    clips = clips_frame(("zero", 0, 30, 10), ("a", 100, 30, 10), reported=True)

    assert counts_by_window(labels(clips)) == {"v:7": 1, "v:8": 1, "v:9": 1}


def test_moments_merged_with_another_rule_are_rejected() -> None:
    clips = clips_frame(("a", 100, 30, 10), reported=True)
    v0_moments = merge_clips_into_moments(clips, rule=CLIP_START).moments

    with pytest.raises(ValueError, match="another clip position rule than 'end'"):
        label_windows(
            make_windows("v", 300),
            v0_moments,
            clips,
            broadcaster_id="b",
            split="train",
            mature=True,
        )
