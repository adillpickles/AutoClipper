"""Tests for `autoclipper.eval.taste_test`: pairing, the page and key, and scoring.

Synthetic analysis results only; the "clips" are a few bytes of text written by a fake
renderer, so nothing here needs FFmpeg or real media.
"""

import csv
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from autoclipper.analysis import AnalysisResult, Candidate, ScorerInfo, VodSummary
from autoclipper.eval import taste_test as tt

NOW = datetime(2026, 11, 28, 12, tzinfo=UTC)


def vod(video_id: str = "9000000001") -> VodSummary:
    return VodSummary(
        video_id=video_id,
        title="Synthetic stream",
        broadcaster_id="9100000001",
        broadcaster_display_name="user_001",
        duration_s=3600.0,
        created_at=datetime(2026, 10, 12, 18, tzinfo=UTC),
    )


def result(name: str, starts: list[float], video_id: str = "9000000001") -> AnalysisResult:
    candidates = [
        Candidate(
            candidate_id=f"{name}-{i:02d}",
            rank=i,
            score=100.0 - i,
            start_s=start,
            end_s=start + 30.0,
            source="discovered",
            n_viewer_clips=0,
        )
        for i, start in enumerate(starts, start=1)
    ]
    return AnalysisResult(
        generated_at=NOW,
        vod=vod(video_id),
        scorer=ScorerInfo(kind="heuristic", name=name),
        n_viewer_clips_total=0,
        candidates=tuple(candidates),
    )


class FakeRenderer:
    """Writes a tiny text file instead of an MP4 and counts calls."""

    def __init__(self) -> None:
        self.calls: list[tuple[float, float]] = []

    def __call__(self, video: Path, start_s: float, end_s: float, output: Path) -> None:
        self.calls.append((start_s, end_s))
        output.write_text(f"{video.name} {start_s}-{end_s}", encoding="utf-8")


OURS = [100.0, 400.0, 700.0, 1000.0, 1300.0]
# 405 s overlaps ours' 400 s (IoU 25/35 ≥ 0.5): one shared moment, left out.
BASE = [200.0, 405.0, 800.0, 1100.0, 1400.0]


def inputs(tmp_path: Path) -> list[tt.MethodPair]:
    video = tmp_path / "vod.mp4"
    video.write_text("not a video", encoding="utf-8")
    return [
        tt.MethodPair(
            ours=result("lightgbm", OURS), baseline=result("chat_spike", BASE), video=video
        )
    ]


_DEFAULT = FakeRenderer()


def build(
    tmp_path: Path,
    test_id: str = "tt-a",
    seed: int = 7,
    *,
    renderer: tt.Renderer | None = _DEFAULT,
    pairs: list[tt.MethodPair] | None = None,
    captions: bool = False,
) -> tt.BuiltTest:
    return tt.build_test(
        pairs or inputs(tmp_path),
        tmp_path / "tests" / test_id,
        tt.BuildOptions(test_id=test_id, seed=seed, captions=captions),
        renderer=FakeRenderer() if renderer is _DEFAULT else renderer,
        clip_cache=tmp_path / "clips",
        git_sha="abc1234",
        now=NOW,
    )


def page_data(page: Path) -> dict[str, Any]:
    text = page.read_text(encoding="utf-8")
    match = re.search(r"const TEST = (\{.*?\});\n", text)
    assert match is not None
    return json.loads(match.group(1))


# --- pairing ---------------------------------------------------------------------------------


def test_iou() -> None:
    assert tt.iou((0, 10), (5, 15)) == pytest.approx(5 / 15)
    assert tt.iou((0, 10), (10, 20)) == 0.0
    assert tt.iou((0, 10), (0, 10)) == 1.0


def test_shared_moments_are_left_out_of_both_lists() -> None:
    ours = result("a", [100.0, 400.0, 410.0, 700.0]).candidates
    base = result("b", [405.0, 900.0]).candidates

    ours_only, base_only, shared = tt.shared_moments(ours, base)

    # 400 s is matched with 405 s; 410 s overlaps that same baseline moment and goes too.
    assert [c.start_s for c in ours_only] == [100.0, 700.0]
    assert [c.start_s for c in base_only] == [900.0]
    assert [(o.start_s, b.start_s) for o, b in shared] == [(400.0, 405.0)]


