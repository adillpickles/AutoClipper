"""Where viewer clips really are in their VOD, found by matching their audio (#36, exp-002).

exp-001 found viewer clips' reported positions (Helix ``vod_offset``) apparently later than
their content. This module checks that directly: a clip's own audio is matched against the VOD's
16 kHz WAV near the reported position, and the best match is the clip's true start.

- `envelope`: a log-energy envelope (10 ms frames by default). Clip and VOD audio come from
  differently encoded copies of the same stream, so their loudness *shape* matches even where
  the waveforms don't line up sample for sample.
- `normalized_xcorr` / `match_clip`: Pearson correlation of the clip's envelope with every
  position of the VOD's envelope in the search span; the best position, its correlation and the
  best correlation more than `EXCLUSION_S` away (a distinct match has a clear margin).
- `select_clips`: the most-viewed clips per channel, live and post-stream, spread over VODs,
  skipping clips that overlap a muted VOD segment or whose VOD audio is mostly silent there.
- `shift_vod`: a VOD's clips moved by a shift (optionally minus each clip's own length),
  re-placed and re-merged into moments, to rerun exp-001's measures on corrected positions.
- `summarize_matches`: over strong matches only, the shift (true minus reported start) and the
  *end residual* (true end minus reported position), which is the one to read if the reported
  position marks the clip's end; plus a line fit of shift against clip length.

All times are seconds of VOD time.
"""

import math
import wave
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Final

import numpy as np
import numpy.typing as npt
import polars as pl

from autoclipper.labels import alignment as al
from autoclipper.labels.alignment_study import VodData, place_clips, top_clips
from autoclipper.labels.moments import merge_clips_into_moments

SAMPLE_RATE_HZ: Final = 16_000
HOP_S: Final = 0.01
"""Envelope frame length and hop."""
FLOOR_DB: Final = -60.0
"""Envelope floor; quieter frames count as this (the features' silence level)."""
EXCLUSION_S: Final = 1.0
"""The runner-up match is the best one more than this far from the best."""
CLIP_CLASSES: Final = ("live", "post_stream")
SHORT_CLIP_S: Final = 10.0
"""Clips this long or shorter count as short in the end-error breakdown."""

type FloatArray = npt.NDArray[np.float64]


class ClipAudioError(ValueError):
    """A WAV file isn't 16-bit mono PCM at the expected rate, or is empty."""


def read_wav(
    path: Path, start_s: float = 0.0, end_s: float | None = None
) -> tuple[FloatArray, float]:
    """Samples of a 16-bit mono WAV from `start_s` to `end_s` (clamped to the file), full scale 1.

    Returns:
        ``(samples, actual_start_s)``.

    Raises:
        ClipAudioError: The file isn't 16-bit mono PCM at `SAMPLE_RATE_HZ`.
    """
    with wave.open(str(path), "rb") as reader:
        if (reader.getnchannels(), reader.getsampwidth(), reader.getframerate()) != (
            1,
            2,
            SAMPLE_RATE_HZ,
        ):
            raise ClipAudioError(f"{path.name} isn't 16-bit mono PCM at {SAMPLE_RATE_HZ} Hz")
        total = reader.getnframes()
        first = min(total, max(0, math.floor(start_s * SAMPLE_RATE_HZ)))
        last = total if end_s is None else min(total, max(first, math.ceil(end_s * SAMPLE_RATE_HZ)))
        reader.setpos(first)
        raw = reader.readframes(last - first)
    samples = np.frombuffer(raw, dtype="<i2").astype(np.float64) / 32768.0
    return samples, first / SAMPLE_RATE_HZ


def envelope(samples: FloatArray, *, hop_s: float = HOP_S) -> FloatArray:
    """Frame energy in dBFS, one value per `hop_s` of 16 kHz audio, floored at `FLOOR_DB`.

    A trailing partial frame is dropped.
    """
    hop = round(hop_s * SAMPLE_RATE_HZ)
    n = samples.size // hop
    frames = samples[: n * hop].reshape(n, hop)
    power = (frames * frames).mean(axis=1)
    return np.maximum(10 * np.log10(np.maximum(power, 1e-12)), FLOOR_DB)


