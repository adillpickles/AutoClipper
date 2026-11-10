"""Trained window scorers: logistic regression and LightGBM (docs/ml/modeling-plan.md § 2).

Both learn from the labeled windows of a dataset and plug into the shared `Scorer` interface, so
candidate generation, evaluation and the app treat them like any baseline.

- The feature columns are every numeric column given to `fit`, in that order. They're saved with
  the model and `score` requires exactly those.
- Class imbalance: positives are rare, so `balance_classes` rescales the sample weights until the
  positive and negative windows carry the same total weight (modeling-plan.md § 4).
- ``logreg`` standardizes each feature with the mean and standard deviation of the *training*
  windows only; they're saved with the coefficients, so scoring never refits anything. A missing
  value counts as the training mean. LightGBM handles missing values natively.
- Scores are raw margins (log-odds): finite, higher = more clip-worthy, and unbounded like the
  z-score sums the baselines produce, which is what candidate generation expects.

scikit-learn and LightGBM are imported inside the methods that need them: importing
`autoclipper.models` (and so the API) stays light.
"""

import json
import logging
import math
import warnings
from abc import abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Final, Self

import numpy as np
import polars as pl
from numpy.typing import NDArray

from autoclipper.models.base import Scorer, ScorerError, ScorerKind, register_scorer

if TYPE_CHECKING:
    import lightgbm as lgb

log = logging.getLogger(__name__)

FEATURES_FILE: Final = "features.json"
LOGREG_FILE: Final = "weights.json"
LIGHTGBM_FILE: Final = "model.txt"

type Matrix = NDArray[np.float64]


def balance_classes(labels: NDArray[np.int_], weights: NDArray[np.float64]) -> NDArray[np.float64]:
    """Reweight so positives and negatives carry equal total weight; the mean weight stays 1.

    Raises:
        ScorerError: One class has no weight, so there's nothing to balance.
    """
    positive = float(weights[labels == 1].sum())
    negative = float(weights[labels == 0].sum())
    if positive <= 0 or negative <= 0:
        raise ScorerError("training needs weighted windows of both classes")
    balanced = np.where(labels == 1, weights * (negative / positive), weights)
    return balanced * (len(balanced) / float(balanced.sum()))


def _number(value: object, name: str, *, low: float, high: float = math.inf) -> float:
    ok = isinstance(value, int | float) and not isinstance(value, bool)
    if not ok or not low <= float(value) <= high:  # pyright: ignore[reportArgumentType]
        raise ValueError(f"{name} must be a number between {low} and {high}, got {value!r}")
    return float(value)  # pyright: ignore[reportArgumentType]