# --- build -----------------------------------------------------------------------------------


def test_build_writes_an_anonymous_page_and_a_private_key(tmp_path: Path) -> None:
    built = build(tmp_path)

    assert built.n_pairs == 4
    assert built.n_shared == 1
    assert built.n_rendered == 8
    assert built.n_reused == 0
    key = json.loads(built.key.read_text(encoding="utf-8"))
    assert key["format"] == tt.KEY_FORMAT
    assert key["seed"] == 7
    assert key["git_sha"] == "abc1234"
    assert key["shared"] == [{"video_id": "9000000001", "ours_rank": 2, "baseline_rank": 2}]
    assert [p["pair_id"] for p in key["pairs"]] == ["pair-01", "pair-02", "pair-03", "pair-04"]
    for pair in key["pairs"]:
        assert {pair["A"]["role"], pair["B"]["role"]} == {"ours", "baseline"}
    # Pairs stay rank-matched: ours' i-th unshared clip against the baseline's i-th.
    by_rank = sorted(
        (p[s] for p in key["pairs"] for s in "AB" if p[s]["role"] == "ours"),
        key=lambda c: c["rank"],
    )
    assert [c["start_s"] for c in by_rank] == [100.0, 700.0, 1000.0, 1300.0]

    # The key stays outside the folder raters get, and the page names no method.
    page_dir = built.page.parent
    assert built.key.parent == page_dir.parent
    files = sorted(p.relative_to(page_dir).as_posix() for p in page_dir.rglob("*") if p.is_file())
    assert files == ["index.html"] + [f"media/pair-0{i}-{s}.mp4" for i in range(1, 5) for s in "ab"]
    text = built.page.read_text(encoding="utf-8")
    for leak in ("lightgbm", "chat_spike", "ours", "baseline", "100.0", "9000000001"):
        assert leak not in text
    data = page_data(built.page)
    assert data == {
        "format": tt.ANSWERS_FORMAT,
        "test_id": "tt-a",
        "pairs": [
            {"id": f"pair-0{i}", "a": f"media/pair-0{i}-a.mp4", "b": f"media/pair-0{i}-b.mp4"}
            for i in range(1, 5)
        ],
    }
    # Each media file is the render the key says it is.
    for pair in key["pairs"]:
        for side in "AB":
            clip = pair[side]
            media = page_dir / "media" / f"{pair['pair_id']}-{side.lower()}.mp4"
            assert media.read_text(encoding="utf-8") == f"vod.mp4 {clip['start_s']}-{clip['end_s']}"


def test_build_is_reproducible_from_its_seed(tmp_path: Path) -> None:
    def layout(seed: int, test_id: str) -> list[tuple[float, str]]:
        key = json.loads(build(tmp_path, test_id, seed).key.read_text(encoding="utf-8"))
        return [(p["A"]["start_s"], p["A"]["role"]) for p in key["pairs"]]

    first = layout(7, "tt-a")
    assert layout(7, "tt-b") == first
    assert any(layout(seed, f"tt-s{seed}") != first for seed in range(8, 14))


def test_a_second_build_reuses_the_rendered_clips(tmp_path: Path) -> None:
    build(tmp_path, "tt-a")
    renderer = FakeRenderer()

    second = build(tmp_path, "tt-b", seed=3, renderer=renderer)

    assert renderer.calls == []
    assert second.n_reused == 8
    # Without a renderer, cached clips are still enough.
    assert build(tmp_path, "tt-c", renderer=None).n_pairs == 4


def test_render_settings_are_part_of_the_cache_name(tmp_path: Path) -> None:
    build(tmp_path, "tt-a")
    renderer = FakeRenderer()

    build(tmp_path, "tt-b", renderer=renderer, captions=True)

    assert len(renderer.calls) == 8