def normalized_xcorr(segment: FloatArray, template: FloatArray) -> FloatArray:
    """Pearson correlation of `template` with each same-length stretch of `segment`.

    Entry ``k`` compares ``segment[k : k + len(template)]`` with `template`. A flat stretch, or a
    flat template, gives 0. Empty when the template is longer than the segment.
    """
    m = template.size
    count = segment.size - m + 1
    if m < 2 or count < 1:
        return np.zeros(max(count, 0), dtype=np.float64)
    t_std = float(template.std())
    if t_std == 0:
        return np.zeros(count, dtype=np.float64)
    t = (template - template.mean()) / t_std
    sums = np.concatenate(([0.0], np.cumsum(segment)))
    squares = np.concatenate(([0.0], np.cumsum(segment * segment)))
    mean = (sums[m:] - sums[:count]) / m
    var = (squares[m:] - squares[:count]) / m - mean * mean
    std = np.sqrt(np.maximum(var, 0.0))
    # Correlate through the FFT: the segment can be minutes of 10 ms frames.
    size = 1 << (segment.size + m - 1).bit_length()
    product = np.fft.irfft(np.fft.rfft(segment, size) * np.conj(np.fft.rfft(t, size)), size)
    dots = product[:count]
    out = np.zeros(count, dtype=np.float64)
    np.divide(dots, m * std, out=out, where=std > 1e-9)
    return np.clip(out, -1.0, 1.0)


@dataclass(frozen=True, slots=True)
class Match:
    """Where a clip's audio best matches its VOD.

    Attributes:
        true_start_s: The matched start of the clip in VOD time.
        shift_s: ``true_start_s`` minus the reported start.
        corr: Envelope correlation at the best position (1 = identical shape).
        runner_up: The best correlation more than `EXCLUSION_S` from the best position.
        margin: ``corr - runner_up``.
        at_search_edge: The best position is at an end of the searched span (it may lie beyond).
        clip_length_s: The downloaded clip's own audio length (the metadata ``duration_s`` is
            rounded, and cut at the VOD's end when placed).
    """

    true_start_s: float
    shift_s: float
    corr: float
    runner_up: float
    margin: float
    at_search_edge: bool
    clip_length_s: float


def match_clip(
    clip_samples: FloatArray,
    vod_wav: Path,
    *,
    reported_s: float,
    search_s: float,
    hop_s: float = HOP_S,
) -> Match | None:
    """Find a clip's audio in its VOD's WAV within `search_s` of the reported start.

    Returns:
        The best match, or None when the clip is shorter than two frames or the searched span
        is shorter than the clip.
    """
    clip_env = envelope(clip_samples, hop_s=hop_s)
    clip_s = clip_env.size * hop_s
    segment, start_s = read_wav(vod_wav, reported_s - search_s, reported_s + clip_s + search_s)
    seg_env = envelope(segment, hop_s=hop_s)
    corr = normalized_xcorr(seg_env, clip_env)
    if corr.size == 0 or clip_env.size < 2:
        return None
    best = int(np.argmax(corr))
    far = np.abs(np.arange(corr.size) - best) * hop_s > EXCLUSION_S
    runner_up = float(corr[far].max()) if far.any() else -1.0
    true_start = start_s + best * hop_s
    return Match(
        true_start_s=true_start,
        shift_s=true_start - reported_s,
        corr=float(corr[best]),
        runner_up=runner_up,
        margin=float(corr[best]) - runner_up,
        at_search_edge=best in (0, corr.size - 1),
        clip_length_s=clip_samples.size / SAMPLE_RATE_HZ,
    )


def silent_fraction(vod_wav: Path, start_s: float, end_s: float) -> float:
    """Share of 10 ms frames of the VOD's audio in ``[start_s, end_s)`` below `FLOOR_DB`.

    1 when the range holds no audio.
    """
    samples, _ = read_wav(vod_wav, start_s, end_s)
    env = envelope(samples)
    return float((env <= FLOOR_DB).mean()) if env.size else 1.0


def _overlaps(start: float, end: float, segments: Sequence[tuple[float, float]]) -> bool:
    return any(start < offset + duration and offset < end for offset, duration in segments)


