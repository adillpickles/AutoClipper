"""Blind taste test: people compare our clips with another method's (docs/ml/evaluation.md § 7).

Two steps, with real people rating in between:

1. **Build** (`build_test`): takes pairs of `AnalysisResult`s, one per VOD from each method
   ("ours", the scorer under test, and "baseline", what it is compared with), drops the moments
   both methods picked (`shared_moments`), pairs the remaining top clips by rank, renders each
   clip once (or reuses a render from an earlier build), and shuffles the pairs and which side
   is A with a saved seed. It writes a self-contained rating page (``page/index.html`` plus
   anonymized copies of the clips under ``page/media/``) and a private key (``key.json``) that
   maps every pair's A and B to its method. Raters only ever get the page folder.
2. **Score** (`score_test`): reads the answer files the page exports plus the key, and reports
   per comparison (ours' scorer against the baseline's) the win rate with a Wilson interval
   and an exact two-sided sign test, a pair-resampled bootstrap interval, the mean 1-5 ratings
   when raters gave them, position bias and inter-rater agreement. Rater names are replaced
   with ``rater_1``... and free-text notes never leave the test's artifacts folder.

Times are float seconds from VOD start. Nothing here needs a server: the page runs from disk.
"""

import csv
import hashlib
import html
import json
import math
import os
import random
import shutil
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import resources
from itertools import combinations
from pathlib import Path
from typing import Any, Final, Literal

import numpy as np

from autoclipper.analysis import AnalysisResult, Candidate

KEY_FORMAT: Final = "autoclipper-taste-key/1"
ANSWERS_FORMAT: Final = "autoclipper-taste-answers/1"
SUMMARY_FORMAT: Final = "autoclipper-taste-summary/1"
DEFAULT_CLIPS_PER_METHOD: Final = 10
SHARED_IOU: Final = 0.5
"""Two candidates with at least this temporal IoU are the same moment."""
Z_95: Final = 1.959963984540054
N_RESAMPLES: Final = 1000

type Role = Literal["ours", "baseline"]
type Choice = Literal["A", "B", "tie"]
type Renderer = Callable[[Path, float, float, Path], None]
"""Renders ``(video, start_s, end_s, output)`` into an MP4 at `output`."""

CHOICES: Final[tuple[Choice, ...]] = ("A", "B", "tie")


class TasteTestError(ValueError):
    """Inputs that can't make or score a taste test; the message says why."""


@dataclass(frozen=True, slots=True)
class MethodPair:
    """One VOD's two analysis results and the video their clips are cut from.

    Attributes:
        ours: The result of the method under test.
        baseline: The result it is compared with; same VOD.
        video: The VOD's video file (starting at VOD time 0), or None when every clip is
            already rendered in the clip cache.
        ours_path, baseline_path: Where the results came from, recorded in the key.
    """

    ours: AnalysisResult
    baseline: AnalysisResult
    video: Path | None = None
    ours_path: str = ""
    baseline_path: str = ""


@dataclass(frozen=True, slots=True)
class BuildOptions:
    """How a test is built; everything here is saved in the key.

    Attributes:
        test_id: Name of the test, shown on the page; it must not hint at the methods.
        seed: Seeds the pair order and the A/B sides.
        clips_per_method: How many of each result's top candidates to consider.
        shared_iou: Candidates of the two methods at least this close (temporal IoU) are one
            shared moment and are left out.
        layout: Export layout recorded for the renders (the renderer applies it).
        captions: Whether the renders have burned-in captions (recorded the same way).
    """

    test_id: str
    seed: int
    clips_per_method: int = DEFAULT_CLIPS_PER_METHOD
    shared_iou: float = SHARED_IOU
    layout: str = "center_crop"
    captions: bool = False


@dataclass(frozen=True, slots=True)
class BuiltTest:
    """What `build_test` wrote."""

    directory: Path
    page: Path
    key: Path
    n_pairs: int
    n_shared: int
    n_rendered: int
    n_reused: int


