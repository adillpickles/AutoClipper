"""Tests for the evaluation harness, report files and the evaluate command."""

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import polars as pl
import pytest
from typer.testing import CliRunner

from autoclipper.cli import app
from autoclipper.eval.harness import (
    ReportExistsError,
    current_git_sha,
    default_run_id,
    evaluate,
    evaluate_files,
    evaluate_mean,
    write_report,
)
from autoclipper.labels.moments import CLIP_START, merge_clips_into_moments
from autoclipper.synthetic import generate_dataset

runner = CliRunner()


def candidates_frame(rows: list[tuple[str, str, float, float, float]]) -> pl.DataFrame:
    return pl.DataFrame(
        rows,
        schema={
            "video_id": pl.String,
            "candidate_id": pl.String,
            "start_s": pl.Float64,
            "end_s": pl.Float64,
            "score": pl.Float64,
        },
        orient="row",
    )


def moments_frame(
    rows: list[tuple[str, str, float, float, list[str]]], clip_position: str = "end"
) -> pl.DataFrame:
    return pl.DataFrame(
        rows,
        schema={
            "video_id": pl.String,
            "moment_id": pl.String,
            "start_s": pl.Float64,
            "end_s": pl.Float64,
            "clip_ids": pl.List(pl.String),
        },
        orient="row",
    ).with_columns(clip_position=pl.lit(clip_position))


def clips_frame(views: dict[str, tuple[str, int]]) -> pl.DataFrame:
    """Clips as {clip_id: (video_id, view_count)}."""
    return pl.DataFrame(
        {
            "clip_id": list(views),
            "video_id": [v for v, _ in views.values()],
            "view_count": [n for _, n in views.values()],
        }
    )


@pytest.fixture
def inputs() -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame]:
    candidates = candidates_frame(
        [
            ("a", "a:c0", 0.0, 30.0, 0.9),  # hits a:m0
            ("a", "a:c1", 400.0, 430.0, 0.8),  # miss
            ("b", "b:c0", 100.0, 120.0, 0.5),  # hits b:m0
            ("c", "c:c0", 0.0, 30.0, 0.5),  # VOD c has no moments
        ]
    )
    moments = moments_frame(
        [
            ("a", "a:m0", 5.0, 35.0, ["x1"]),
            ("a", "a:m1", 900.0, 930.0, ["x2"]),
            ("b", "b:m0", 100.0, 130.0, ["y1"]),
        ]
    )
    return candidates, moments, clips_frame({"x1": ("a", 10), "x2": ("a", 50), "y1": ("b", 3)})


def test_evaluate_scores_each_vod_and_averages_over_vods(
    inputs: tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame],
) -> None:
    result = evaluate(*inputs, seed=0)

    per_vod = {row["video_id"]: row for row in result.per_vod.iter_rows(named=True)}
    assert per_vod["a"]["hit_rate_at_10"] == 0.5
    assert per_vod["a"]["top_clip_recall_at_10"] == 0.5  # both moments are top-clip moments
    assert per_vod["b"]["hit_rate_at_10"] == 1.0
    assert per_vod["b"]["top_clip_recall_at_10"] == 1.0
    assert per_vod["c"]["hit_rate_at_10"] is None
    assert result.excluded == {"c": "no observed moments"}
    assert result.n_vods == 3
    hit = result.summary["hit_rate_at_10"]
    assert hit is not None
    assert hit.mean == pytest.approx(0.75)


def test_flagged_snapshots_are_left_out_of_top_clip_recall_only(
    inputs: tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame],
) -> None:
    result = evaluate(*inputs, seed=0, flagged_video_ids=["b"])

    row = result.per_vod.filter(pl.col("video_id") == "b").row(0, named=True)
    assert row["hit_rate_at_10"] == 1.0
    assert row["top_clip_recall_at_10"] is None
    assert "flagged" in result.excluded["b"]


