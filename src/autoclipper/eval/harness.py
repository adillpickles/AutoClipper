"""Evaluate a scorer's candidates against viewer-clip moments and write a report.

`evaluate` computes, per VOD, hit rate@10 and top-clip recall@10 (docs/ml/evaluation.md § 0 step
1, § 3), averages them over VODs with 95% bootstrap intervals that resample VODs, and records
which VODs were excluded and why. `write_report` saves ``metrics.json`` and ``summary.md`` to a
run directory, normally ``artifacts/reports/<run_id>/``.

Inputs are Polars frames:

- **candidates:** ``video_id``, ``candidate_id``, ``start_s``, ``end_s``, ``score``: every
  candidate the scorer produced for each VOD, made without access to clips. Only the top 10 per
  VOD by score count.
- **moments:** the ``moments`` table (`autoclipper.labels.moments`) from the same clips snapshot.
- **clips:** the ``clips`` table rows of that snapshot (``clip_id``, ``video_id``,
  ``view_count``), for the 5 most-viewed clips per VOD. A clip listed twice counts once, with
  its higher view count.

The VODs evaluated are those in `video_ids` (the test VODs of the split), by default every VOD
with candidates; moments of other VODs are ignored, so a dataset-wide moments table is fine. A
VOD without moments is excluded from both metrics; a VOD whose clip snapshot is flagged
(``flagged_video_ids``, labeling.md § 7) is excluded from top-clip recall only.
"""

import json
import subprocess
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import polars as pl

from autoclipper.eval.bootstrap import Interval, bootstrap_mean
from autoclipper.eval.metrics import (
    TOL_S,
    TOP_CLIPS,
    TOP_K,
    Candidate,
    Moment,
    hit_rate_at_k,
    top_clip_moments,
    top_clip_recall_at_k,
)

REPORT_SCHEMA_VERSION: Final = 1
CANDIDATE_COLUMNS: Final = ("video_id", "candidate_id", "start_s", "end_s", "score")
MOMENT_COLUMNS: Final = ("video_id", "moment_id", "start_s", "end_s", "clip_ids")
CLIP_COLUMNS: Final = ("clip_id", "video_id", "view_count")
METRICS: Final = ("hit_rate_at_10", "top_clip_recall_at_10")


@dataclass(frozen=True, slots=True)
class EvalResult:
    """Per-VOD metrics and their averages.

    Attributes:
        per_vod: One row per evaluated VOD: ``video_id``, ``n_candidates``, ``n_moments``,
            the `METRICS` (null where the VOD is excluded) and ``excluded_reason``.
        summary: For each metric, its `Interval` over the VODs that have it (None if none do).
        n_vods: VODs in the population.
        excluded: ``{video_id: reason}`` for VODs left out of at least one metric.
    """

    per_vod: pl.DataFrame
    summary: dict[str, Interval | None]
    n_vods: int
    excluded: dict[str, str]