def test_build_rejects_bad_inputs(tmp_path: Path) -> None:
    video = tmp_path / "vod.mp4"
    mismatch = [tt.MethodPair(result("a", OURS), result("b", BASE, video_id="9000000002"), video)]
    with pytest.raises(tt.TasteTestError, match="different VODs"):
        build(tmp_path, pairs=mismatch)
    same = [tt.MethodPair(result("a", OURS), result("b", OURS), video)]
    with pytest.raises(tt.TasteTestError, match="no pairs left"):
        build(tmp_path, pairs=same)
    with pytest.raises(tt.TasteTestError, match="isn't rendered"):
        build(tmp_path, renderer=None)
    build(tmp_path, "tt-a")
    with pytest.raises(tt.TasteTestError, match="already holds"):
        build(tmp_path, "tt-a")


# --- scoring ---------------------------------------------------------------------------------


def test_wilson_interval_and_sign_test() -> None:
    low, high = tt.wilson_interval(8, 10) or (0.0, 0.0)
    assert (low, high) == (pytest.approx(0.4902, abs=1e-4), pytest.approx(0.9433, abs=1e-4))
    assert tt.wilson_interval(0, 0) is None
    assert tt.sign_test_p(8, 10) == pytest.approx(0.109375)
    assert tt.sign_test_p(5, 10) == pytest.approx(1.0)
    assert tt.sign_test_p(10, 10) == pytest.approx(2 / 1024)
    assert tt.sign_test_p(0, 0) is None


def sheet(
    key: dict[str, Any], rater: str, pick: str, submitted_at: str = "2026-11-28T12:00:00Z"
) -> dict[str, Any]:
    """An answer file choosing the clip of role `pick` (or "tie") in every pair."""
    answers = []
    for pair in key["pairs"]:
        side = "tie" if pick == "tie" else ("A" if pair["A"]["role"] == pick else "B")
        answers.append(
            {
                "pair_id": pair["pair_id"],
                "choice": side,
                "rating_a": 4 if pair["A"]["role"] == "ours" else 2,
                "rating_b": 4 if pair["B"]["role"] == "ours" else 2,
                "note": "",
            }
        )
    return {
        "format": tt.ANSWERS_FORMAT,
        "test_id": key["test_id"],
        "rater": rater,
        "submitted_at": submitted_at,
        "answers": answers,
    }


