"""The four baselines every model must beat (docs/ml/evaluation.md § 2).

| ID | Window score |
|---|---|
| ``random`` | Uniform random in [0, 1), seeded (reports average 100 seeds) |
| ``chat_spike`` | ``chat_msg_rate_z`` |
| ``loudness`` | ``audio_rms_max_z`` |
| ``chat_loudness`` | ``chat_msg_rate_z + audio_rms_max_z`` |

A missing value of a required feature (e.g. no audio for that window) counts as 0, the VOD's
average on the z-score scale, and the number of such windows is logged, so an unavailable
signal is recorded rather than silently changing the ranking.
"""

import hashlib
import logging
from typing import ClassVar

import numpy as np
import polars as pl

from autoclipper.models.base import Scorer, ScorerKind, register_scorer

log = logging.getLogger(__name__)


class _FeatureSum(Scorer):
    """Score = the sum of some standardized feature columns."""

    kind: ClassVar[ScorerKind] = "heuristic"

    def score(self, features: pl.DataFrame) -> pl.DataFrame:
        """The sum of `required_features`, missing values counted as 0."""
        self._require(features)
        columns = self.required_features
        missing = features.select(
            pl.any_horizontal(pl.col(c).is_null() | pl.col(c).is_nan() for c in columns)
        ).to_series()
        n_missing = int(missing.sum())
        if n_missing:
            log.warning(
                "scorer=%s windows with a missing signal counted as 0 n=%d of=%d",
                self.name,
                n_missing,
                features.height,
            )
        total = pl.sum_horizontal(pl.col(c).fill_nan(None).fill_null(0.0) for c in columns)
        return features.select("window_id", score=total.cast(pl.Float64))


@register_scorer
class ChatSpike(_FeatureSum):
    """``chat_msg_rate_z``: the biggest chat spikes."""

    name: ClassVar[str] = "chat_spike"
    required_features: ClassVar[tuple[str, ...]] = ("chat_msg_rate_z",)


@register_scorer
class Loudness(_FeatureSum):
    """``audio_rms_max_z``: the loudest moments."""

    name: ClassVar[str] = "loudness"
    required_features: ClassVar[tuple[str, ...]] = ("audio_rms_max_z",)


@register_scorer
class ChatLoudness(_FeatureSum):
    """``chat_msg_rate_z + audio_rms_max_z``: the strong unlearned heuristic."""

    name: ClassVar[str] = "chat_loudness"
    required_features: ClassVar[tuple[str, ...]] = ("chat_msg_rate_z", "audio_rms_max_z")


@register_scorer
class RandomScorer(Scorer):
    """Uniform random scores in [0, 1).

    A window's score depends only on the seed and its ``window_id`` (hashed), not on row order
    or which other windows are scored, so the same seed always gives the same ranking.
    """

    name: ClassVar[str] = "random"
    kind: ClassVar[ScorerKind] = "heuristic"

    def __init__(self, seed: int = 0) -> None:
        if seed < 0:
            raise ValueError("seed must be >= 0")
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
