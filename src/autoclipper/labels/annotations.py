"""Source-time window labels and training weights, separate from inference inputs."""

import math
from bisect import bisect_left, bisect_right
from statistics import median
from typing import Final

import polars as pl

from autoclipper.labels.splits import Split

NEG_BUFFER_S: Final = 60.0
LABEL_RULES_VERSION: Final = "v0"
WEIGHT_VERSION: Final = "one-plus-log1p-relviews-v1"
LABELS_SCHEMA: Final = pl.Schema(
    {
        "window_id": pl.String,
        "label_pos": pl.Boolean,
        "label_n_clips": pl.Int64,
        "label_moment_id": pl.String,
        "split": pl.String,
        "train_eligible": pl.Boolean,
        "moment_strength": pl.Float64,
        "sample_weight": pl.Float64,
    }
)


def label_windows(
    windows: pl.DataFrame,
    moments: pl.DataFrame,
    clips: pl.DataFrame,
    *,
    broadcaster_id: str,
    split: Split,
    mature: bool,
) -> pl.DataFrame:
    """Annotate one covered VOD using its pinned snapshot's clips and moments.

    Half-open intervals that only touch do not overlap. A negative is eligible for
    training at distance >= 60 s from the nearest moment (all windows if no moments).
    Buffer rows remain annotated for evaluation. Immature captures keep binary labels
    but are ineligible for training and have no view weights. Positive weight is
    1 + log1p(max_views / max(channel median views, 1)); negatives weigh 1.

    `clips` includes all of this channel's clips in the same pinned snapshot, including
    unplaced clips for the median. The caller must supply verified collection coverage.
    """
    _check(windows, ("window_id", "video_id", "start_s", "end_s"), "window_id")
    _check(
        moments, ("moment_id", "video_id", "start_s", "end_s", "n_clips", "max_views"), "moment_id"
    )
    _check(
        clips,
        ("clip_id", "broadcaster_id", "video_id", "vod_offset_s", "duration_s", "view_count"),
        "clip_id",
        nullable={"video_id", "vod_offset_s"},
    )
    ids = windows["video_id"].unique().to_list()
    if len(ids) > 1:
        raise ValueError("label_windows accepts one VOD at a time")
    if not ids:
        return LABELS_SCHEMA.to_frame()
    video_id = ids[0]
    if moments.filter(pl.col("video_id") != video_id).height:
        raise ValueError("moments must belong to the windows' VOD")
    ordered = moments.sort(["start_s", "moment_id"])
    groups = ordered.to_dicts()
    starts = ordered["start_s"].to_list()
    ends = ordered["end_s"].to_list()
    if any(ends[i] > starts[i + 1] for i in range(len(groups) - 1)):
        raise ValueError("label moments must not overlap")
    channel = clips.filter(pl.col("broadcaster_id") == broadcaster_id)
    if channel.filter(pl.col("view_count") < 0).height:
        raise ValueError("view_count must be non-negative")
    typical = max(float(median(channel["view_count"].to_list())), 1.0) if channel.height else 1.0
    placed = channel.filter((pl.col("video_id") == video_id) & pl.col("vod_offset_s").is_not_null())
    if placed.filter(
        ~pl.col("vod_offset_s").is_finite()
        | (pl.col("vod_offset_s") < 0)
        | ~pl.col("duration_s").is_finite()
        | (pl.col("duration_s") <= 0)
    ).height:
        raise ValueError("invalid clip interval")
    clip_starts = sorted(placed["vod_offset_s"].to_list())
    clip_ends = sorted((placed["vod_offset_s"] + placed["duration_s"]).to_list())
    rows = []
    for window in windows.sort(["start_s", "window_id"]).iter_rows(named=True):
        start, end = window["start_s"], window["end_s"]
        left, right = bisect_right(ends, start), bisect_left(starts, end)
        overlaps = groups[left:right]
        best = (
            min(overlaps, key=lambda m: (-m["n_clips"], m["start_s"], m["moment_id"]))
            if overlaps
            else None
        )
        distance = min(
            start - ends[left - 1] if left else math.inf,
            starts[right] - end if right < len(starts) else math.inf,
        )
        eligible = mature and (best is not None or distance >= NEG_BUFFER_S)
        strength = best["max_views"] / typical if best is not None and mature else None
        weight = (1.0 + math.log1p(strength) if strength is not None else 1.0) if eligible else None
        rows.append(
            {
                "window_id": window["window_id"],
                "label_pos": best is not None,
                "label_n_clips": bisect_left(clip_starts, end) - bisect_right(clip_ends, start),
                "label_moment_id": best["moment_id"] if best else None,
                "split": split,
                "train_eligible": eligible,
                "moment_strength": strength,
                "sample_weight": weight,
            }
        )
    return pl.DataFrame(rows, schema=LABELS_SCHEMA)


def _check(
    frame: pl.DataFrame, columns: tuple[str, ...], key: str, *, nullable: set[str] | None = None
) -> None:
    missing = set(columns) - set(frame.columns)
    if missing:
        raise ValueError(f"{key}: missing columns {sorted(missing)}")
    if frame[key].n_unique() != frame.height:
        raise ValueError(f"{key} must be unique")
    for column in set(columns) - (nullable or set()):
        if frame[column].null_count():
            raise ValueError(f"{column} must not be null")
    if (
        "start_s" in columns
        and frame.filter(
            ~pl.col("start_s").is_finite()
            | ~pl.col("end_s").is_finite()
            | (pl.col("start_s") < 0)
            | (pl.col("end_s") <= pl.col("start_s"))
        ).height
    ):
        raise ValueError(f"{key}: invalid interval")
