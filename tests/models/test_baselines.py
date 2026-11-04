"""Tests for the scorer interface and the four baselines (docs/ml/evaluation.md § 2)."""

import json
import logging
from pathlib import Path

import polars as pl
import pytest

from autoclipper.eval.harness import evaluate, evaluate_mean
from autoclipper.features.audio import compute_audio_features
from autoclipper.features.chat import compute_chat_features, load_lexicon
from autoclipper.labels.moments import merge_clips_into_moments
from autoclipper.labels.windows import windows_for_vods
from autoclipper.models.base import (
    Scorer,
    ScorerError,
    load_scorer,
    make_scorer,
    scorer_names,
)
from autoclipper.models.baselines import BASELINES, RandomScorer, random_scorers
from autoclipper.models.candidates import candidates_frame, generate_candidates
from autoclipper.synthetic import generate_dataset

LEXICON = Path(__file__).parents[2] / "configs" / "lexicon.yaml"


def planted_features(n: int = 40) -> pl.DataFrame:
    """Chat spikes at window 5, a loud burst at window 12, and both, milder, at window 30."""
    chat = [0.0] * n
    audio = [0.0] * n
    chat[5], audio[12] = 3.0, 3.0
    chat[30], audio[30] = 2.0, 2.0
    return pl.DataFrame(
        {
            "window_id": [f"v:{i}" for i in range(n)],
            "chat_msg_rate_z": chat,
            "audio_rms_max_z": audio,
        }
    )


def top_window(name: str, features: pl.DataFrame) -> str:
    scores = make_scorer(name).score(features)
    return scores.sort("score", descending=True)["window_id"][0]


@pytest.mark.parametrize(
    ("name", "expected"),
    [("chat_spike", "v:5"), ("loudness", "v:12"), ("chat_loudness", "v:30")],
)
def test_each_heuristic_ranks_its_planted_peak_first(name: str, expected: str) -> None:
    assert top_window(name, planted_features()) == expected


def test_scores_keep_the_row_order_and_are_floats() -> None:
    features = planted_features().reverse()

    scores = make_scorer("chat_loudness").score(features)

    assert scores["window_id"].to_list() == features["window_id"].to_list()
    assert scores.schema["score"] == pl.Float64


def test_random_scores_are_seeded_and_independent_of_row_order() -> None:
    features = planted_features()

    first = RandomScorer(seed=1).score(features)
    again = RandomScorer(seed=1).score(features.reverse()).reverse()
    other = RandomScorer(seed=2).score(features)

    assert first.equals(again)
    assert not first["score"].equals(other["score"])
    assert ((first["score"] >= 0.0) & (first["score"] < 1.0)).all()


def test_missing_values_count_as_zero_and_are_logged(caplog: pytest.LogCaptureFixture) -> None:
    features = planted_features().with_columns(
        audio_rms_max_z=pl.when(pl.col("window_id") == "v:30")
        .then(None)
        .otherwise(pl.col("audio_rms_max_z"))
    )

    with caplog.at_level(logging.WARNING, logger="autoclipper.models.baselines"):
        scores = make_scorer("chat_loudness").score(features)

    assert scores.filter(pl.col("window_id") == "v:30")["score"][0] == 2.0
    assert "missing or muted signal" in caplog.text


def test_a_missing_feature_column_is_a_scorer_error() -> None:
    with pytest.raises(ScorerError, match="audio_rms_max_z"):
        make_scorer("loudness").score(planted_features().drop("audio_rms_max_z"))


def test_every_baseline_is_registered() -> None:
    assert set(BASELINES) <= set(scorer_names())
    with pytest.raises(ScorerError, match="unknown scorer"):
        make_scorer("nope")
    with pytest.raises(ScorerError, match="bad parameters"):
        make_scorer("chat_spike", seed=1)


@pytest.mark.parametrize("name", BASELINES)
def test_scorers_save_and_load(tmp_path: Path, name: str) -> None:
    scorer = make_scorer(name, seed=7) if name == "random" else make_scorer(name)

    path = scorer.save(tmp_path / name)
    loaded = load_scorer(tmp_path / name)

    assert json.loads(path.read_text(encoding="utf-8"))["name"] == name
    assert loaded.name == name
    assert loaded.score(planted_features()).equals(scorer.score(planted_features()))


def test_loading_a_broken_or_unknown_scorer_fails_clearly(tmp_path: Path) -> None:
    with pytest.raises(ScorerError, match="can't read"):
        load_scorer(tmp_path)
    (tmp_path / "scorer.json").write_text('{"name": "nope"}', encoding="utf-8")
    with pytest.raises(ScorerError, match="unknown scorer"):
        load_scorer(tmp_path)


