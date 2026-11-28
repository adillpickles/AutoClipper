"""Tests for scripts/taste_test.py: ``build`` then ``score`` from the command line.

Synthetic analysis results; the clip renderer is replaced with one that writes a few bytes, so
nothing needs FFmpeg or media.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
import taste_test as script
from typer.testing import CliRunner

from autoclipper.analysis import AnalysisResult, Candidate, ScorerInfo, VodSummary
from autoclipper.cli import ExitCode
from autoclipper.config import get_settings
from autoclipper.eval import taste_test as tt

OURS = [100.0, 400.0, 700.0, 1000.0, 1300.0]
BASE = [200.0, 405.0, 800.0, 1100.0, 1400.0]  # 405 s is the same moment as 400 s


def result(name: str, starts: list[float]) -> AnalysisResult:
    return AnalysisResult(
        generated_at=datetime(2026, 11, 28, 12, tzinfo=UTC),
        vod=VodSummary(
            video_id="9000000001",
            title="Synthetic stream",
            broadcaster_id="9100000001",
            broadcaster_display_name="user_001",
            duration_s=3600.0,
            created_at=datetime(2026, 10, 12, 18, tzinfo=UTC),
        ),
        scorer=ScorerInfo(kind="heuristic", name=name),
        n_viewer_clips_total=0,
        candidates=tuple(
            Candidate(
                candidate_id=f"{name}-{i}",
                rank=i,
                score=100.0 - i,
                start_s=start,
                end_s=start + 30.0,
                source="discovered",
                n_viewer_clips=0,
            )
            for i, start in enumerate(starts, start=1)
        ),
    )


def fake_render(video: Path, start_s: float, end_s: float, output: Path) -> None:
    output.write_text(f"{start_s}-{end_s}", encoding="utf-8")


def answers_for_ours(key: dict[str, object], path: Path) -> Path:
    """An answer file preferring the method under test in every pair."""
    pairs = key["pairs"]
    assert isinstance(pairs, list)
    data = {
        "format": tt.ANSWERS_FORMAT,
        "test_id": key["test_id"],
        "rater": "A Rater",
        "submitted_at": "2026-11-28T12:00:00Z",
        "answers": [
            {"pair_id": p["pair_id"], "choice": "A" if p["A"]["role"] == "ours" else "B"}
            for p in pairs
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


@pytest.fixture
def results(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, Path]:
    monkeypatch.setenv("AUTOCLIPPER_ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    get_settings.cache_clear()
    monkeypatch.setattr(script, "_clip_renderer", lambda layout, captions: fake_render)
    ours = tmp_path / "ours.json"
    ours.write_text(result("lightgbm", OURS).model_dump_json(), encoding="utf-8")
    base = tmp_path / "base.json"
    base.write_text(result("chat_spike", BASE).model_dump_json(), encoding="utf-8")
    video = tmp_path / "vod.mp4"
    video.write_text("not a video", encoding="utf-8")
    return ours, base, video


def test_build_then_score(results: tuple[Path, Path, Path], tmp_path: Path) -> None:
    ours, base, video = results
    runner = CliRunner()

    built = runner.invoke(
        script.app,
        ["build", "--ours", str(ours), "--baseline", str(base), "--video", str(video),
         "--test-id", "tt-1", "--seed", "3"],
    )  # fmt: skip

    assert built.exit_code == 0, built.output
    assert "4 pairs (1 shared moments left out; 8 clips rendered, 0 reused)" in built.output
    test_dir = tmp_path / "artifacts" / "taste-tests" / "tt-1"
    assert (test_dir / "page" / "index.html").is_file()
    assert len(list((tmp_path / "artifacts" / "taste-tests" / "clips").iterdir())) == 8
    key = json.loads((test_dir / "key.json").read_text(encoding="utf-8"))
    assert key["sources"][0]["ours"] == ours.as_posix()
    answers = answers_for_ours(key, test_dir / "answers" / "a.json")
    out = tmp_path / "experiments" / "exp-999"

    scored = runner.invoke(
        script.app, ["score", "--key", str(test_dir / "key.json"), "--answers", str(answers),
                     "--out", str(out)],
    )  # fmt: skip

    assert scored.exit_code == 0, scored.output
    assert "| lightgbm vs chat_spike | 4 | 4 | 4 / 0 / 0 | 100 %" in scored.output
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert summary["format"] == tt.SUMMARY_FORMAT
    assert summary["seed"] == 3


def test_build_checks_its_options(results: tuple[Path, Path, Path]) -> None:
    ours, base, _video = results
    runner = CliRunner()

    counts = runner.invoke(script.app, ["build", "--ours", str(ours), "--ours", str(ours),
                                        "--baseline", str(base)])  # fmt: skip
    bad_id = runner.invoke(
        script.app, ["build", "--ours", str(ours), "--baseline", str(base), "--test-id", "a/b"]
    )
    not_cached = runner.invoke(script.app, ["build", "--ours", str(ours), "--baseline", str(base)])

    assert counts.exit_code == ExitCode.USER_ERROR
    assert "one --baseline per --ours" in counts.output
    assert bad_id.exit_code == ExitCode.USER_ERROR
    assert "--test-id" in bad_id.output
    assert not_cached.exit_code == ExitCode.USER_ERROR
    assert "isn't rendered" in not_cached.output
