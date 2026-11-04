"""The four baselines every model must beat (docs/ml/evaluation.md § 2).

| ID | Window score |
|---|---|
| ``random`` | Uniform random in [0, 1), seeded (reports average 100 seeds) |
| ``chat_spike`` | ``chat_msg_rate_z`` |
| ``loudness`` | ``audio_rms_max_z`` |
| ``chat_loudness`` | ``chat_msg_rate_z + audio_rms_max_z`` |

A missing value of a required feature counts as 0, the VOD's average on the z-score scale, and
the number of such windows is logged, so an unavailable signal is recorded rather than silently
changing the ranking. Audio of a window that overlaps a Twitch muted segment
(``audio_is_muted == 1``) is unreliable (features.md), so it counts as missing too: a long mute
would otherwise read as the quietest stretch of the VOD and bury a chat spike inside it.

``random`` is reported as the mean over `RANDOM_SEEDS` (evaluation.md § 2):
`random_scorers` gives one scorer per seed, and `autoclipper.eval.harness.evaluate_mean`
averages their per-VOD metrics.
"""

import hashlib
import logging
from collections.abc import Iterable
from typing import ClassVar

import numpy as np
import polars as pl

from autoclipper.models.base import Scorer, ScorerError, ScorerKind, register_scorer

log = logging.getLogger(__name__)


class _FeatureSum(Scorer):
    """Score = the sum of some standardized feature columns."""

    kind: ClassVar[ScorerKind] = "heuristic"

    def score(self, features: pl.DataFrame) -> pl.DataFrame:
        """The sum of `required_features`, missing or muted values counted as 0."""
        self._require(features)
        muted = (
            pl.col("audio_is_muted").fill_null(0.0) == 1.0
            if "audio_is_muted" in features.columns
            else pl.lit(False)
        )

        def value(column: str) -> pl.Expr:
            x = pl.col(column).cast(pl.Float64).fill_nan(None)
            if column.startswith("audio_"):
                x = pl.when(muted).then(None).otherwise(x)
            return x

        columns = self.required_features
        values = features.select("window_id", *(value(c).alias(c) for c in columns))
        n_missing = values.select(
            pl.any_horizontal(pl.col(c).is_null() for c in columns)
        ).to_series()
        if int(n_missing.sum()):
            log.warning(
                "scorer=%s windows with a missing or muted signal counted as 0 n=%d of=%d",
                self.name,
                int(n_missing.sum()),
                features.height,
            )
        scores = values.select(
            "window_id", score=pl.sum_horizontal(pl.col(c).fill_null(0.0) for c in columns)
        )
        if not scores["score"].is_finite().all():
            raise ScorerError(f"{self.name}: a feature value is infinite")
        return scores


@register_scorer
class ChatSpike(_FeatureSum):
    """``chat_msg_rate_z``: the biggest chat spikes."""

    name: ClassVar[str] = "chat_spike"
    required_features = ("chat_msg_rate_z",)


@register_scorer
class Loudness(_FeatureSum):
    """``audio_rms_max_z``: the loudest moments."""

    name: ClassVar[str] = "loudness"
    required_features = ("audio_rms_max_z",)


@register_scorer
class ChatLoudness(_FeatureSum):
    """``chat_msg_rate_z + audio_rms_max_z``: the strong unlearned heuristic."""

    name: ClassVar[str] = "chat_loudness"
    required_features = ("chat_msg_rate_z", "audio_rms_max_z")


@register_scorer
class RandomScorer(Scorer):
    """Uniform random scores in [0, 1).

    A window's score depends only on the seed and its ``window_id`` (hashed), not on row order
    or which other windows are scored, so the same seed always gives the same ranking.
    """

    name: ClassVar[str] = "random"
    kind: ClassVar[ScorerKind] = "heuristic"

    def __init__(self, seed: int = 0) -> None:
        if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
            raise ValueError(f"seed must be a non-negative integer, got {seed!r}")
        self.seed = seed

    def params(self) -> dict[str, object]:
        """The seed."""
        return {"seed": self.seed}

    def score(self, features: pl.DataFrame) -> pl.DataFrame:
        """A hash of the seed and each ``window_id``, scaled to [0, 1)."""
        self._require(features)
        ids = features["window_id"].to_list()
        values = np.array(
            [
                int.from_bytes(
                    hashlib.blake2b(f"{self.seed}:{i}".encode(), digest_size=8).digest(), "big"
                )
                / 2**64
                for i in ids
            ],
            dtype=np.float64,
        )
        return pl.DataFrame(
            {"window_id": ids, "score": values},
            schema={"window_id": pl.String, "score": pl.Float64},
        )


BASELINES: tuple[str, ...] = ("random", "chat_spike", "loudness", "chat_loudness")
"""The baseline IDs, in the order reports list them."""

RANDOM_SEEDS: range = range(100)
"""The seeds the random baseline is averaged over (evaluation.md § 2)."""


def random_scorers(seeds: Iterable[int] = RANDOM_SEEDS) -> list[RandomScorer]:
    """One random scorer per seed, for reporting the random baseline's mean."""
    return [RandomScorer(seed) for seed in seeds]
