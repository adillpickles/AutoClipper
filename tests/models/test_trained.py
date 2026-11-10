"""Tests for the trained scorers: logistic regression and LightGBM."""

import json
import logging
from pathlib import Path

import numpy as np
import polars as pl
import pytest

from autoclipper.models.base import ScorerError, load_scorer, make_scorer, scorer_names
from autoclipper.models.trained import (
    FEATURES_FILE,
    LightGBMScorer,
    LogisticRegressionScorer,
    balance_classes,
)

MODELS = ["logreg", "lightgbm"]


def labeled_windows(n: int = 400, seed: int = 0) -> tuple[pl.DataFrame, pl.Series]:
    """About 10% positives, loud and chatty; a third feature is pure noise."""
    rng = np.random.default_rng(seed)
    labels = rng.random(n) < 0.1
    features = pl.DataFrame(
        {
            "window_id": [f"v:{i}" for i in range(n)],
            "chat_msg_rate_z": rng.normal(0, 1, n) + 3.0 * labels,
            "audio_rms_max_z": rng.normal(0, 1, n) + 2.0 * labels,
            "noise": rng.normal(0, 1, n),
        }
    )
    return features, pl.Series("label_pos", labels)


def auc(scores: pl.Series, labels: pl.Series) -> float:
    """Probability that a random positive outscores a random negative."""
    s, y = scores.to_numpy(), labels.to_numpy()
    positives, negatives = s[y], s[~y]
    return float((positives[:, None] > negatives[None, :]).mean())


def test_balance_classes_gives_both_classes_equal_total_weight() -> None:
    y = np.array([1, 0, 0, 0, 0, 0])
    w = np.array([2.0, 1.0, 1.0, 1.0, 1.0, 1.0])
    balanced = balance_classes(y, w)
    assert balanced[y == 1].sum() == pytest.approx(balanced[y == 0].sum())
    assert balanced.mean() == pytest.approx(1.0)
    assert balanced[0] > balanced[1]  # the rare class counts for more per window


def test_balance_classes_needs_both_classes() -> None:
    with pytest.raises(ScorerError, match="both classes"):
        balance_classes(np.array([0, 0]), np.array([1.0, 1.0]))


@pytest.mark.parametrize("name", MODELS)
def test_a_trained_model_ranks_the_planted_positives_above_the_rest(name: str) -> None:
    features, labels = labeled_windows()
    scorer = make_scorer(name).fit(features, labels)
    scores = scorer.score(features)
    assert scores["window_id"].to_list() == features["window_id"].to_list()
    assert scores.schema["score"] == pl.Float64
    assert scores["score"].is_finite().all()
    assert auc(scores["score"], labels) > 0.9


def test_trained_models_are_registered_and_use_numeric_columns_only() -> None:
    assert {"logreg", "lightgbm"} <= set(scorer_names())
    features, labels = labeled_windows()
    features = features.with_columns(feature_version=pl.lit("chat-v1"))
    scorer = make_scorer("logreg").fit(features, labels)
    assert scorer.required_features == ("chat_msg_rate_z", "audio_rms_max_z", "noise")
    assert scorer.kind == "trained_model"


def test_logistic_regression_scales_with_the_training_windows_only() -> None:
    features, labels = labeled_windows()
    scorer = LogisticRegressionScorer().fit(features, labels)
    x = features.select(scorer.required_features).to_numpy()
    assert scorer._mean == pytest.approx(x.mean(axis=0))  # pyright: ignore[reportPrivateUsage]
    assert scorer._scale == pytest.approx(x.std(axis=0))  # pyright: ignore[reportPrivateUsage]
    before = scorer.score(features)["score"].to_list()
    # Scoring windows from a very different distribution doesn't refit the scaling.
    scorer.score(features.with_columns(pl.col("chat_msg_rate_z") + 100.0))
    assert scorer.score(features)["score"].to_list() == before


def test_logistic_coefficients_show_which_signals_matter() -> None:
    features, labels = labeled_windows()
    weights = LogisticRegressionScorer().fit(features, labels).importances()
    assert weights["chat_msg_rate_z"] > weights["audio_rms_max_z"] > 0
    assert abs(weights["noise"]) < weights["audio_rms_max_z"]


def test_lightgbm_importances_name_the_useful_features() -> None:
    features, labels = labeled_windows()
    gains = LightGBMScorer().fit(features, labels).importances()
    assert gains["chat_msg_rate_z"] > gains["noise"]
    assert set(gains) == {"chat_msg_rate_z", "audio_rms_max_z", "noise"}