@dataclass(frozen=True, slots=True)
class Selection:
    """The clips to check and what was skipped.

    Attributes:
        clips: ``video_id``, ``login``, ``clip_id``, ``clip_class``, ``reported_s``,
            ``duration_s``, ``view_count``, ``created_s`` (creation on the VOD timeline).
        n_skipped_muted: Candidates overlapping a muted VOD segment.
        n_skipped_silent: Candidates whose VOD audio is mostly silent at the reported position.
    """

    clips: pl.DataFrame
    n_skipped_muted: int
    n_skipped_silent: int


def _created_s(vod: VodData) -> pl.Expr:
    vod_start = pl.col("ended_at") - pl.duration(seconds=vod.duration_s)
    return (pl.col("created_at") - vod_start).dt.total_milliseconds().cast(pl.Float64) / 1000


def select_clips(
    vods: Sequence[VodData],
    *,
    wav_paths: Mapping[str, Path],
    muted: Mapping[str, Sequence[tuple[float, float]]],
    per_channel: int,
    live_per_channel: int,
    max_per_vod: int,
    max_silent_fraction: float = 0.5,
) -> Selection:
    """The most-viewed clips of each channel, `live_per_channel` live and the rest post-stream.

    Within a channel, clips are taken by views (ties to the smaller clip ID), at most
    `max_per_vod` per VOD, live ones until `live_per_channel` and post-stream ones until the
    channel has `per_channel`; when one kind runs out, the other fills in. A clip overlapping one
    of its VOD's `muted` segments, or whose VOD audio is more than `max_silent_fraction` silent
    over its reported span, is skipped and counted.
    """
    candidates = clip_candidates(vods)
    picked: list[dict[str, Any]] = []
    n_muted = n_silent = 0
    for login in dict.fromkeys(v.login for v in vods):
        pool = candidates.filter(pl.col("login") == login).sort(
            ["view_count", "clip_id"], descending=[True, False]
        )
        quota = {"live": live_per_channel, "post_stream": per_channel - live_per_channel}
        taken: list[dict[str, Any]] = []
        per_vod: dict[str, int] = {}
        for fill in (False, True):  # first by quota, then let either kind fill the rest
            for row in pool.iter_rows(named=True):
                if len(taken) >= per_channel:
                    break
                if row in taken or per_vod.get(row["video_id"], 0) >= max_per_vod:
                    continue
                kind_count = sum(t["clip_class"] == row["clip_class"] for t in taken)
                if not fill and kind_count >= quota[row["clip_class"]]:
                    continue
                start, end = row["reported_s"], row["reported_s"] + row["duration_s"]
                if _overlaps(start, end, muted.get(row["video_id"], ())):
                    n_muted += 1
                    continue
                if silent_fraction(wav_paths[row["video_id"]], start, end) > max_silent_fraction:
                    n_silent += 1
                    continue
                taken.append(row)
                per_vod[row["video_id"]] = per_vod.get(row["video_id"], 0) + 1
        picked.extend(taken)
    return Selection(pl.DataFrame(picked, schema=CANDIDATE_SCHEMA), n_muted, n_silent)


CANDIDATE_SCHEMA: Final = pl.Schema(
    {
        "video_id": pl.String,
        "login": pl.String,
        "clip_id": pl.String,
        "clip_class": pl.String,
        "reported_s": pl.Float64,
        "duration_s": pl.Float64,
        "view_count": pl.Int64,
        "created_s": pl.Float64,
    }
)
"""Columns of `clip_candidates` and of a `Selection`'s clips."""


def clip_candidates(vods: Sequence[VodData]) -> pl.DataFrame:
    """Every placed clip of `vods` with its class and creation time, in `CANDIDATE_SCHEMA`."""
    rows: list[dict[str, Any]] = []
    for vod in vods:
        if vod.clips.height == 0:
            continue
        ranked = top_clips(vod, vod.clips.height).with_columns(
            created_s=_created_s(vod),
            clip_class=pl.when(pl.col("created_at") > pl.col("ended_at"))
            .then(pl.lit("post_stream"))
            .otherwise(pl.lit("live")),
        )
        for row in ranked.iter_rows(named=True):
            rows.append(
                {
                    "video_id": vod.video_id,
                    "login": vod.login,
                    "clip_id": row["clip_id"],
                    "clip_class": row["clip_class"],
                    "reported_s": float(row["vod_offset_s"]),
                    "duration_s": float(row["duration_s"]),
                    "view_count": int(row["view_count"]),
                    "created_s": float(row["created_s"]),
                }
            )
    return pl.DataFrame(rows, schema=CANDIDATE_SCHEMA)