def iou(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Temporal intersection over union of two ``(start_s, end_s)`` intervals."""
    inter = max(0.0, min(a[1], b[1]) - max(a[0], b[0]))
    union = max(a[1], b[1]) - min(a[0], b[0])
    return inter / union if union > 0 else 0.0


def _span(candidate: Candidate) -> tuple[float, float]:
    return (candidate.start_s, candidate.end_s)


def shared_moments(
    ours: Sequence[Candidate], baseline: Sequence[Candidate], *, min_iou: float = SHARED_IOU
) -> tuple[list[Candidate], list[Candidate], list[tuple[Candidate, Candidate]]]:
    """Split two top lists into the moments only one method picked and the shared ones.

    A candidate is shared when it overlaps a candidate of the other list with IoU ≥ `min_iou`;
    each shared pair is matched greedily by highest IoU, once. Showing the same moment on both
    sides tells nothing about which method is better, so shared moments are left out.

    Returns:
        ``(ours_only, baseline_only, shared)``, the first two in their original order.
    """
    overlaps = sorted(
        (
            (iou(_span(o), _span(b)), i, j)
            for i, o in enumerate(ours)
            for j, b in enumerate(baseline)
            if iou(_span(o), _span(b)) >= min_iou
        ),
        key=lambda t: (-t[0], t[1], t[2]),
    )
    used_o: set[int] = set()
    used_b: set[int] = set()
    shared: list[tuple[Candidate, Candidate]] = []
    for _, i, j in overlaps:
        if i in used_o or j in used_b:
            continue
        used_o.add(i)
        used_b.add(j)
        shared.append((ours[i], baseline[j]))
    # A candidate overlapping a shared one of the other side also repeats that moment.
    for i, o in enumerate(ours):
        if i not in used_o and any(iou(_span(o), _span(b)) >= min_iou for b in baseline):
            used_o.add(i)
    for j, b in enumerate(baseline):
        if j not in used_b and any(iou(_span(b), _span(o)) >= min_iou for o in ours):
            used_b.add(j)
    ours_only = [c for i, c in enumerate(ours) if i not in used_o]
    baseline_only = [c for j, c in enumerate(baseline) if j not in used_b]
    return ours_only, baseline_only, shared


def clip_cache_name(video_id: str, candidate: Candidate, layout: str, captions: bool) -> str:
    """The clip cache's file name for one render: VOD, exact interval and render settings."""
    suffix = "_captions" if captions else ""
    return f"{video_id}_{candidate.start_s:.3f}-{candidate.end_s:.3f}_{layout}{suffix}.mp4"


def _clip_record(role: Role, result: AnalysisResult, candidate: Candidate) -> dict[str, Any]:
    return {
        "role": role,
        "scorer": result.scorer.name,
        "scorer_kind": result.scorer.kind,
        "run_id": result.scorer.run_id,
        "candidate_id": candidate.candidate_id,
        "rank": candidate.rank,
        "start_s": candidate.start_s,
        "end_s": candidate.end_s,
    }


def _place_media(source: Path, target: Path) -> None:
    """Hard-link `source` to `target`, or copy it where links don't work."""
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()
    try:
        os.link(source, target)
    except OSError:
        shutil.copyfile(source, target)


def page_html(test_id: str, pairs: Sequence[Mapping[str, str]]) -> str:
    """The rating page: the template with the test's pairs embedded (no method names)."""
    template = resources.files("autoclipper.eval").joinpath("taste_test_page.html")
    data = json.dumps({"format": ANSWERS_FORMAT, "test_id": test_id, "pairs": list(pairs)})
    # Inside <script>, "</" could close the tag early.
    data = data.replace("</", "<\\/")
    text = template.read_text(encoding="utf-8")
    return text.replace("__TITLE__", html.escape(test_id)).replace("__TEST_DATA__", data)


def build_test(
    inputs: Sequence[MethodPair],
    out_dir: Path,
    options: BuildOptions,
    *,
    renderer: Renderer | None,
    clip_cache: Path | None = None,
    git_sha: str = "unknown",
    now: datetime | None = None,
) -> BuiltTest:
    """Pair, render, shuffle and anonymize the clips and write the page and the key.

    Args:
        inputs: One `MethodPair` per VOD.
        out_dir: The test's folder (``<artifacts>/taste-tests/<test_id>/``); it gets
            ``key.json``, ``page/`` and (by default) the clip cache ``clips/``.
        options: See `BuildOptions`.
        renderer: Renders a clip missing from the cache; None to only reuse cached clips.
        clip_cache: Where renders are kept and reused. Default: ``out_dir/clips``.
        git_sha: Recorded in the key.
        now: The build time recorded in the key. Default: the current UTC time.

    Raises:
        TasteTestError: No inputs, two results of different VODs, no pairs left, a clip that
            isn't cached when it can't be rendered, or an existing page in `out_dir`.
    """
    if not inputs:
        raise TasteTestError("a taste test needs at least one pair of analysis results")
    if options.clips_per_method < 1:
        raise TasteTestError("clips_per_method must be at least 1")
    page_dir = out_dir / "page"
    if (out_dir / "key.json").exists() or page_dir.exists():
        raise TasteTestError(f"{out_dir} already holds a taste test; pick another test ID")
    cache = clip_cache or out_dir / "clips"
    pairs: list[dict[str, Any]] = []
    shared_records: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    for item in inputs:
        vod = item.ours.vod.video_id
        if item.baseline.vod.video_id != vod:
            raise TasteTestError(
                f"results are for different VODs ({vod} and {item.baseline.vod.video_id})"
            )
        n = options.clips_per_method
        ours_only, base_only, shared = shared_moments(
            item.ours.candidates[:n], item.baseline.candidates[:n], min_iou=options.shared_iou
        )
        shared_records.extend(
            {"video_id": vod, "ours_rank": o.rank, "baseline_rank": b.rank} for o, b in shared
        )
        sources.append(
            {
                "video_id": vod,
                "ours": item.ours_path,
                "baseline": item.baseline_path,
                "ours_scorer": item.ours.scorer.name,
                "baseline_scorer": item.baseline.scorer.name,
                "n_ours_only": len(ours_only),
                "n_baseline_only": len(base_only),
                "n_shared": len(shared),
            }
        )
        for o, b in zip(ours_only, base_only, strict=False):
            pairs.append(
                {
                    "video_id": vod,
                    "video": item.video,
                    "ours": _clip_record("ours", item.ours, o),
                    "baseline": _clip_record("baseline", item.baseline, b),
                    "_cands": (o, b),
                }
            )
    if not pairs:
        raise TasteTestError("no pairs left: the two methods picked the same moments")

    # Render or reuse every clip before writing anything a rater would see.
    n_rendered = n_reused = 0
    for pair in pairs:
        for role, cand in zip(("ours", "baseline"), pair["_cands"], strict=True):
            path = cache / clip_cache_name(pair["video_id"], cand, options.layout, options.captions)
            pair[role]["clip"] = path
            if path.is_file():
                n_reused += 1
                continue
            if renderer is None or pair["video"] is None:
                raise TasteTestError(
                    f"clip {path.name} isn't rendered and no video was given for VOD "
                    f"{pair['video_id']}"
                )
            path.parent.mkdir(parents=True, exist_ok=True)
            renderer(pair["video"], cand.start_s, cand.end_s, path)
            if not path.is_file():
                raise TasteTestError(f"rendering {path.name} produced no file")
            n_rendered += 1

    rng = random.Random(options.seed)
    rng.shuffle(pairs)
    key_pairs: list[dict[str, Any]] = []
    page_pairs: list[dict[str, str]] = []
    width = max(2, len(str(len(pairs))))
    for index, pair in enumerate(pairs, start=1):
        pair_id = f"pair-{index:0{width}d}"
        a_role, b_role = ("ours", "baseline") if rng.random() < 0.5 else ("baseline", "ours")
        media = {}
        for side, role in (("a", a_role), ("b", b_role)):
            name = f"{pair_id}-{side}.mp4"
            _place_media(pair[role]["clip"], page_dir / "media" / name)
            media[side] = f"media/{name}"
        clean = {r: {k: v for k, v in pair[r].items() if k != "clip"} for r in ("ours", "baseline")}
        for r in ("ours", "baseline"):
            clean[r]["clip_file"] = pair[r]["clip"].name
        key_pairs.append(
            {
                "pair_id": pair_id,
                "video_id": pair["video_id"],
                "A": clean[a_role],
                "B": clean[b_role],
            }
        )
        page_pairs.append({"id": pair_id, "a": media["a"], "b": media["b"]})

    page = page_dir / "index.html"
    page.write_text(page_html(options.test_id, page_pairs), encoding="utf-8")
    created = (now or datetime.now(UTC)).astimezone(UTC).isoformat().replace("+00:00", "Z")
    key = {
        "format": KEY_FORMAT,
        "test_id": options.test_id,
        "created_at": created,
        "git_sha": git_sha,
        "seed": options.seed,
        "clips_per_method": options.clips_per_method,
        "shared_iou": options.shared_iou,
        "layout": options.layout,
        "captions": options.captions,
        "sources": sources,
        "shared": shared_records,
        "pairs": key_pairs,
    }
    key_path = out_dir / "key.json"
    key_path.write_text(json.dumps(key, indent=2) + "\n", encoding="utf-8")
    return BuiltTest(
        directory=out_dir,
        page=page,
        key=key_path,
        n_pairs=len(key_pairs),
        n_shared=len(shared_records),
        n_rendered=n_rendered,
        n_reused=n_reused,
    )


# --- scoring ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Judgment:
    """One rater's answer for one pair, resolved to methods."""

    rater: str
    pair_id: str
    video_id: str
    comparison: str
    choice: Choice
    winner: Role | Literal["tie"]
    rating_ours: int | None
    rating_baseline: int | None
    note: str


def wilson_interval(successes: int, n: int, z: float = Z_95) -> tuple[float, float] | None:
    """Wilson score interval of a proportion; None when `n` is 0."""
    if n == 0:
        return None
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def sign_test_p(successes: int, n: int) -> float | None:
    """Exact two-sided binomial test of ``p = 0.5``; None when `n` is 0."""
    if n == 0:
        return None
    probs = [math.comb(n, k) / 2**n for k in range(n + 1)]
    observed = probs[successes]
    return min(1.0, sum(p for p in probs if p <= observed * (1 + 1e-9)))


def load_key(path: Path) -> dict[str, Any]:
    """Read and check a key file."""
    try:
        key = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TasteTestError(f"can't read key {path}: {exc}") from None
    if not isinstance(key, dict) or key.get("format") != KEY_FORMAT:
        raise TasteTestError(f"{path} isn't a taste test key ({KEY_FORMAT})")
    return key


def load_answers(paths: Sequence[Path], test_id: str) -> list[dict[str, Any]]:
    """Read answer files (directories mean their ``*.json``), one per rater.

    A rater who sent several files keeps only the latest by ``submitted_at``.

    Raises:
        TasteTestError: A file that isn't an answer file, belongs to another test, or no files.
    """
    files: list[Path] = []
    for path in paths:
        files.extend(sorted(path.glob("*.json")) if path.is_dir() else [path])
    by_rater: dict[str, dict[str, Any]] = {}
    for file in files:
        try:
            data = json.loads(file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise TasteTestError(f"can't read answers {file}: {exc}") from None
        if not isinstance(data, dict) or data.get("format") != ANSWERS_FORMAT:
            raise TasteTestError(f"{file} isn't a taste test answer file ({ANSWERS_FORMAT})")
        if data.get("test_id") != test_id:
            raise TasteTestError(f"{file} is for test {data.get('test_id')!r}, not {test_id!r}")
        rater = str(data.get("rater") or "").strip().casefold()
        if not rater:
            raise TasteTestError(f"{file} has no rater name")
        old = by_rater.get(rater)
        if old is None or str(data.get("submitted_at", "")) > str(old.get("submitted_at", "")):
            by_rater[rater] = data
    if not by_rater:
        raise TasteTestError("no answer files found")
    return [by_rater[r] for r in sorted(by_rater)]


def _rating(value: object) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, int | float) or value not in range(1, 6):
        raise TasteTestError(f"rating {value!r} isn't 1-5")
    return int(value)


def comparison_name(ours_scorer: str, baseline_scorer: str) -> str:
    """The label of one comparison: ``<ours scorer> vs <baseline scorer>``."""
    return f"{ours_scorer} vs {baseline_scorer}"


def resolve(key: Mapping[str, Any], answers: Sequence[Mapping[str, Any]]) -> list[Judgment]:
    """Turn the raters' A/B/tie answers into judgments about methods, using the key.

    Raters are renamed ``rater_1``, ``rater_2``... in the order of their (case-folded) names.
    """
    pairs = {p["pair_id"]: p for p in key["pairs"]}
    judgments: list[Judgment] = []
    for number, sheet in enumerate(answers, start=1):
        rater = f"rater_{number}"
        seen: set[str] = set()
        for answer in sheet.get("answers", []):
            pair_id = answer.get("pair_id")
            pair = pairs.get(pair_id)
            if pair is None:
                raise TasteTestError(f"{rater} answered unknown pair {pair_id!r}")
            if pair_id in seen:
                raise TasteTestError(f"{rater} answered {pair_id} twice")
            seen.add(pair_id)
            choice = answer.get("choice")
            if choice in (None, ""):
                continue  # skipped pair
            if choice not in CHOICES:
                raise TasteTestError(f"{rater}'s choice {choice!r} for {pair_id} isn't A/B/tie")
            a, b = pair["A"], pair["B"]
            winner = "tie" if choice == "tie" else (a if choice == "A" else b)["role"]
            ratings = {
                a["role"]: _rating(answer.get("rating_a")),
                b["role"]: _rating(answer.get("rating_b")),
            }
            ours = a if a["role"] == "ours" else b
            base = b if ours is a else a
            judgments.append(
                Judgment(
                    rater=rater,
                    pair_id=pair_id,
                    video_id=pair["video_id"],
                    comparison=comparison_name(ours["scorer"], base["scorer"]),
                    choice=choice,
                    winner=winner,
                    rating_ours=ratings["ours"],
                    rating_baseline=ratings["baseline"],
                    note=str(answer.get("note") or "").strip(),
                )
            )
    return judgments


def _round(value: float | None, digits: int = 4) -> float | None:
    return None if value is None else round(value, digits)


def _interval(bounds: tuple[float, float] | None) -> list[float | None] | None:
    return None if bounds is None else [_round(bounds[0]), _round(bounds[1])]


def _pair_bootstrap(
    by_pair: Mapping[str, Sequence[Judgment]],
    stat: Callable[[Sequence[Judgment]], float | None],
    *,
    seed: int,
    n_resamples: int = N_RESAMPLES,
) -> tuple[float, float] | None:
    """Percentile 95% interval of `stat` over judgments, resampling pairs with replacement.

    A pair's judgments by different raters aren't independent; resampling whole pairs keeps
    that in the interval.
    """
    keys = sorted(by_pair)
    if not keys:
        return None
    rng = np.random.default_rng(seed)
    values: list[float] = []
    for _ in range(n_resamples):
        picks = rng.integers(0, len(keys), size=len(keys))
        sample = [j for i in picks for j in by_pair[keys[i]]]
        value = stat(sample)
        if value is not None:
            values.append(value)
    if not values:
        return None
    low, high = np.percentile(values, [2.5, 97.5])
    return (float(low), float(high))


def _win_rate(judgments: Sequence[Judgment]) -> float | None:
    decisive = [j for j in judgments if j.winner != "tie"]
    return sum(j.winner == "ours" for j in decisive) / len(decisive) if decisive else None


def _ties_half(judgments: Sequence[Judgment]) -> float | None:
    if not judgments:
        return None
    return sum(
        1.0 if j.winner == "ours" else 0.5 if j.winner == "tie" else 0.0 for j in judgments
    ) / len(judgments)


def _mean_rating(role: Role) -> Callable[[Sequence[Judgment]], float | None]:
    def stat(judgments: Sequence[Judgment]) -> float | None:
        values = [
            r
            for j in judgments
            if (r := j.rating_ours if role == "ours" else j.rating_baseline) is not None
        ]
        return sum(values) / len(values) if values else None

    return stat


def agreement(judgments: Sequence[Judgment]) -> tuple[float | None, int]:
    """Mean pairwise percent agreement over pairs with at least two judgments.

    Returns:
        ``(agreement, n_pairs)``: the share of rater pairs giving the same answer (A, B or
        tie), averaged over pairs, and how many pairs were counted.
    """
    by_pair: dict[str, list[Choice]] = defaultdict(list)
    for j in judgments:
        by_pair[j.pair_id].append(j.choice)
    shares = []
    for choices in by_pair.values():
        if len(choices) < 2:
            continue
        both = list(combinations(choices, 2))
        shares.append(sum(x == y for x, y in both) / len(both))
    return (sum(shares) / len(shares) if shares else None, len(shares))


def summarize(judgments: Sequence[Judgment], *, seed: int) -> dict[str, Any]:
    """Per-comparison results and the checks on the raters (module docs)."""
    comparisons: dict[str, Any] = {}
    for name in sorted({j.comparison for j in judgments}):
        group = [j for j in judgments if j.comparison == name]
        by_pair: dict[str, list[Judgment]] = defaultdict(list)
        for j in group:
            by_pair[j.pair_id].append(j)
        counts = Counter(j.winner for j in group)
        wins, losses, ties = counts["ours"], counts["baseline"], counts["tie"]
        decisive = wins + losses
        ratings: dict[str, Any] = {}
        for role in ("ours", "baseline"):
            stat = _mean_rating(role)
            n_ratings = sum(
                (j.rating_ours if role == "ours" else j.rating_baseline) is not None for j in group
            )
            ratings[role] = {
                "n": n_ratings,
                "mean": _round(stat(group)),
                "ci95_pair_bootstrap": _interval(_pair_bootstrap(by_pair, stat, seed=seed)),
            }
        comparisons[name] = {
            "n_pairs": len(by_pair),
            "n_raters": len({j.rater for j in group}),
            "n_judgments": len(group),
            "ours_wins": wins,
            "baseline_wins": losses,
            "ties": ties,
            "win_rate": _round(wins / decisive if decisive else None),
            "win_rate_ci95_wilson": _interval(wilson_interval(wins, decisive)),
            "win_rate_ci95_pair_bootstrap": _interval(
                _pair_bootstrap(by_pair, _win_rate, seed=seed)
            ),
            "sign_test_p": _round(sign_test_p(wins, decisive)),
            "win_rate_ties_half": _round(_ties_half(group)),
            "mean_rating": ratings,
        }
    decisive_all = [j for j in judgments if j.choice != "tie"]
    chose_a = sum(j.choice == "A" for j in decisive_all)
    agree, n_agree = agreement(judgments)
    return {
        "n_raters": len({j.rater for j in judgments}),
        "n_judgments": len(judgments),
        "n_notes": sum(bool(j.note) for j in judgments),
        "comparisons": comparisons,
        "position_bias": {
            "share_chose_a": _round(chose_a / len(decisive_all) if decisive_all else None),
            "ci95_wilson": _interval(wilson_interval(chose_a, len(decisive_all))),
            "n_decisive": len(decisive_all),
        },
        "agreement": {"pairwise_percent": _round(agree), "n_pairs": n_agree},
    }


def _fmt(value: float | None, pct: bool = False) -> str:
    if value is None:
        return "—"
    return f"{value * 100:.0f} %" if pct else f"{value:.2f}"


def _fmt_ci(bounds: Sequence[float | None] | None, pct: bool = False) -> str:
    if bounds is None:
        return "—"
    return f"{_fmt(bounds[0], pct)} to {_fmt(bounds[1], pct)}"


def summary_markdown(summary: Mapping[str, Any]) -> str:
    """The results as Markdown tables, for the experiment README."""
    lines = [
        f"# Taste test {summary['test_id']}",
        "",
        f"{summary['n_raters']} raters, {summary['n_judgments']} judgments, seed "
        f"{summary['seed']}, scored at `{summary['git_sha']}`.",
        "",
        "| Comparison | Pairs | Judgments | Ours / baseline / tie | Win rate (95% Wilson) "
        "| Pair bootstrap | Sign test p | Ties as half |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for name, c in summary["comparisons"].items():
        lines.append(
            f"| {name} | {c['n_pairs']} | {c['n_judgments']} | {c['ours_wins']} / "
            f"{c['baseline_wins']} / {c['ties']} | {_fmt(c['win_rate'], True)} "
            f"({_fmt_ci(c['win_rate_ci95_wilson'], True)}) | "
            f"{_fmt_ci(c['win_rate_ci95_pair_bootstrap'], True)} | {_fmt(c['sign_test_p'])} | "
            f"{_fmt(c['win_rate_ties_half'], True)} |"
        )
    lines += ["", "| Comparison | Mean rating, ours (95% CI) | Mean rating, baseline (95% CI) |"]
    lines.append("|---|---|---|")
    for name, c in summary["comparisons"].items():
        cells = []
        for role in ("ours", "baseline"):
            r = c["mean_rating"][role]
            cells.append(f"{_fmt(r['mean'])} ({_fmt_ci(r['ci95_pair_bootstrap'])}, n = {r['n']})")
        lines.append(f"| {name} | {cells[0]} | {cells[1]} |")
    bias, agree = summary["position_bias"], summary["agreement"]
    lines += [
        "",
        f"Position bias: A chosen in {_fmt(bias['share_chose_a'], True)} of "
        f"{bias['n_decisive']} decisive judgments ({_fmt_ci(bias['ci95_wilson'], True)}). "
        f"Inter-rater agreement: {_fmt(agree['pairwise_percent'], True)} over "
        f"{agree['n_pairs']} pairs rated at least twice.",
        "",
    ]
    return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class ScoredTest:
    """What `score_test` wrote."""

    summary: dict[str, Any]
    summary_path: Path
    ratings_path: Path
    markdown_path: Path
    notes_path: Path | None


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def score_test(
    key_path: Path,
    answer_paths: Sequence[Path],
    out_dir: Path,
    *,
    seed: int | None = None,
    notes_path: Path | None = None,
    git_sha: str = "unknown",
) -> ScoredTest:
    """Score a test and write ``summary.json``, ``summary.md`` and ``ratings.csv`` to `out_dir`.

    ``ratings.csv`` holds every judgment without names or notes, so the README's numbers can
    be reproduced from it. Notes (with ``rater_N`` labels) go to `notes_path` only, by default
    ``notes.md`` next to the key, which stays in the git-ignored artifacts.

    Args:
        key_path: The test's ``key.json``.
        answer_paths: Answer files, or folders whose ``*.json`` are answer files.
        out_dir: Where the summary goes, normally ``experiments/<id>/``.
        seed: Seeds the bootstrap. Default: the key's seed.
        notes_path: Where rater notes go. Default: ``notes.md`` next to the key.
        git_sha: The scoring code's commit, recorded in the summary.

    Raises:
        TasteTestError: A bad key or answer file, or no answers at all.
    """
    key = load_key(key_path)
    answers = load_answers(answer_paths, key["test_id"])
    judgments = resolve(key, answers)
    if not judgments:
        raise TasteTestError("the answer files hold no answers")
    used_seed = key["seed"] if seed is None else seed
    summary = {
        "format": SUMMARY_FORMAT,
        "test_id": key["test_id"],
        "key_sha256": _sha256(key_path),
        "built_at_git_sha": key.get("git_sha"),
        "git_sha": git_sha,
        "seed": used_seed,
        "build": {
            k: key[k] for k in ("seed", "clips_per_method", "shared_iou", "layout", "captions")
        },
        "sources": key["sources"],
        "n_pairs_built": len(key["pairs"]),
        "n_shared_left_out": len(key["shared"]),
        **summarize(judgments, seed=used_seed),
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    summary_path = out_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    markdown_path = out_dir / "summary.md"
    markdown_path.write_text(summary_markdown(summary), encoding="utf-8")
    ratings_path = out_dir / "ratings.csv"
    with ratings_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(
            [
                "rater",
                "pair_id",
                "video_id",
                "comparison",
                "choice",
                "winner",
                "rating_ours",
                "rating_baseline",
            ]
        )
        for j in judgments:
            writer.writerow(
                [
                    j.rater,
                    j.pair_id,
                    j.video_id,
                    j.comparison,
                    j.choice,
                    j.winner,
                    "" if j.rating_ours is None else j.rating_ours,
                    "" if j.rating_baseline is None else j.rating_baseline,
                ]
            )
    notes = [j for j in judgments if j.note]
    target = notes_path or key_path.parent / "notes.md"
    written: Path | None = None
    if notes:
        body = ["# Rater notes (private: keep out of git)", ""]
        body += [f"- {j.rater}, {j.pair_id} ({j.winner}): {j.note}" for j in notes]
        target.write_text("\n".join(body) + "\n", encoding="utf-8")
        written = target
    return ScoredTest(summary, summary_path, ratings_path, markdown_path, written)