def test_sample_weights_matter_within_a_class_but_class_balancing_cancels_between_classes() -> None:
    features, labels = labeled_windows()
    plain = LogisticRegressionScorer().fit(features, labels).score(features)["score"].to_list()
    flat = pl.Series(np.where(labels.to_numpy(), 1.0, 5.0))
    same = LogisticRegressionScorer().fit(features, labels, weights=flat)
    assert same.score(features)["score"].to_list() == pytest.approx(plain)
    # Nearly ignore half of the positives: now the model really is different.
    uneven = pl.Series(np.where(labels.to_numpy() & (np.arange(labels.len()) % 2 == 0), 0.01, 1.0))
    other = LogisticRegressionScorer().fit(features, labels, weights=uneven)
    assert other.score(features)["score"].to_list() != pytest.approx(plain)


@pytest.mark.parametrize("name", MODELS)
def test_a_saved_model_loads_and_scores_identically(tmp_path: Path, name: str) -> None:
    features, labels = labeled_windows()
    scorer = make_scorer(name).fit(features, labels)
    scorer.save(tmp_path / "run")
    assert json.loads((tmp_path / "run" / FEATURES_FILE).read_text(encoding="utf-8")) == list(
        scorer.required_features
    )
    loaded = load_scorer(tmp_path / "run")
    assert type(loaded) is type(scorer)
    assert loaded.required_features == scorer.required_features
    assert loaded.params() == scorer.params()
    assert loaded.score(features)["score"].to_list() == pytest.approx(
        scorer.score(features)["score"].to_list()
    )


def test_lightgbm_training_is_deterministic_for_a_seed() -> None:
    features, labels = labeled_windows()
    one = LightGBMScorer(seed=3).fit(features, labels).score(features)["score"].to_list()
    two = LightGBMScorer(seed=3).fit(features, labels).score(features)["score"].to_list()
    assert one == two


@pytest.mark.parametrize("name", MODELS)
def test_missing_values_are_tolerated(name: str) -> None:
    features, labels = labeled_windows()
    holes = features.with_columns(
        pl.when(pl.int_range(pl.len()) % 7 == 0)
        .then(None)
        .otherwise(pl.col("audio_rms_max_z"))
        .alias("audio_rms_max_z")
    )
    scorer = make_scorer(name).fit(holes, labels)
    assert scorer.score(holes)["score"].is_finite().all()


@pytest.mark.parametrize("name", MODELS)
def test_bad_inputs_are_scorer_errors(name: str) -> None:
    features, labels = labeled_windows(100)
    scorer = make_scorer(name)
    with pytest.raises(ScorerError, match="isn't trained"):
        scorer.score(features)
    with pytest.raises(ScorerError, match="same number of rows"):
        scorer.fit(features, labels.head(10))
    with pytest.raises(ScorerError, match="both classes"):
        scorer.fit(features, pl.Series([False] * features.height))
    with pytest.raises(ScorerError, match="infinite"):
        scorer.fit(features.with_columns(pl.lit(float("inf")).alias("noise")), labels)
    with pytest.raises(ScorerError, match="numeric feature"):
        scorer.fit(features.select("window_id"), labels)
    trained = scorer.fit(features, labels)
    with pytest.raises(ScorerError, match="audio_rms_max_z"):
        trained.score(features.drop("audio_rms_max_z"))


@pytest.mark.parametrize(
    ("name", "params"),
    [
        ("logreg", {"c": 0}),
        ("logreg", {"max_iter": 0}),
        ("lightgbm", {"n_estimators": 0}),
        ("lightgbm", {"subsample": 1.5}),
        ("lightgbm", {"learning_rate": "fast"}),
        ("lightgbm", {"seed": -1}),
        ("lightgbm", {"depth": 3}),
    ],
)
def test_bad_parameters_are_scorer_errors(name: str, params: dict[str, object]) -> None:
    with pytest.raises(ScorerError, match="bad parameters"):
        make_scorer(name, **params)


def test_logistic_regression_warns_when_it_does_not_converge(
    caplog: pytest.LogCaptureFixture,
) -> None:
    features, labels = labeled_windows()
    with caplog.at_level(logging.WARNING):
        LogisticRegressionScorer(max_iter=1).fit(features, labels)
    assert "did not converge" in caplog.text