CELLS: Final = (("top", "live"), ("top", "post_stream"), ("low", "live"), ("low", "post_stream"))
"""The selection cells of `select_stratified_clips`, in the order their counts are given."""


def select_stratified_clips(
    vods: Sequence[VodData],
    *,
    wav_paths: Mapping[str, Path],
    muted: Mapping[str, Sequence[tuple[float, float]]],
    exclude: Collection[str],
    cell_counts: Sequence[Sequence[int]],
    max_per_vod: int,
    max_silent_fraction: float,
    seed: int,
) -> Selection:
    """Per channel, top and low-view clips, live and post-stream, in the given cell counts.

    Channels are taken in the order of `vods`; channel ``i`` uses
    ``cell_counts[i % len(cell_counts)]``, counts for the `CELLS` (top-live, top-post,
    low-live, low-post). *Top* clips are the most-viewed (ties to the smaller clip ID);
    *low-view* clips have ``view_count`` at or below the channel's median and are drawn in an
    order shuffled with `seed` (per channel). Clips in `exclude` are never used, at most
    `max_per_vod` come from one VOD, and a clip is skipped (and counted) when it overlaps a
    `muted` segment or its VOD audio is more than `max_silent_fraction` silent over either
    reading of its position: ``[vod_offset, vod_offset + duration]`` or
    ``[vod_offset - duration, vod_offset]``.

    Returns:
        The clips with an added ``stratum`` column (``top`` or ``low``).
    """
    candidates = clip_candidates(vods).filter(~pl.col("clip_id").is_in(sorted(exclude)))
    picked: list[dict[str, Any]] = []
    n_muted = n_silent = 0
    for index, login in enumerate(dict.fromkeys(v.login for v in vods)):
        counts = dict(zip(CELLS, cell_counts[index % len(cell_counts)], strict=True))
        pool = candidates.filter(pl.col("login") == login)
        views = pool["view_count"].to_numpy()
        median_views = float(np.median(views)) if views.size else 0.0
        top = pool.sort(["view_count", "clip_id"], descending=[True, False])
        low = pool.filter(pl.col("view_count") <= median_views).sort("clip_id")
        order = np.random.default_rng([seed, index]).permutation(low.height)
        low = low[order.tolist()] if low.height else low
        used: set[str] = set()
        per_vod: dict[str, int] = {}
        for (stratum, clip_class), wanted in counts.items():
            source = top if stratum == "top" else low
            got = 0
            for row in source.filter(pl.col("clip_class") == clip_class).iter_rows(named=True):
                if got >= wanted:
                    break
                if row["clip_id"] in used or per_vod.get(row["video_id"], 0) >= max_per_vod:
                    continue
                offset, length = row["reported_s"], row["duration_s"]
                spans = ((offset, offset + length), (max(offset - length, 0.0), offset))
                if any(_overlaps(a, b, muted.get(row["video_id"], ())) for a, b in spans):
                    n_muted += 1
                    continue
                wav = wav_paths[row["video_id"]]
                if any(silent_fraction(wav, a, b) > max_silent_fraction for a, b in spans):
                    n_silent += 1
                    continue
                picked.append({**row, "stratum": stratum})
                used.add(row["clip_id"])
                per_vod[row["video_id"]] = per_vod.get(row["video_id"], 0) + 1
                got += 1
    schema = pl.Schema({**CANDIDATE_SCHEMA, "stratum": pl.String})
    return Selection(pl.DataFrame(picked, schema=schema), n_muted, n_silent)