def _whole(value: object, name: str, *, low: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < low:
        raise ValueError(f"{name} must be an integer of at least {low}, got {value!r}")
    return value


class TrainedScorer(Scorer):
    """Shared fit/score plumbing: validation, the feature matrix and the saved feature list."""

    kind: ClassVar[ScorerKind] = "trained_model"

    def fit(
        self,
        features: pl.DataFrame,
        labels: pl.Series,
        *,
        weights: pl.Series | None = None,
    ) -> Self:
        """Train on `features`' numeric columns (``window_id`` aside).

        Args:
            features: One row per training window.
            labels: Boolean (or 0/1) labels, row-aligned with `features`.
            weights: Optional non-negative training weights (default 1), before class balancing.

        Raises:
            ScorerError: Mismatched lengths, no numeric features, an infinite value, a null
                label or weight, or a class with no weight.
        """
        n = features.height
        if labels.len() != n or (weights is not None and weights.len() != n):
            raise ScorerError("features, labels and weights must have the same number of rows")
        if labels.null_count() or (weights is not None and weights.null_count()):
            raise ScorerError("labels and weights must not be null")
        columns = tuple(
            name
            for name, dtype in features.schema.items()
            if name != "window_id" and dtype.is_numeric()
        )
        if not columns:
            raise ScorerError(f"{self.name} needs at least one numeric feature column")
        y = labels.cast(pl.Int64).to_numpy()
        if not set(np.unique(y)) <= {0, 1}:
            raise ScorerError("labels must be boolean or 0/1")
        w = np.ones(n) if weights is None else weights.cast(pl.Float64).to_numpy()
        if not np.isfinite(w).all() or (w < 0).any():
            raise ScorerError("weights must be finite and non-negative")
        x = self._matrix(features, columns)
        self._fit(x, y, balance_classes(y, w), columns)
        self.required_features = columns
        return self

    def score(self, features: pl.DataFrame) -> pl.DataFrame:
        """The model's log-odds for every row, in row order."""
        if not self.required_features:
            raise ScorerError(f"{self.name} isn't trained yet")
        self._require(features)
        margins = self._margins(self._matrix(features, self.required_features))
        if not np.isfinite(margins).all():
            raise ScorerError(f"{self.name}: a score isn't finite")
        return pl.DataFrame(
            {"window_id": features["window_id"], "score": margins},
            schema={"window_id": pl.String, "score": pl.Float64},
        )

    def importances(self) -> dict[str, float]:
        """Each feature's weight in the model, for the run record: the largest matter most."""
        return dict(zip(self.required_features, self._importances(), strict=True))

    def save(self, directory: Path) -> Path:
        """Write ``scorer.json``, the feature list and the model's own file."""
        path = super().save(directory)
        (directory / FEATURES_FILE).write_text(
            json.dumps(list(self.required_features), indent=2) + "\n", encoding="utf-8"
        )
        self._save_model(directory)
        return path

    @classmethod
    def load(cls, directory: Path, params: dict[str, object]) -> Self:
        """Rebuild a saved model, ready to score."""
        scorer = cls(**params)
        columns: object = json.loads((directory / FEATURES_FILE).read_text(encoding="utf-8"))
        if not isinstance(columns, list) or not all(isinstance(c, str) for c in columns):
            raise ValueError(f"{FEATURES_FILE} must list feature names")
        scorer._load_model(directory, tuple(columns))
        scorer.required_features = tuple(columns)
        return scorer

    @staticmethod
    def _matrix(features: pl.DataFrame, columns: tuple[str, ...]) -> Matrix:
        """The feature columns as a float matrix; nulls become NaN, infinities are an error."""
        try:
            x = features.select(pl.col(c).cast(pl.Float64) for c in columns).to_numpy()
        except pl.exceptions.PolarsError as exc:
            raise ScorerError(f"feature columns must be numeric: {exc}") from exc
        if np.isinf(x).any():
            raise ScorerError("a feature value is infinite")
        return np.asarray(x, dtype=np.float64)

    @abstractmethod
    def _fit(
        self, x: Matrix, y: NDArray[np.int_], w: NDArray[np.float64], columns: tuple[str, ...]
    ) -> None:
        """Train on a validated matrix with class-balanced weights."""

    @abstractmethod
    def _margins(self, x: Matrix) -> Matrix:
        """Raw scores for a feature matrix."""

    @abstractmethod
    def _importances(self) -> list[float]:
        """One weight per feature, in column order."""

    @abstractmethod
    def _save_model(self, directory: Path) -> None:
        """Write the fitted model next to ``scorer.json``."""

    @abstractmethod
    def _load_model(self, directory: Path, columns: tuple[str, ...]) -> None:
        """Read what `_save_model` wrote."""


@register_scorer
class LogisticRegressionScorer(TrainedScorer):
    """L2-regularized logistic regression on standardized features.

    Attributes:
        c: Inverse regularization strength (scikit-learn's ``C``).
        max_iter: Optimizer iteration cap.
    """

    name: ClassVar[str] = "logreg"

    def __init__(self, c: float = 1.0, max_iter: int = 1000) -> None:
        self.c = _number(c, "c", low=1e-9)
        self.max_iter = _whole(max_iter, "max_iter", low=1)
        self._mean: Matrix = np.zeros(0)
        self._scale: Matrix = np.ones(0)
        self._coef: Matrix = np.zeros(0)
        self._intercept = 0.0

    def params(self) -> dict[str, object]:
        """The constructor arguments."""
        return {"c": self.c, "max_iter": self.max_iter}

    def _fit(
        self, x: Matrix, y: NDArray[np.int_], w: NDArray[np.float64], columns: tuple[str, ...]
    ) -> None:
        from sklearn.exceptions import ConvergenceWarning
        from sklearn.linear_model import LogisticRegression

        # Scaling comes from the training windows only; all-missing columns count as 0.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            mean = np.nan_to_num(np.nanmean(x, axis=0))
            std = np.nan_to_num(np.nanstd(x, axis=0))
        self._mean = mean
        self._scale = np.where(std > 1e-12, std, 1.0)
        model = LogisticRegression(C=self.c, max_iter=self.max_iter)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            model.fit(self._standardize(x), y, sample_weight=w)
        if any(issubclass(c.category, ConvergenceWarning) for c in caught):
            log.warning("logistic regression did not converge max_iter=%d", self.max_iter)
        self._coef = np.asarray(model.coef_, dtype=np.float64).ravel()
        self._intercept = float(np.ravel(model.intercept_)[0])

    def _standardize(self, x: Matrix) -> Matrix:
        z = (x - self._mean) / self._scale
        return np.nan_to_num(z, nan=0.0)  # a missing value is the training mean

    def _margins(self, x: Matrix) -> Matrix:
        return self._standardize(x) @ self._coef + self._intercept

    def _importances(self) -> list[float]:
        """The coefficients on standardized features: sign shows direction, size importance."""
        return [float(c) for c in self._coef]

    def _save_model(self, directory: Path) -> None:
        state = {
            "mean": self._mean.tolist(),
            "scale": self._scale.tolist(),
            "coef": self._coef.tolist(),
            "intercept": self._intercept,
        }
        (directory / LOGREG_FILE).write_text(
            json.dumps(state, indent=2, allow_nan=False) + "\n", encoding="utf-8"
        )

    def _load_model(self, directory: Path, columns: tuple[str, ...]) -> None:
        state = json.loads((directory / LOGREG_FILE).read_text(encoding="utf-8"))
        arrays = [np.asarray(state[k], dtype=np.float64) for k in ("mean", "scale", "coef")]
        if any(a.shape != (len(columns),) for a in arrays):
            raise ValueError(f"{LOGREG_FILE} doesn't match the feature list")
        self._mean, self._scale, self._coef = arrays
        self._intercept = float(state["intercept"])


@register_scorer
class LightGBMScorer(TrainedScorer):
    """Gradient-boosted trees (``LGBMClassifier``) on the window features.

    Training is deterministic for a seed: one thread, deterministic histogram building.

    Attributes:
        n_estimators: Boosting rounds.
        learning_rate: Shrinkage per round.
        num_leaves: Leaves per tree.
        min_child_samples: Minimum windows in a leaf.
        subsample: Fraction of windows each tree sees (bagged every round).
        colsample_bytree: Fraction of features each tree sees.
        reg_lambda: L2 regularization.
        seed: Seed for the bagging and feature sampling.
    """

    name: ClassVar[str] = "lightgbm"

    def __init__(
        self,
        n_estimators: int = 200,
        learning_rate: float = 0.05,
        num_leaves: int = 15,
        min_child_samples: int = 20,
        subsample: float = 0.8,
        colsample_bytree: float = 0.8,
        reg_lambda: float = 1.0,
        seed: int = 0,
    ) -> None:
        self.n_estimators = _whole(n_estimators, "n_estimators", low=1)
        self.learning_rate = _number(learning_rate, "learning_rate", low=1e-9)
        self.num_leaves = _whole(num_leaves, "num_leaves", low=2)
        self.min_child_samples = _whole(min_child_samples, "min_child_samples", low=1)
        self.subsample = _number(subsample, "subsample", low=1e-9, high=1.0)
        self.colsample_bytree = _number(colsample_bytree, "colsample_bytree", low=1e-9, high=1.0)
        self.reg_lambda = _number(reg_lambda, "reg_lambda", low=0.0)
        self.seed = _whole(seed, "seed", low=0)
        self._booster: object | None = None

    def params(self) -> dict[str, object]:
        """The constructor arguments."""
        return {
            "n_estimators": self.n_estimators,
            "learning_rate": self.learning_rate,
            "num_leaves": self.num_leaves,
            "min_child_samples": self.min_child_samples,
            "subsample": self.subsample,
            "colsample_bytree": self.colsample_bytree,
            "reg_lambda": self.reg_lambda,
            "seed": self.seed,
        }

    def _fit(
        self, x: Matrix, y: NDArray[np.int_], w: NDArray[np.float64], columns: tuple[str, ...]
    ) -> None:
        import lightgbm as lgb

        model = lgb.LGBMClassifier(
            n_estimators=self.n_estimators,
            learning_rate=self.learning_rate,
            num_leaves=self.num_leaves,
            min_child_samples=self.min_child_samples,
            subsample=self.subsample,
            subsample_freq=1,
            colsample_bytree=self.colsample_bytree,
            reg_lambda=self.reg_lambda,
            random_state=self.seed,
            deterministic=True,
            force_row_wise=True,
            n_jobs=1,
            verbose=-1,
        )
        model.fit(x, y, sample_weight=w)
        self._booster = model.booster_

    def _model(self) -> "lgb.Booster":
        if self._booster is None:
            raise ScorerError(f"{self.name} isn't trained yet")
        return self._booster  # pyright: ignore[reportReturnType]

    def _margins(self, x: Matrix) -> Matrix:
        return np.asarray(self._model().predict(x, raw_score=True), dtype=np.float64)

    def _importances(self) -> list[float]:
        """Total split gain per feature."""
        gains = self._model().feature_importance(importance_type="gain")
        return [float(g) for g in gains]

    def _save_model(self, directory: Path) -> None:
        self._model().save_model(str(directory / LIGHTGBM_FILE))

    def _load_model(self, directory: Path, columns: tuple[str, ...]) -> None:
        import lightgbm as lgb

        booster = lgb.Booster(model_file=str(directory / LIGHTGBM_FILE))
        if booster.num_feature() != len(columns):
            raise ValueError(f"{LIGHTGBM_FILE} doesn't match the feature list")
        self._booster = booster