def evaluate(
    candidates: pl.DataFrame,
    moments: pl.DataFrame,
    clips: pl.DataFrame,
    *,
    seed: int,
    video_ids: Iterable[str] | None = None,
    flagged_video_ids: Iterable[str] = (),
    k: int = TOP_K,
) -> EvalResult:
    """Score each VOD's top `k` candidates against its moments.

    Raises:
        ValueError: A column is missing or null, a ``candidate_id`` or ``moment_id`` repeats,
            a score or bound isn't finite, or an interval ends before it starts.
    """
    _check(candidates, CANDIDATE_COLUMNS, "candidates", unique="candidate_id")
    _check(moments, MOMENT_COLUMNS, "moments", unique="moment_id")
    _check(clips, ("clip_id", "view_count"), "clips")
    if "video_id" not in clips.columns:
        raise ValueError("clips is missing columns: video_id")
    _check_types(candidates, moments, clips)
    for name, frame in (("candidates", candidates), ("moments", moments)):
        bounds = frame.select("start_s", "end_s")
        if not (bounds.select(pl.all().is_finite().all()).row(0) == (True, True)):
            raise ValueError(f"{name} has a start_s or end_s that isn't finite")
        if frame.filter(pl.col("end_s") <= pl.col("start_s")).height:
            raise ValueError(f"{name} has an interval that doesn't end after it starts")
    if not candidates["score"].is_finite().all():
        raise ValueError("candidates has a score that isn't finite")

    population = sorted(set(candidates["video_id"]) if video_ids is None else set(video_ids))
    if not population:
        raise ValueError("no VODs to evaluate: the candidates are empty and no video_ids given")
    flagged = set(flagged_video_ids)
    by_vod_candidates = candidates.partition_by("video_id", as_dict=True, include_key=True)
    by_vod_moments = moments.partition_by("video_id", as_dict=True, include_key=True)
    clip_views = (
        clips.filter(pl.col("video_id").is_not_null())
        .group_by("video_id", "clip_id")
        .agg(pl.col("view_count").max())
    )
    views_by_vod: dict[str, dict[str, int]] = {}
    for video_id, clip_id, views in clip_views.iter_rows():
        views_by_vod.setdefault(video_id, {})[clip_id] = views

    rows: list[dict[str, object]] = []
    excluded: dict[str, str] = {}
    for video_id in population:
        vod_candidates = [
            Candidate(r["candidate_id"], r["start_s"], r["end_s"], r["score"])
            for r in by_vod_candidates.get((video_id,), _empty(CANDIDATE_COLUMNS)).iter_rows(
                named=True
            )
        ]
        vod_moment_rows = list(
            by_vod_moments.get((video_id,), _empty(MOMENT_COLUMNS)).iter_rows(named=True)
        )
        vod_moments = [Moment(r["moment_id"], r["start_s"], r["end_s"]) for r in vod_moment_rows]
        hit = hit_rate_at_k(vod_candidates, vod_moments, k=k, tol_s=TOL_S)
        recall: float | None = None
        reason: str | None = None
        if not vod_moments:
            reason = "no observed moments"
        elif video_id in flagged:
            reason = "clip snapshot flagged: excluded from view-weighted metrics"
        else:
            top = top_clip_moments(
                {r["moment_id"]: r["clip_ids"] for r in vod_moment_rows},
                views_by_vod.get(video_id, {}),
                n_clips=TOP_CLIPS,
            )
            recall = top_clip_recall_at_k(vod_candidates, vod_moments, top, k=k, tol_s=TOL_S)
            if recall is None:
                reason = "no clip views for top-clip recall"
        if reason is not None:
            excluded[video_id] = reason
        rows.append(
            {
                "video_id": video_id,
                "n_candidates": len(vod_candidates),
                "n_moments": len(vod_moments),
                "hit_rate_at_10": hit,
                "top_clip_recall_at_10": recall,
                "excluded_reason": reason,
            }
        )
    per_vod = pl.DataFrame(
        rows,
        schema={
            "video_id": pl.String,
            "n_candidates": pl.Int64,
            "n_moments": pl.Int64,
            "hit_rate_at_10": pl.Float64,
            "top_clip_recall_at_10": pl.Float64,
            "excluded_reason": pl.String,
        },
    )
    summary = {
        metric: bootstrap_mean(per_vod[metric].drop_nulls().to_list(), seed=seed)
        for metric in METRICS
    }
    return EvalResult(per_vod, summary, len(population), excluded)