def shift_vod(vod: VodData, shift_s: Mapping[str, float], *, per_duration: float = 0.0) -> VodData:
    """`vod` with each clip moved, re-placed and re-merged into moments.

    A clip moves by its class's shift (``live``/``post_stream``) plus `per_duration` times its
    own length: ``per_duration=-1`` reads the reported position as the clip's end. The moved
    clips are placed again: clips starting at or after the VOD's end are dropped, clips running
    past it or before its start are cut at the edge, and moments are rebuilt.
    """
    post = pl.col("created_at") > pl.col("ended_at")
    moved = vod.clips.with_columns(
        vod_offset_s=pl.col("vod_offset_s")
        + per_duration * pl.col("duration_s")
        + pl.when(post).then(shift_s.get("post_stream", 0.0)).otherwise(shift_s.get("live", 0.0))
    )
    before = pl.col("vod_offset_s") < 0
    moved = moved.with_columns(
        duration_s=pl.when(before)
        .then(pl.col("duration_s") + pl.col("vod_offset_s"))
        .otherwise(pl.col("duration_s")),
        vod_offset_s=pl.when(before).then(0.0).otherwise(pl.col("vod_offset_s")),
    ).filter(pl.col("duration_s") > 0)
    placed, _, _, _ = place_clips(moved, vod.duration_s)
    return replace(vod, clips=placed, moments=merge_clips_into_moments(placed).moments)


def _shift_stats(
    frame: pl.DataFrame,
    *,
    seed: int,
    n_resamples: int,
    consistent_iqr_s: float,
    column: str = "shift_s",
) -> dict[str, Any] | None:
    if frame.height == 0:
        return None
    shifts = frame[column].to_numpy()
    by_vod = [g[column].to_numpy() for _, g in frame.group_by("video_id", maintain_order=True)]
    ci = al.bootstrap_statistic(
        len(by_vod),
        lambda idx: float(np.median(np.concatenate([by_vod[i] for i in idx]))),
        seed=seed,
        n_resamples=n_resamples,
    )
    median = float(np.median(shifts))
    q25, q75 = (float(v) for v in np.percentile(shifts, [25, 75]))
    return {
        "n_clips": int(shifts.size),
        "n_vods": len(by_vod),
        "median_s": median,
        "low_s": ci[0] if ci else None,
        "high_s": ci[1] if ci else None,
        "min_s": float(shifts.min()),
        "max_s": float(shifts.max()),
        "p10_s": float(np.percentile(shifts, 10)),
        "p25_s": q25,
        "p75_s": q75,
        "p90_s": float(np.percentile(shifts, 90)),
        "iqr_s": q75 - q25,
        "share_within_2s_of_median": float((np.abs(shifts - median) <= 2).mean()),
        "consistent": (q75 - q25) <= consistent_iqr_s,
    }


def strong_matches(
    matches: pl.DataFrame, *, min_corr: float, min_margin: float
) -> tuple[pl.DataFrame, pl.DataFrame]:
    """`matches` with a ``strong`` flag, and the strong rows with their end residuals.

    A match is strong when its correlation is at least `min_corr`, its margin over the runner-up
    at least `min_margin` and it isn't at the search edge. The end residual is the true end minus
    the reported position: ``end_residual_s`` takes the true end as the true start plus the
    downloaded clip's own length (``clip_length_s``, falling back to ``duration_s``);
    ``end_residual_metadata_s`` uses the metadata ``duration_s`` (rounded, and cut at the VOD's
    end), which is what a pipeline correction would apply.
    """
    strong_expr = (
        pl.col("corr").is_not_null()
        & (pl.col("corr") >= min_corr)
        & (pl.col("margin") >= min_margin)
        & ~pl.col("at_search_edge").fill_null(True)
    )
    flagged = matches.with_columns(strong=strong_expr.fill_null(False))
    length = (
        pl.col("clip_length_s").fill_null(pl.col("duration_s"))
        if "clip_length_s" in matches.columns
        else pl.col("duration_s")
    )
    strong = flagged.filter(pl.col("strong")).with_columns(
        end_residual_s=pl.col("shift_s") + length,
        end_residual_metadata_s=pl.col("shift_s") + pl.col("duration_s"),
    )
    return flagged, strong


def _fit(strong: pl.DataFrame) -> dict[str, float | None] | None:
    """Least-squares line shift = intercept + slope * clip length, over strong matches."""
    if strong.height < 3 or strong["duration_s"].n_unique() < 2:
        return None
    x = strong["duration_s"].to_numpy()
    y = strong["shift_s"].to_numpy()
    slope, intercept = (float(v) for v in np.polyfit(x, y, 1))
    residual = y - (intercept + slope * x)
    # A constant shift (zero spread) has no correlation with length to report.
    r = float(np.corrcoef(x, y)[0, 1]) if float(y.std()) > 1e-9 else None
    return {
        "slope": slope,
        "intercept_s": intercept,
        "r": r,
        "residual_iqr_s": float(np.subtract(*np.percentile(residual, [75, 25]))),
    }


