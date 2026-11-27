"""Merge a VOD's viewer clips into moments: distinct stretches viewers found worth clipping.

Follows docs/data/labeling.md § 2 and the ``moments`` table in docs/architecture/data-model.md:

1. Clips with a null ``vod_offset_s`` or ``video_id`` are skipped and counted.
2. Each clip becomes an interval through one `ClipPositionRule` (`clip_interval`). The default,
   `CLIP_END`, reads Helix's ``vod_offset_s`` as where the clip ends (exp-002, exp-003):
   ``[max(vod_offset_s - duration_s, 0), vod_offset_s)``, cut at the VOD's start. `CLIP_START`,
   the reading before #126, is ``[vod_offset_s, vod_offset_s + duration_s)``. A clip whose
   interval is empty (``vod_offset_s == 0`` under `CLIP_END`) is skipped and counted.
3. Within a VOD, clips sorted by start join the current moment when they overlap it or start
   within `GAP_S` seconds of its end (touching clips merge). Chains of overlapping clips stay one
   moment however long they get.
4. A moment spans its earliest member start to its latest member end. ``moment_id`` is
   ``{video_id}:m{index}`` with ``index`` 0-based in start order; merged moments never overlap.

The input is one pinned clips snapshot, so a ``clip_id`` appears once. A clip listed twice with
the same VOD position (a repeated observation) counts once, keeping its latest observation, with
a warning; listed with different positions is an error. Within a moment, ``clip_ids`` are in the
order the clips start (then end, then ID). The output depends only on the input rows, not their
order.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

import polars as pl

log = logging.getLogger(__name__)

# docs/data/labeling.md § 2.
GAP_S: Final = 5.0

_TIMESTAMP: Final = pl.Datetime("us", "UTC")


@dataclass(frozen=True, slots=True)
class ClipPositionRule:
    """How a clip's reported ``vod_offset_s`` and ``duration_s`` become its VOD interval.

    Attributes:
        name: Recorded in the ``moments`` table's ``clip_position`` column.
        start: Builds the interval's start from the offset and duration expressions.
        end: Builds the interval's end from the same expressions.
    """

    name: str
    start: Callable[[pl.Expr, pl.Expr], pl.Expr]
    end: Callable[[pl.Expr, pl.Expr], pl.Expr]


CLIP_END: Final = ClipPositionRule(
    "end",
    start=lambda offset, duration: pl.max_horizontal(offset - duration, pl.lit(0.0)),
    end=lambda offset, duration: offset,
)
"""``vod_offset_s`` is where the clip ends; a clip that would start before the VOD starts at 0."""

CLIP_START: Final = ClipPositionRule(
    "start",
    start=lambda offset, duration: offset,
    end=lambda offset, duration: offset + duration,
)
"""``vod_offset_s`` is where the clip starts: the reading before #126 (label rules v0)."""

CLIP_POSITION_RULE: Final = CLIP_END
"""The rule the pipeline uses unless a caller asks for another."""


def clip_interval(clips: pl.DataFrame, rule: ClipPositionRule = CLIP_POSITION_RULE) -> pl.DataFrame:
    """`clips` with ``start_s`` and ``end_s`` (Float64) from ``vod_offset_s`` and ``duration_s``.

    The one place clip positions become intervals. A null offset gives null bounds; callers
    validate the result.
    """
    offset = pl.col("vod_offset_s").cast(pl.Float64)
    duration = pl.col("duration_s").cast(pl.Float64)
    return clips.with_columns(
        start_s=rule.start(offset, duration).cast(pl.Float64),
        end_s=rule.end(offset, duration).cast(pl.Float64),
    )


def valid_clip_interval() -> pl.Expr:
    """True for a `clip_interval` row with a usable duration and a finite interval in the VOD.

    The interval may be empty (``end_s == start_s``); callers skip those. A negative offset
    fails: it starts before 0 or ends before it starts.
    """
    return (
        pl.col("duration_s").is_finite()
        & (pl.col("duration_s") > 0)
        & pl.col("start_s").is_finite()
        & pl.col("end_s").is_finite()
        & (pl.col("start_s") >= 0)
        & (pl.col("end_s") >= pl.col("start_s"))
    ).fill_null(False)


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
        "clip_position": pl.String,
    }
)
"""The ``moments`` table (data-model.md). ``clip_position`` is the `ClipPositionRule`'s name."""