def test_a_vod_with_moments_but_no_candidates_scores_zero(
    inputs: tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame],
) -> None:
    candidates, moments, clips = inputs

    result = evaluate(
        candidates.filter(pl.col("video_id") != "b"), moments, clips, seed=0, video_ids=["a", "b"]
    )

    row = result.per_vod.filter(pl.col("video_id") == "b").row(0, named=True)
    assert (row["hit_rate_at_10"], row["top_clip_recall_at_10"]) == (0.0, 0.0)


def test_moments_of_vods_outside_the_population_are_ignored(
    inputs: tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame],
) -> None:
    candidates, moments, clips = inputs
    training = moments_frame([("t", "t:m0", 0.0, 30.0, ["z"])])  # a training VOD's moment

    result = evaluate(candidates, pl.concat([moments, training]), clips, seed=0)

    assert set(result.per_vod["video_id"]) == {"a", "b", "c"}


def test_a_clip_listed_twice_counts_once(
    inputs: tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame],
) -> None:
    candidates, moments, clips = inputs

    result = evaluate(candidates, moments, pl.concat([clips, clips]), seed=0)

    assert (
        result.summary["top_clip_recall_at_10"]
        == evaluate(*inputs, seed=0).summary["top_clip_recall_at_10"]
    )


def test_nothing_to_evaluate_is_an_error(
    inputs: tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame],
) -> None:
    candidates, moments, clips = inputs

    with pytest.raises(ValueError, match="no VODs to evaluate"):
        evaluate(candidates.clear(), moments, clips, seed=0)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda c: c.with_columns(pl.col("video_id").cast(pl.Categorical)), "video_id must be"),
        (lambda c: c.with_columns(pl.col("score").cast(pl.String)), "score must be numeric"),
        (lambda c: c.drop("score"), "missing columns: score"),
        (lambda c: pl.concat([c, c]), "candidate_id more than once"),
        (lambda c: c.with_columns(score=pl.lit(float("nan"))), "score"),
        (lambda c: c.with_columns(end_s=pl.col("start_s")), "doesn't end after"),
    ],
)
def test_bad_candidates_are_rejected(
    inputs: tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame], change: object, message: str
) -> None:
    candidates, moments, clips = inputs

    with pytest.raises(ValueError, match=message):
        evaluate(change(candidates), moments, clips, seed=0)  # type: ignore[operator]


def test_report_records_dataset_commit_and_seed(
    tmp_path: Path, inputs: tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame]
) -> None:
    result = evaluate(*inputs, seed=3)

    metrics_path, summary_path = write_report(
        result,
        tmp_path / "run",
        run_id="eval-test",
        dataset_version="v-synthetic",
        git_sha="abc123",
        seed=3,
        scorer="chat_spike",
        split="splits/v1.json fold 0",
        created_at=datetime(2026, 11, 4, 12, 0, tzinfo=UTC),
    )

    metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
    assert metrics["dataset_version"] == "v-synthetic"
    assert (metrics["label_rules_version"], metrics["clip_position"]) == ("v1", "end")
    assert metrics["schema_version"] == 2
    assert metrics["git_sha"] == "abc123"
    assert metrics["seed"] == 3
    assert metrics["split"] == "splits/v1.json fold 0"
    assert metrics["created_at"] == "2026-11-04T12:00:00Z"
    assert metrics["metrics"]["hit_rate_at_10"]["mean"] == pytest.approx(0.75)
    assert metrics["metrics"]["hit_rate_at_10"]["n_vods"] == 2
    assert metrics["excluded"] == {"c": "no observed moments"}
    summary = summary_path.read_text(encoding="utf-8")
    for text in ("v-synthetic", "abc123", "Seed: 3", "hit_rate_at_10 | 0.750", "`c`: no observed"):
        assert text in summary
    assert "Label rules: `v1` (viewer-clip positions read as clip ends)" in summary


# Clip positions (#126): the match runs against moments read with one recorded rule.