def summarize_matches(
    matches: pl.DataFrame,
    *,
    min_corr: float,
    min_margin: float,
    consistent_iqr_s: float,
    seed: int,
    n_resamples: int,
) -> dict[str, Any]:
    """Shift statistics over strong matches: overall, per channel, per clip class.

    A match is strong when its correlation is at least `min_corr`, its margin over the runner-up
    at least `min_margin` and it isn't at the search edge. Weak matches are counted, not used.

    Args:
        matches: One row per checked clip: ``video_id``, ``login``, ``clip_class``,
            ``duration_s``, ``created_s``, ``shift_s``, ``true_start_s``, ``corr``, ``margin``,
            ``at_search_edge`` (None in the match columns when no match was possible).
        min_corr: Lowest correlation of a strong match.
        min_margin: Lowest margin of a strong match.
        consistent_iqr_s: A group's shift is consistent when its interquartile range is at most
            this.
        seed: Resampling seed.
        n_resamples: Resamples for the median's interval (VODs resampled).
    """
    flagged, strong = strong_matches(matches, min_corr=min_corr, min_margin=min_margin)

    def stats(frame: pl.DataFrame, column: str = "shift_s") -> dict[str, Any] | None:
        return _shift_stats(
            frame,
            seed=seed,
            n_resamples=n_resamples,
            consistent_iqr_s=consistent_iqr_s,
            column=column,
        )

    def by_class(column: str) -> dict[str, Any]:
        return {
            "all": stats(strong, column),
            **{c: stats(strong.filter(pl.col("clip_class") == c), column) for c in CLIP_CLASSES},
        }

    live = strong.filter(pl.col("clip_class") == "live")
    # True end = true start + the downloaded clip's own length (metadata length as fallback).
    lengths = (
        live["clip_length_s"].fill_null(live["duration_s"])
        if "clip_length_s" in live.columns
        else live["duration_s"]
    )
    end_vs_created = (live["true_start_s"] + lengths - live["created_s"]).to_numpy()
    return {
        "n_checked": matches.height,
        "n_strong": strong.height,
        "n_weak": matches.height - strong.height,
        "weak_by_reason": {
            "no_match": int(flagged["corr"].is_null().sum()),
            "low_corr": int(flagged.filter(pl.col("corr") < min_corr).height),
            "low_margin": int(
                flagged.filter(
                    (pl.col("corr") >= min_corr) & (pl.col("margin") < min_margin)
                ).height
            ),
            "at_search_edge": int(flagged["at_search_edge"].fill_null(False).sum()),
        },
        "corr_of_strong_p10": float(np.percentile(strong["corr"].to_numpy(), 10))
        if strong.height
        else None,
        "shift": by_class("shift_s"),
        "shift_by_channel": {
            login: stats(strong.filter(pl.col("login") == login))
            for login in dict.fromkeys(matches["login"].to_list())
        },
        # The reported position read as the clip's END: true end minus reported offset.
        "end_residual": by_class("end_residual_s"),
        # The same with metadata lengths: the rule a pipeline correction would apply.
        "end_residual_metadata": by_class("end_residual_metadata_s"),
        "end_residual_by_channel": {
            login: stats(strong.filter(pl.col("login") == login), "end_residual_s")
            for login in dict.fromkeys(matches["login"].to_list())
        },
        "shift_vs_duration": _fit(strong),
        # Live clips: where the content really ends against the clip's creation time.
        "live_true_end_minus_created_s": (
            {f"p{q}": float(np.percentile(end_vs_created, q)) for q in (10, 25, 50, 75, 90)}
            if end_vs_created.size
            else None
        ),
    }


