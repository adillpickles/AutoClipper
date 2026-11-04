"""Tests for the scorer interface and the four baselines (docs/ml/evaluation.md § 2)."""

import json
import logging
from pathlib import Path

import polars as pl
import pytest

from autoclipper.eval.harness import evaluate
from autoclipper.features.audio import compute_audio_features
from autoclipper.features.chat import compute_chat_features, load_lexicon
from autoclipper.labels.moments import merge_clips_into_moments
from autoclipper.labels.windows import windows_for_vods
from autoclipper.models.base import (
    ScorerError,
    load_scorer,
    make_scorer,
    scorer_names,
)
from autoclipper.models.baselines import BASELINES, RandomScorer
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
    assert "missing signal" in caplog.text


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

    hit_rates: dict[str, float] = {}
    for name in BASELINES:
        frames = []
        for video_id, duration_s, windows, features in per_vod:
            scores = windows.select("window_id", "start_s", "end_s").join(
                make_scorer(name).score(features), on="window_id"
            )
            candidates = generate_candidates(scores, duration_s=duration_s, features=features)
            frames.append(candidates_frame(video_id, candidates))
        result = evaluate(pl.concat(frames), moments, clips, seed=0)
        hit = result.summary["hit_rate_at_10"]
        assert hit is not None, name
        hit_rates[name] = hit.mean

    # The planted highlights are chat bursts and loud bursts, so every signal baseline finds
    # the clipped ones; random has no reason to.
    for name in ("chat_spike", "loudness", "chat_loudness"):
        assert hit_rates[name] > hit_rates["random"], hit_rates
    assert hit_rates["chat_loudness"] > 0.5