def write_report(
    result: EvalResult,
    out_dir: Path,
    *,
    run_id: str,
    dataset_version: str,
    git_sha: str,
    seed: int,
    scorer: str,
    split: str | None = None,
    inputs: Sequence[Path] = (),
    created_at: datetime | None = None,
) -> tuple[Path, Path]:
    """Write ``metrics.json`` and ``summary.md`` into `out_dir` (created if needed).

    Returns:
        The two paths.
    """
    created = (created_at or datetime.now(tz=UTC)).astimezone(UTC)
    out_dir.mkdir(parents=True, exist_ok=True)
    metrics = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "run_id": run_id,
        "created_at": created.isoformat().replace("+00:00", "Z"),
        "scorer": scorer,
        "dataset_version": dataset_version,
        "split": split,
        "git_sha": git_sha,
        "seed": seed,
        "k": TOP_K,
        "tol_s": TOL_S,
        "bootstrap": {"resamples": 1000, "confidence": 0.95, "unit": "vod"},
        "inputs": [str(path) for path in inputs],
        "n_vods": result.n_vods,
        "metrics": {
            name: None
            if interval is None
            else {
                "mean": interval.mean,
                "ci_low": interval.low,
                "ci_high": interval.high,
                "n_vods": result.per_vod[name].drop_nulls().len(),
            }
            for name, interval in result.summary.items()
        },
        "excluded": result.excluded,
        "per_vod": result.per_vod.to_dicts(),
    }
    metrics_path = out_dir / "metrics.json"
    metrics_path.write_text(
        json.dumps(metrics, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8"
    )
    summary_path = out_dir / "summary.md"
    summary_path.write_text(_summary_markdown(metrics), encoding="utf-8")
    return metrics_path, summary_path


def _summary_markdown(metrics: dict[str, object]) -> str:
    lines = [
        f"# Evaluation {metrics['run_id']}",
        "",
        f"- Scorer: `{metrics['scorer']}`",
        f"- Dataset version: `{metrics['dataset_version']}`",
        f"- Split: {metrics['split'] or 'not given'}",
        f"- Git commit: `{metrics['git_sha']}`",
        f"- Seed: {metrics['seed']}",
        f"- Created: {metrics['created_at']}",
        f"- VODs: {metrics['n_vods']}",
        "",
        "| Metric | Mean | 95% CI | VODs |",
        "|---|---|---|---|",
    ]
    values = metrics["metrics"]
    assert isinstance(values, dict)
    for name in METRICS:
        value = values[name]
        if value is None:
            lines.append(f"| {name} | n/a | n/a | 0 |")
        else:
            lines.append(
                f"| {name} | {value['mean']:.3f} | {value['ci_low']:.3f} to {value['ci_high']:.3f} "
                f"| {value['n_vods']} |"
            )
    excluded = metrics["excluded"]
    assert isinstance(excluded, dict)
    if excluded:
        lines += ["", "Excluded VODs:", ""]
        lines += [f"- `{video_id}`: {reason}" for video_id, reason in sorted(excluded.items())]
    return "\n".join(lines) + "\n"


def _check(
    frame: pl.DataFrame, columns: Sequence[str], name: str, *, unique: str | None = None
) -> None:
    missing = sorted(set(columns) - set(frame.columns))
    if missing:
        raise ValueError(f"{name} is missing columns: {', '.join(missing)}")
    nulls = [c for c in columns if frame[c].null_count()]
    if nulls:
        raise ValueError(f"{name} has nulls in: {', '.join(nulls)}")
    if unique is not None and frame[unique].n_unique() != frame.height:
        raise ValueError(f"{name} lists a {unique} more than once")


def _check_types(candidates: pl.DataFrame, moments: pl.DataFrame, clips: pl.DataFrame) -> None:
    """IDs must be strings (Twitch IDs are never numbers here) and times and scores numeric."""
    expected = [
        (candidates, "candidates", ("video_id", "candidate_id"), ("start_s", "end_s", "score")),
        (moments, "moments", ("video_id", "moment_id"), ("start_s", "end_s")),
        (clips, "clips", ("clip_id", "video_id"), ("view_count",)),
    ]
    for frame, name, strings, numbers in expected:
        for column in strings:
            if frame.schema[column] != pl.String:
                raise ValueError(f"{name}.{column} must be strings, got {frame.schema[column]}")
        for column in numbers:
            if not frame.schema[column].is_numeric():
                raise ValueError(f"{name}.{column} must be numeric, got {frame.schema[column]}")


def _empty(columns: Sequence[str]) -> pl.DataFrame:
    return pl.DataFrame({c: [] for c in columns})


class ReportExistsError(FileExistsError):
    """The run directory already holds a report (pass ``force`` to replace it)."""


def evaluate_files(
    candidates_path: Path,
    moments_path: Path,
    clips_path: Path,
    *,
    out_dir: Path,
    run_id: str,
    dataset_version: str,
    scorer: str,
    seed: int,
    git_sha: str,
    split: str | None = None,
    video_ids: Iterable[str] | None = None,
    flagged_video_ids: Iterable[str] = (),
    force: bool = False,
) -> tuple[EvalResult, Path, Path]:
    """Read the three Parquet tables, `evaluate` and `write_report` into `out_dir`.

    `video_ids`, `flagged_video_ids` and `split` are passed through to `evaluate` and the report.

    Raises:
        FileNotFoundError: An input file doesn't exist.
        ReportExistsError: `out_dir` already has a ``metrics.json`` and `force` is False.
        ValueError: As for `evaluate`, or an input isn't a readable Parquet file.
    """
    paths = (candidates_path, moments_path, clips_path)
    for path in paths:
        if not path.is_file():
            raise FileNotFoundError(f"input not found: {path}")
    if (out_dir / "metrics.json").exists() and not force:
        raise ReportExistsError(f"{out_dir} already has a report; pick another run ID or --force")
    try:
        candidates, moments, clips = (pl.read_parquet(path) for path in paths)
    except pl.exceptions.PolarsError as exc:
        raise ValueError(f"can't read the inputs as Parquet: {exc}") from exc
    result = evaluate(
        candidates,
        moments,
        clips,
        seed=seed,
        video_ids=video_ids,
        flagged_video_ids=flagged_video_ids,
    )
    metrics_path, summary_path = write_report(
        result,
        out_dir,
        run_id=run_id,
        dataset_version=dataset_version,
        git_sha=git_sha,
        seed=seed,
        scorer=scorer,
        split=split,
        inputs=paths,
    )
    return result, metrics_path, summary_path


def current_git_sha(cwd: Path | None = None) -> str:
    """The commit of the checkout holding `cwd`, or ``"unknown"`` outside a git checkout.

    `cwd` defaults to this package's own directory, so the SHA is that of the code running,
    wherever the command is started from.

    ``-dirty`` is appended when tracked files have local changes, so a report never claims a
    commit it wasn't produced from.
    """
    cwd = Path(__file__).resolve().parent if cwd is None else cwd
    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return f"{head}-dirty" if status else head


def default_run_id(scorer: str, now: datetime | None = None) -> str:
    """``eval-<scorer>-<UTC time>``, e.g. ``eval-chat_spike-20261104T120000Z``."""
    stamp = (now or datetime.now(tz=UTC)).astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"eval-{scorer}-{stamp}"