def _end_error_stats(
    frame: pl.DataFrame,
    *,
    tolerance_s: float,
    seed: int,
    n_resamples: int,
    column: str = "end_residual_metadata_s",
) -> dict[str, Any] | None:
    """Signed and absolute end error over `frame`'s strong matches, intervals by VOD resampling."""
    if frame.height == 0:
        return None
    signed = frame[column].to_numpy()
    error = np.abs(signed)
    groups = [
        np.abs(g[column].to_numpy()) for _, g in frame.group_by("video_id", maintain_order=True)
    ]

    def interval(statistic: Callable[[FloatArray], float]) -> tuple[float, float] | None:
        return al.bootstrap_statistic(
            len(groups),
            lambda idx: statistic(np.concatenate([groups[i] for i in idx])),
            seed=seed,
            n_resamples=n_resamples,
        )

    median_ci = interval(lambda e: float(np.median(e)))
    share_ci = interval(lambda e: float((e <= tolerance_s).mean()))
    return {
        "n_clips": int(error.size),
        "n_vods": len(groups),
        "median_signed_s": float(np.median(signed)),
        "median_abs_error_s": float(np.median(error)),
        "median_abs_error_low_s": median_ci[0] if median_ci else None,
        "median_abs_error_high_s": median_ci[1] if median_ci else None,
        "share_within_tolerance": float((error <= tolerance_s).mean()),
        "share_within_tolerance_low": share_ci[0] if share_ci else None,
        "share_within_tolerance_high": share_ci[1] if share_ci else None,
        "p90_abs_error_s": float(np.percentile(error, 90)),
        "min_signed_s": float(signed.min()),
        "max_signed_s": float(signed.max()),
        "n_beyond_tolerance": int((error > tolerance_s).sum()),
        # Of those, clips of at most SHORT_CLIP_S by metadata length.
        "n_beyond_tolerance_short": int(
            ((error > tolerance_s) & (frame["duration_s"].to_numpy() <= SHORT_CLIP_S)).sum()
        ),
    }


def evaluate_end_rule(
    matches: pl.DataFrame,
    *,
    tolerance_s: float,
    max_median_abs_error_s: float,
    min_share_within_tolerance: float,
    min_corr: float,
    min_margin: float,
    seed: int,
    n_resamples: int,
    length: str = "metadata",
) -> dict[str, Any]:
    """Test "true clip end = reported position" against pass criteria fixed in advance.

    The error is ``|true end - vod_offset|`` over strong matches (`strong_matches`), the true
    end being the true start plus the clip's metadata ``duration_s`` (`length` ``"metadata"``,
    exp-003's registered definition) or its downloaded audio length (``"audio"``). PASS when
    its median is at most `max_median_abs_error_s` and at least `min_share_within_tolerance`
    of clips are within `tolerance_s`; otherwise FAIL. Breakdowns by ``stratum`` (when
    present), clip class and channel are descriptive and carry no criterion.
    """
    if length not in ("metadata", "audio"):
        raise ValueError(f"length must be 'metadata' or 'audio', got {length!r}")
    column = "end_residual_metadata_s" if length == "metadata" else "end_residual_s"
    flagged, strong = strong_matches(matches, min_corr=min_corr, min_margin=min_margin)

    def stats(frame: pl.DataFrame) -> dict[str, Any] | None:
        return _end_error_stats(
            frame, tolerance_s=tolerance_s, seed=seed, n_resamples=n_resamples, column=column
        )

    overall = stats(strong)
    passed = (
        overall is not None
        and overall["median_abs_error_s"] <= max_median_abs_error_s
        and overall["share_within_tolerance"] >= min_share_within_tolerance
    )
    breakdown: dict[str, Any] = {
        "clip_class": {c: stats(strong.filter(pl.col("clip_class") == c)) for c in CLIP_CLASSES},
        "channel": {
            login: stats(strong.filter(pl.col("login") == login))
            for login in dict.fromkeys(matches["login"].to_list())
        },
    }
    if "stratum" in strong.columns:
        breakdown["stratum"] = {
            s: stats(strong.filter(pl.col("stratum") == s)) for s in ("top", "low")
        }
        breakdown["cell"] = {
            f"{s}_{c}": stats(strong.filter((pl.col("stratum") == s) & (pl.col("clip_class") == c)))
            for s, c in CELLS
        }
    return {
        "result": "PASS" if passed else "FAIL",
        "length": length,
        "criteria": {
            "tolerance_s": tolerance_s,
            "max_median_abs_error_s": max_median_abs_error_s,
            "min_share_within_tolerance": min_share_within_tolerance,
        },
        "n_checked": matches.height,
        "n_strong": strong.height,
        "n_weak": int((~flagged["strong"]).sum()),
        "overall": overall,
        "breakdown": breakdown,
    }
