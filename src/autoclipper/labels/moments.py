"""Merge a VOD's viewer clips into moments: distinct stretches viewers found worth clipping.

Follows docs/data/labeling.md § 2 and the ``moments`` table in docs/architecture/data-model.md:

1. Clips with a null ``vod_offset_s`` or ``video_id`` are skipped and counted.
2. Each clip is the interval ``[vod_offset_s, vod_offset_s + duration_s)``.
3. Within a VOD, clips sorted by start join the current moment when they overlap it or start
   within `GAP_S` seconds of its end (touching clips merge). Chains of overlapping clips stay one
   moment however long they get.
4. A moment spans its earliest member start to its latest member end. ``moment_id`` is
   ``{video_id}:m{index}`` with ``index`` 0-based in start order; merged moments never overlap.

The input is one pinned clips snapshot, so a ``clip_id`` appears once. A clip listed twice with
the same interval (a repeated observation) counts once, keeping its latest observation; listed
twice with different intervals is an error. The output depends only on the input rows, not their
order.
"""

import logging
from dataclasses import dataclass
from typing import Final

import polars as pl

log = logging.getLogger(__name__)

# docs/data/labeling.md § 2.
GAP_S: Final = 5.0

_TIMESTAMP: Final = pl.Datetime("us", "UTC")

CLIP_COLUMNS: Final = (
    "clip_id",
    "video_id",
    "vod_offset_s",
    "duration_s",
    "view_count",
    "created_at",
    "fetched_at",
)
"""The ``clips`` columns `merge_clips_into_moments` reads."""

MOMENTS_SCHEMA: Final = pl.Schema(
    {
        "moment_id": pl.String,
        "video_id": pl.String,
        "start_s": pl.Float64,
        "end_s": pl.Float64,
        "n_clips": pl.Int64,
        "total_views": pl.Int64,
        "max_views": pl.Int64,
        "first_clip_created_at": _TIMESTAMP,
        "clip_ids": pl.List(pl.String),
        "views_observed_at": _TIMESTAMP,
    }
)
"""The ``moments`` table (data-model.md)."""


@dataclass(frozen=True, slots=True)
class MomentsResult:
    """Moments and the clips that couldn't be placed on a VOD's timeline.

    Attributes:
        moments: A frame with `MOMENTS_SCHEMA`, sorted by ``video_id`` and ``start_s``.
        n_without_vod_offset: Clips skipped for a null ``vod_offset_s`` (with a ``video_id``).
        n_without_video: Clips skipped for a null ``video_id``.
        n_repeated: Repeated listings of a clip that were counted once.
    """

    moments: pl.DataFrame
    n_without_vod_offset: int
    n_without_video: int
    n_repeated: int


def merge_clips_into_moments(clips: pl.DataFrame, *, gap_s: float = GAP_S) -> MomentsResult:
    """Group the clips of one snapshot into moments, per VOD.

    Args:
        clips: Rows of the ``clips`` table (at least `CLIP_COLUMNS`).
        gap_s: Clips starting at most this many seconds after the current moment's end join it.

    Raises:
        ValueError: A column is missing, `gap_s` is negative, a clip has a negative offset or a
            non-positive duration, or one ``clip_id`` appears with different intervals.
    """
    missing = set(CLIP_COLUMNS) - set(clips.columns)
    if missing:
        raise ValueError(f"clips is missing columns: {', '.join(sorted(missing))}")
    if gap_s < 0:
        raise ValueError(f"gap_s must be >= 0, got {gap_s!r}")
    if clips["clip_id"].null_count():
        raise ValueError("clips has a null clip_id")

    frame = clips.select(CLIP_COLUMNS)
    n_without_video = frame["video_id"].null_count()
    n_without_offset = frame.filter(
        pl.col("video_id").is_not_null() & pl.col("vod_offset_s").is_null()
    ).height
    if n_without_video or n_without_offset:
        log.info(
            "skipped clips without a VOD position without_video=%d without_offset=%d",
            n_without_video,
            n_without_offset,
        )
    placed = frame.filter(pl.col("video_id").is_not_null() & pl.col("vod_offset_s").is_not_null())
    if placed.filter((pl.col("vod_offset_s") < 0) | ~(pl.col("duration_s") > 0)).height:
        raise ValueError("clips have a negative vod_offset_s or a duration_s that isn't positive")

    placed = placed.with_columns(
        start_s=pl.col("vod_offset_s").cast(pl.Float64),
        end_s=(pl.col("vod_offset_s") + pl.col("duration_s")).cast(pl.Float64),
    )
    conflicting = (
        placed.group_by("clip_id")
        .agg(pl.struct("video_id", "start_s", "end_s").n_unique().alias("n"))
        .filter(pl.col("n") > 1)
    )
    if conflicting.height:
        raise ValueError(
            f"{conflicting.height} clip_id(s) appear with different intervals, e.g. "
            f"{conflicting['clip_id'].sort()[0]}; clips must come from one snapshot"
        )
    n_rows = placed.height
    placed = placed.sort(["clip_id", "fetched_at", "view_count"]).unique(
        "clip_id", keep="last", maintain_order=True
    )
    n_repeated = n_rows - placed.height
    if n_repeated:
        log.info("counted repeated clip listings once n=%d", n_repeated)

    moments = (
        placed.sort(["video_id", "start_s", "end_s", "clip_id"])
        .with_columns(
            # The furthest end reached by earlier clips of the same VOD: the current moment's end.
            reach_s=pl.col("end_s").cum_max().shift(1).over("video_id"),
        )
        .with_columns(
            starts_moment=pl.col("reach_s").is_null()
            | (pl.col("start_s") > pl.col("reach_s") + gap_s)
        )
        .with_columns(index=(pl.col("starts_moment").cum_sum().over("video_id") - 1))
        .group_by(["video_id", "index"], maintain_order=True)
        .agg(
            start_s=pl.col("start_s").min(),
            end_s=pl.col("end_s").max(),
            n_clips=pl.len().cast(pl.Int64),
            total_views=pl.col("view_count").sum().cast(pl.Int64),
            max_views=pl.col("view_count").max().cast(pl.Int64),
            first_clip_created_at=pl.col("created_at").min(),
            clip_ids=pl.col("clip_id"),
            views_observed_at=pl.col("fetched_at").max(),
        )
        .with_columns(
            moment_id=pl.format("{}:m{}", pl.col("video_id"), pl.col("index")),
        )
        .sort(["video_id", "start_s"])
        .select(MOMENTS_SCHEMA.names())
        .cast(MOMENTS_SCHEMA)
    )
    return MomentsResult(moments, n_without_offset, n_without_video, n_repeated)