def test_evaluation_matches_a_clip_where_it_ends() -> None:
    # A clip reported at 100 s, 30 s long, covers [70, 100); one reported at 10 s covers [0, 10).
    # The late candidates are within 15 s of where the old reading put the clips ([100, 130)
    # and [10, 40)) but 16 s past where they are.
    clips = pl.DataFrame(
        {
            "clip_id": ["x", "y"],
            "video_id": ["a", "b"],
            "vod_offset_s": [100.0, 10.0],
            "duration_s": [30.0, 30.0],
            "view_count": [5, 5],
            "created_at": [datetime(2026, 1, 1, tzinfo=UTC)] * 2,
            "fetched_at": [datetime(2026, 1, 9, tzinfo=UTC)] * 2,
        }
    )
    moments = merge_clips_into_moments(clips).moments
    assert moments.select("video_id", "start_s", "end_s").rows() == [
        ("a", 70.0, 100.0),
        ("b", 0.0, 10.0),
    ]
    late = candidates_frame([("a", "a:late", 116.0, 146.0, 1.0), ("b", "b:c", 26.0, 56.0, 1.0)])
    on_clip = candidates_frame([("a", "a:on", 70.0, 100.0, 1.0), ("b", "b:c", 20.0, 50.0, 1.0)])
    old = merge_clips_into_moments(clips, rule=CLIP_START).moments

    assert evaluate(late, moments, clips, seed=0).per_vod["hit_rate_at_10"].to_list() == [0, 0]
    assert evaluate(on_clip, moments, clips, seed=0).per_vod["hit_rate_at_10"].to_list() == [1, 1]
    result = evaluate(late, old, clips, seed=0)
    assert result.per_vod["hit_rate_at_10"].to_list() == [1, 1]
    assert (result.label_rules_version, result.clip_position) == ("v0", "start")


def test_moments_without_one_known_clip_position_are_rejected() -> None:
    candidates = candidates_frame([("a", "a:c", 0.0, 30.0, 1.0)])
    clips = clips_frame({"x": ("a", 5)})
    end = moments_frame([("a", "a:m0", 0.0, 30.0, ["x"])])
    start = moments_frame([("a", "a:m1", 100.0, 130.0, ["x"])], clip_position="start")

    with pytest.raises(ValueError, match=r"no clip_position column.*rebuild"):
        evaluate(candidates, end.drop("clip_position"), clips, seed=0)
    with pytest.raises(ValueError, match="mix clip_position rules: end, start"):
        evaluate(candidates, pl.concat([end, start]), clips, seed=0)
    with pytest.raises(ValueError, match="unknown clip_position 'middle'"):
        evaluate(candidates, end.with_columns(clip_position=pl.lit("middle")), clips, seed=0)


def test_evaluate_files_refuses_to_overwrite_a_report(
    tmp_path: Path, inputs: tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame]
) -> None:
    paths = []
    for name, frame in zip(("candidates", "moments", "clips"), inputs, strict=True):
        path = tmp_path / f"{name}.parquet"
        frame.write_parquet(path)
        paths.append(path)
    kwargs = {
        "out_dir": tmp_path / "report",
        "run_id": "r",
        "dataset_version": "v",
        "scorer": "s",
        "seed": 0,
        "git_sha": "x",
    }

    evaluate_files(*paths, **kwargs)  # type: ignore[arg-type]
    with pytest.raises(ReportExistsError):
        evaluate_files(*paths, **kwargs)  # type: ignore[arg-type]
    evaluate_files(*paths, force=True, **kwargs)  # type: ignore[arg-type]
    with pytest.raises(FileNotFoundError):
        evaluate_files(tmp_path / "missing.parquet", *paths[1:], force=True, **kwargs)  # type: ignore[arg-type]