def write(path: Path, data: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_score_reports_win_rates_without_names_or_notes(tmp_path: Path) -> None:
    built = build(tmp_path)
    key = json.loads(built.key.read_text(encoding="utf-8"))
    answers = tmp_path / "answers"
    write(answers / "1.json", sheet(key, "Alice Example", "ours"))
    write(answers / "2.json", sheet(key, "Bob", "ours"))
    third = sheet(key, "carol", "baseline")
    third["answers"][0]["note"] = "Alice's friend says B is funnier"
    third["answers"][1]["choice"] = "tie"
    write(answers / "3.json", third)
    # An older submission by the same rater (names compare case-insensitively) is replaced.
    write(
        answers / "0.json",
        sheet(key, "ALICE EXAMPLE", "baseline", submitted_at="2026-11-28T11:00:00Z"),
    )
    out = tmp_path / "experiments" / "exp-999"

    scored = tt.score_test(built.key, [answers], out, git_sha="def5678")

    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert summary == scored.summary
    assert summary["n_raters"] == 3
    assert summary["n_judgments"] == 12
    assert summary["n_notes"] == 1
    assert summary["n_pairs_built"] == 4
    assert summary["n_shared_left_out"] == 1
    c = summary["comparisons"]["lightgbm vs chat_spike"]
    assert (c["ours_wins"], c["baseline_wins"], c["ties"]) == (8, 3, 1)
    assert c["win_rate"] == pytest.approx(8 / 11, abs=1e-4)
    assert c["win_rate_ties_half"] == pytest.approx(8.5 / 12, abs=1e-4)
    low, high = c["win_rate_ci95_wilson"]
    assert low < 8 / 11 < high
    assert c["sign_test_p"] == pytest.approx(tt.sign_test_p(8, 11), abs=1e-4)
    assert c["mean_rating"]["ours"] == {"n": 12, "mean": 4.0, "ci95_pair_bootstrap": [4.0, 4.0]}
    assert c["mean_rating"]["baseline"]["mean"] == 2.0
    assert summary["agreement"]["n_pairs"] == 4
    # Pair 1: ours, ours, baseline -> 1 of 3 rater pairs agree; pair 2 has a tie: also 1/3.
    assert summary["agreement"]["pairwise_percent"] == pytest.approx(1 / 3, abs=1e-4)

    published = "".join(p.read_text(encoding="utf-8") for p in out.iterdir())
    for private in ("Alice", "ALICE", "Bob", "carol", "funnier"):
        assert private not in published
    with (out / "ratings.csv").open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 12
    assert {r["rater"] for r in rows} == {"rater_1", "rater_2", "rater_3"}
    assert sum(r["winner"] == "ours" for r in rows) == 8
    assert scored.notes_path == built.key.parent / "notes.md"
    assert "funnier" in (built.key.parent / "notes.md").read_text(encoding="utf-8")
    assert "lightgbm vs chat_spike" in (out / "summary.md").read_text(encoding="utf-8")


def test_score_is_reproducible(tmp_path: Path) -> None:
    built = build(tmp_path)
    key = json.loads(built.key.read_text(encoding="utf-8"))
    mixed = sheet(key, "a", "ours")
    mixed["answers"][0]["choice"] = "tie"
    files = [
        write(tmp_path / "a.json", mixed),
        write(tmp_path / "b.json", sheet(key, "b", "baseline")),
    ]

    first = tt.score_test(built.key, files, tmp_path / "one").summary
    second = tt.score_test(built.key, files, tmp_path / "two").summary

    assert first == second


def test_skipped_pairs_are_not_judgments(tmp_path: Path) -> None:
    built = build(tmp_path)
    key = json.loads(built.key.read_text(encoding="utf-8"))
    partial = sheet(key, "a", "ours")
    partial["answers"][0]["choice"] = None
    partial["answers"][0]["rating_a"] = partial["answers"][0]["rating_b"] = None

    summary = tt.score_test(
        built.key, [write(tmp_path / "a.json", partial)], tmp_path / "o"
    ).summary

    assert summary["n_judgments"] == 3
    assert summary["agreement"] == {"pairwise_percent": None, "n_pairs": 0}


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"test_id": "other"}, "is for test"),
        ({"format": "nope"}, "isn't a taste test answer file"),
        ({"rater": " "}, "no rater name"),
    ],
)
def test_score_rejects_foreign_answer_files(
    tmp_path: Path, change: dict[str, Any], message: str
) -> None:
    built = build(tmp_path)
    key = json.loads(built.key.read_text(encoding="utf-8"))
    path = write(tmp_path / "a.json", {**sheet(key, "a", "ours"), **change})

    with pytest.raises(tt.TasteTestError, match=message):
        tt.score_test(built.key, [path], tmp_path / "out")


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("pair_id", "pair-99", "unknown pair"),
        ("choice", "C", "isn't A/B/tie"),
        ("rating_a", 6, "isn't 1-5"),
    ],
)
def test_score_rejects_bad_answers(tmp_path: Path, field: str, value: object, message: str) -> None:
    built = build(tmp_path)
    key = json.loads(built.key.read_text(encoding="utf-8"))
    data = sheet(key, "a", "ours")
    data["answers"][0][field] = value

    with pytest.raises(tt.TasteTestError, match=message):
        tt.score_test(built.key, [write(tmp_path / "a.json", data)], tmp_path / "out")


def test_score_rejects_a_file_that_is_not_a_key(tmp_path: Path) -> None:
    path = write(tmp_path / "key.json", {"format": "something else"})

    with pytest.raises(tt.TasteTestError, match="isn't a taste test key"):
        tt.score_test(path, [], tmp_path / "out")