def test_all_four_baselines_run_through_the_evaluation_on_the_synthetic_dataset(
    tmp_path: Path,
) -> None:
    dataset = generate_dataset(tmp_path / "synthetic", seed=1)
    root = dataset.out_dir
    vods = pl.read_parquet(root / "processed" / "vods.parquet")
    clips = pl.read_parquet(root / "processed" / "clips" / "synthetic-seed-1.parquet")
    moments = merge_clips_into_moments(clips).moments
    lexicon = load_lexicon(LEXICON)
    per_vod: list[tuple[str, float, pl.DataFrame, pl.DataFrame]] = []
    for vod in vods.iter_rows(named=True):
        windows = windows_for_vods(vods.filter(pl.col("video_id") == vod["video_id"]))
        chat = compute_chat_features(
            windows,
            pl.read_parquet(root / "interim" / "chat" / f"{vod['video_id']}.parquet"),
            lexicon=lexicon,
        )
        audio = compute_audio_features(
            windows, root / "interim" / "audio" / f"{vod['video_id']}.wav"
        )
        features = chat.drop("feature_version").join(audio.drop("feature_version"), on="window_id")
        per_vod.append((vod["video_id"], vod["duration_s"], windows, features))

    def run(scorer: Scorer) -> pl.DataFrame:
        frames = []
        for video_id, duration_s, windows, features in per_vod:
            scores = windows.select("window_id", "start_s", "end_s").join(
                scorer.score(features), on="window_id"
            )
            candidates = generate_candidates(scores, duration_s=duration_s, features=features)
            frames.append(candidates_frame(video_id, candidates))
        return pl.concat(frames)

    hit_rates: dict[str, float] = {}
    for name in BASELINES:
        if name == "random":  # reported as the mean over 100 seeds (evaluation.md § 2)
            result = evaluate_mean([run(s) for s in random_scorers()], moments, clips, seed=0)
        else:
            result = evaluate(run(make_scorer(name)), moments, clips, seed=0)
        hit = result.summary["hit_rate_at_10"]
        assert hit is not None, name
        hit_rates[name] = hit.mean

    # The planted highlights are chat bursts and loud bursts, so every signal baseline finds
    # the clipped ones; random has no reason to.
    for name in ("chat_spike", "loudness", "chat_loudness"):
        assert hit_rates[name] > hit_rates["random"], hit_rates
    assert hit_rates["chat_loudness"] > 0.5


def test_muted_audio_counts_as_missing_not_as_quiet(caplog: pytest.LogCaptureFixture) -> None:
    # Muted windows read as the quietest of the VOD; their audio must not drag a chat spike down.
    features = planted_features().with_columns(
        audio_is_muted=pl.Series([1.0 if i == 5 else 0.0 for i in range(40)]),
        audio_rms_max_z=pl.Series([-3.0 if i == 5 else 0.0 for i in range(40)]),
    )

    with caplog.at_level(logging.WARNING, logger="autoclipper.models.baselines"):
        scores = make_scorer("chat_loudness").score(features)

    assert scores.filter(pl.col("window_id") == "v:5")["score"][0] == 3.0
    assert "muted" in caplog.text
    # Chat-only scoring doesn't look at audio at all.
    chat = make_scorer("chat_spike").score(features)
    assert chat.filter(pl.col("window_id") == "v:5")["score"][0] == 3.0


@pytest.mark.parametrize("value", [float("nan"), None])
def test_nan_and_null_values_count_as_zero(value: float | None) -> None:
    features = planted_features().with_columns(
        chat_msg_rate_z=pl.when(pl.col("window_id") == "v:30")
        .then(value)
        .otherwise(pl.col("chat_msg_rate_z"))
    )

    scores = make_scorer("chat_loudness").score(features)

    assert scores.filter(pl.col("window_id") == "v:30")["score"][0] == 2.0


def test_infinite_feature_values_are_a_scorer_error() -> None:
    features = planted_features().with_columns(chat_msg_rate_z=pl.lit(float("inf")))

    with pytest.raises(ScorerError, match="infinite"):
        make_scorer("chat_spike").score(features)


@pytest.mark.parametrize("seed", [-1, 1.5, True])
def test_random_seed_must_be_a_non_negative_integer(seed: object) -> None:
    with pytest.raises(ScorerError, match="seed must be a non-negative integer"):
        make_scorer("random", seed=seed)


def test_a_saved_scorer_with_bad_params_or_kind_fails_with_a_scorer_error(tmp_path: Path) -> None:
    (tmp_path / "scorer.json").write_text(
        '{"name": "random", "params": {"seed": -1}}', encoding="utf-8"
    )
    with pytest.raises(ScorerError, match="seed"):
        load_scorer(tmp_path)
    (tmp_path / "scorer.json").write_text(
        '{"name": "chat_spike", "kind": "trained_model"}', encoding="utf-8"
    )
    with pytest.raises(ScorerError, match="kind"):
        load_scorer(tmp_path)


def test_scorers_register_on_any_import_of_the_package() -> None:
    import subprocess
    import sys

    code = "from autoclipper.models.base import make_scorer; print(make_scorer('chat_spike').name)"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)

    assert out.stdout.strip() == "chat_spike"


def test_a_trained_model_must_implement_fit_and_can_set_its_features() -> None:
    class Untrained(Scorer):
        name = "untrained_test_model"
        kind = "trained_model"

        def score(self, features: pl.DataFrame) -> pl.DataFrame:
            return features.select("window_id", score=pl.lit(0.0))

    model = Untrained()
    model.required_features = ("chat_msg_rate_z",)  # decided per instance, e.g. at fit time
    with pytest.raises(NotImplementedError, match="must implement fit"):
        model.fit(planted_features(), pl.Series([0] * 40))
    assert (
        make_scorer("chat_spike").fit(planted_features(), pl.Series([0] * 40)).name == "chat_spike"
    )


def test_evaluate_mean_averages_per_vod_over_runs() -> None:
    moments = pl.DataFrame(
        {
            "video_id": ["a"],
            "moment_id": ["a:m0"],
            "start_s": [0.0],
            "end_s": [30.0],
            "clip_ids": [["x"]],
        }
    )
    clips = pl.DataFrame({"clip_id": ["x"], "video_id": ["a"], "view_count": [5]})

    def run(start: float) -> pl.DataFrame:
        return pl.DataFrame(
            {
                "video_id": ["a"],
                "candidate_id": ["a:c1"],
                "start_s": [start],
                "end_s": [start + 30],
                "score": [1.0],
            }
        )

    result = evaluate_mean([run(0.0), run(500.0)], moments, clips, seed=0)

    hit = result.summary["hit_rate_at_10"]
    assert hit is not None
    assert hit.mean == 0.5  # one run hits, one misses