def test_git_sha_is_the_commit_or_unknown(tmp_path: Path) -> None:
    assert current_git_sha(tmp_path) == "unknown"
    repo = Path(__file__).parents[2]
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    assert current_git_sha(repo).removesuffix("-dirty") == head
    # By default the SHA is the running code's, wherever the command starts.
    assert current_git_sha().removesuffix("-dirty") == head


def test_default_run_id_names_the_scorer_and_utc_time() -> None:
    now = datetime(2026, 11, 4, 7, 5, 9, tzinfo=UTC)

    assert default_run_id("chat_spike", now) == "eval-chat_spike-20261104T070509Z"


def test_evaluate_command_on_the_synthetic_dataset(tmp_path: Path) -> None:
    # Candidates: each clip-derived moment's own span (a perfect scorer) plus one miss per VOD.
    dataset = generate_dataset(tmp_path / "synthetic", seed=1)
    clips = pl.read_parquet(dataset.out_dir / "processed" / "clips" / "synthetic-seed-1.parquet")
    moments = merge_clips_into_moments(clips).moments
    perfect = moments.select(
        "video_id",
        candidate_id=pl.col("moment_id") + "-c",
        start_s="start_s",
        end_s="end_s",
        score=pl.lit(1.0),
    )
    misses = (
        moments.group_by("video_id")
        .agg()
        .with_columns(
            candidate_id=pl.col("video_id") + ":miss",
            start_s=pl.lit(5000.0),
            end_s=pl.lit(5030.0),
            score=pl.lit(0.0),
        )
    )
    for name, frame in (
        ("candidates", pl.concat([perfect, misses])),
        ("moments", moments),
        ("clips", clips),
    ):
        frame.write_parquet(tmp_path / f"{name}.parquet")

    result = runner.invoke(
        app,
        [
            "evaluate",
            *("--candidates", str(tmp_path / "candidates.parquet")),
            *("--moments", str(tmp_path / "moments.parquet")),
            *("--clips", str(tmp_path / "clips.parquet")),
            *("--dataset-version", "synthetic-seed-1", "--scorer", "perfect", "--run-id", "t1"),
        ],
    )

    assert result.exit_code == 0, result.output
    metrics = json.loads((tmp_path / "artifacts" / "reports" / "t1" / "metrics.json").read_text())
    n_moments = dict(moments.group_by("video_id").len().iter_rows())
    expected = sum(n / (n + 1) for n in n_moments.values()) / len(n_moments)
    assert metrics["metrics"]["hit_rate_at_10"]["mean"] == pytest.approx(expected)
    assert metrics["metrics"]["top_clip_recall_at_10"]["mean"] == 1.0
    assert "hit_rate_at_10:" in result.output


def test_evaluate_command_rejects_a_bad_run_id_and_missing_files(tmp_path: Path) -> None:
    base = [
        "evaluate",
        *("--candidates", str(tmp_path / "c.parquet")),
        *("--moments", str(tmp_path / "m.parquet")),
        *("--clips", str(tmp_path / "x.parquet")),
        *("--dataset-version", "v", "--scorer", "s"),
    ]

    bad_id = runner.invoke(app, [*base, "--run-id", "../escape"])
    missing = runner.invoke(app, base)

    assert bad_id.exit_code == 1
    assert "run-id" in bad_id.output
    assert missing.exit_code == 1
    assert "input not found" in missing.output


def test_evaluate_command_limits_vods_and_flags_snapshots(
    tmp_path: Path, inputs: tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame]
) -> None:
    for name, frame in zip(("candidates", "moments", "clips"), inputs, strict=True):
        frame.write_parquet(tmp_path / f"{name}.parquet")
    (tmp_path / "test-vods.txt").write_text("# fold 0 test VODs\na\nb\n", encoding="utf-8")
    (tmp_path / "flagged.txt").write_text("b\n", encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "evaluate",
            *("--candidates", str(tmp_path / "candidates.parquet")),
            *("--moments", str(tmp_path / "moments.parquet")),
            *("--clips", str(tmp_path / "clips.parquet")),
            *("--dataset-version", "v", "--scorer", "s", "--run-id", "limited"),
            *("--vods", str(tmp_path / "test-vods.txt")),
            *("--flagged", str(tmp_path / "flagged.txt")),
            *("--split", "fold 0"),
        ],
    )

    assert result.exit_code == 0, result.output
    report = json.loads(
        (tmp_path / "artifacts" / "reports" / "limited" / "metrics.json").read_text()
    )
    assert report["n_vods"] == 2
    assert report["split"] == "fold 0"
    assert report["metrics"]["top_clip_recall_at_10"]["n_vods"] == 1  # b is flagged