@dataclass(frozen=True, slots=True)
class MomentsResult:
    """Moments and the clips that couldn't be placed on a VOD's timeline.

    Attributes:
        moments: A frame with `MOMENTS_SCHEMA`, sorted by ``video_id`` and ``start_s``.
        n_without_vod_offset: Clips skipped for a null ``vod_offset_s`` (with a ``video_id``).
        n_without_video: Clips skipped for a null ``video_id``.
        n_repeated: Repeated listings of a clip that were counted once.
        n_empty: Clips skipped because their interval under the rule is empty.
    """

    moments: pl.DataFrame
    n_without_vod_offset: int
    n_without_video: int
    n_repeated: int
    n_empty: int = 0


def merge_clips_into_moments(
    clips: pl.DataFrame,
    *,
    gap_s: float = GAP_S,
    rule: ClipPositionRule = CLIP_POSITION_RULE,
) -> MomentsResult:
    """Group the clips of one snapshot into moments, per VOD.

    Args:
        clips: Rows of the ``clips`` table (at least `CLIP_COLUMNS`).
        gap_s: Clips starting at most this many seconds after the current moment's end join it.
        rule: Turns each clip's reported position into its interval (`clip_interval`).

    Raises:
        ValueError: A column is missing, a timestamp column has no time zone, `gap_s` is
            negative, a placed clip's duration is missing, not finite or not positive, its
            interval under `rule` isn't finite, starts before 0 or ends before it starts (a
            negative offset), or one ``clip_id`` appears with different VOD positions.
    """
    missing = set(CLIP_COLUMNS) - set(clips.columns)
    if missing:
        raise ValueError(f"clips is missing columns: {', '.join(sorted(missing))}")
    if gap_s < 0:
        raise ValueError(f"gap_s must be >= 0, got {gap_s!r}")
    if clips["clip_id"].null_count():
        raise ValueError("clips has a null clip_id")

    for column in ("created_at", "fetched_at"):
        dtype = clips.schema[column]
        if not isinstance(dtype, pl.Datetime) or dtype.time_zone is None:
            raise ValueError(f"clips.{column} must be a timezone-aware datetime, got {dtype}")

    frame = clips.select(CLIP_COLUMNS)
    # One snapshot lists a clip once. A repeated listing with the same VOD position is the
    # same observation and counts once (its latest); different positions mean mixed data.
    conflicting = (
        frame.group_by("clip_id")
        .agg(pl.struct("video_id", "vod_offset_s", "duration_s").n_unique().alias("n"))
        .filter(pl.col("n") > 1)
    )
    if conflicting.height:
        raise ValueError(
            f"{conflicting.height} clip_id(s) appear with different VOD positions, e.g. "
            f"{conflicting['clip_id'].sort()[0]}; clips must come from one snapshot"
        )
    n_rows = frame.height
    frame = frame.sort(["clip_id", "fetched_at", "view_count"]).unique(
        "clip_id", keep="last", maintain_order=True
    )
    n_repeated = n_rows - frame.height
    if n_repeated:
        log.warning("counted repeated clip listings once n=%d", n_repeated)

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
    placed = clip_interval(placed, rule)
    # Checked on the interval the rule gives, after any cut at the VOD's start.
    invalid = placed.filter(~valid_clip_interval())
    if invalid.height:
        raise ValueError(
            f"{invalid.height} clip(s) have a vod_offset_s that is negative or not finite, or "
            f"a duration_s that is missing, not finite or not positive, e.g. "
            f"{invalid['clip_id'].sort()[0]}"
        )
    n_empty = placed.filter(pl.col("end_s") == pl.col("start_s")).height
    if n_empty:
        log.warning("skipped clips with an empty interval rule=%s n=%d", rule.name, n_empty)
        placed = placed.filter(pl.col("end_s") > pl.col("start_s"))

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
            clip_position=pl.lit(rule.name),
        )
        .sort(["video_id", "start_s"])
        .select(MOMENTS_SCHEMA.names())
        .cast(MOMENTS_SCHEMA)
    )
    return MomentsResult(moments, n_without_offset, n_without_video, n_repeated, n_empty)