@pytest.mark.parametrize(
    ("option", "value", "message"),
    [
        ("--scorer", "my scorer", "--scorer"),
        ("--run-id", "CON", "--run-id"),
        ("--run-id", "nul.txt", "--run-id"),
    ],
)
def test_evaluate_command_rejects_unsafe_names(
    tmp_path: Path, option: str, value: str, message: str
) -> None:
    args = {
        "--candidates": "c.parquet",
        "--moments": "m.parquet",
        "--clips": "x.parquet",
        "--dataset-version": "v",
        "--scorer": "s",
        option: value,
    }

    result = runner.invoke(app, ["evaluate", *[x for kv in args.items() for x in kv]])

    assert result.exit_code == 1
    assert message in result.output


def test_evaluate_validation_edge_cases_and_missing_clip_views_summary(
    tmp_path: Path, inputs: tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame]
) -> None:
    candidates, moments, clips = inputs

    with pytest.raises(ValueError, match="clips is missing columns: video_id"):
        evaluate(candidates, moments, clips.drop("video_id"), seed=0)

    bad_bound = candidates.with_columns(end_s=pl.lit(float("inf")))
    with pytest.raises(ValueError, match="start_s or end_s that isn't finite"):
        evaluate(bad_bound, moments, clips, seed=0)

    null_col = candidates.with_columns(score=pl.lit(None, dtype=pl.Float64))
    with pytest.raises(ValueError, match="candidates has nulls in: score"):
        evaluate(null_col, moments, clips, seed=0)

    with pytest.raises(ValueError, match="runs is empty"):
        evaluate_mean([], moments, clips, seed=0)

    # Moments whose clip_ids have no view rows exclude top-clip recall only; hit rate still holds.
    no_views_result = evaluate(candidates, moments, clips.head(0), seed=0, video_ids=["a"])
    assert no_views_result.excluded["a"] == "no clip views for top-clip recall"
    assert no_views_result.per_vod["hit_rate_at_10"].to_list() == [0.5]
    assert no_views_result.summary["hit_rate_at_10"] is not None
    assert no_views_result.summary["hit_rate_at_10"].mean == pytest.approx(0.5)
    assert no_views_result.summary["top_clip_recall_at_10"] is None
    _, summary_path = write_report(
        no_views_result,
        tmp_path / "report",
        run_id="no-views",
        dataset_version="v1",
        git_sha="abc123",
        seed=0,
        scorer="chat_spike",
    )
    assert "| top_clip_recall_at_10 | n/a | n/a | 0 |" in summary_path.read_text(encoding="utf-8")

    # Corrupt Parquet file raises ValueError from evaluate_files.
    corrupt = tmp_path / "corrupt.parquet"
    corrupt.write_bytes(b"not a parquet file")
    m_path = tmp_path / "moments.parquet"
    c_path = tmp_path / "clips.parquet"
    moments.write_parquet(m_path)
    clips.write_parquet(c_path)
    with pytest.raises(ValueError, match="can't read the inputs as Parquet"):
        evaluate_files(
            corrupt,
            m_path,
            c_path,
            out_dir=tmp_path / "corrupt-report",
            run_id="corrupt",
            dataset_version="v1",
            scorer="chat_spike",
            seed=0,
            git_sha="abc123",
        )
