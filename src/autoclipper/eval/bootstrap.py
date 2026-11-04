"""Bootstrap confidence intervals over VODs (docs/ml/evaluation.md § 1, § 9)."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

import numpy as np

N_RESAMPLES: Final = 1000
CONFIDENCE: Final = 0.95


@dataclass(frozen=True, slots=True)
class Interval:
    """A metric's mean over VODs and its percentile bootstrap interval."""

    mean: float
    low: float
    high: float


def bootstrap_mean(
    values: Sequence[float],
    *,
    seed: int,
    n_resamples: int = N_RESAMPLES,
    confidence: float = CONFIDENCE,
) -> Interval | None:
    """Mean of per-VOD values with a percentile bootstrap interval, resampling VODs.

    Resampling is over the given per-VOD values (never windows), with replacement, using a
    NumPy generator seeded with `seed`, so the same inputs give the same interval.

    Returns:
        None when `values` is empty.

    Raises:
        ValueError: `n_resamples` < 1, `confidence` outside (0, 1), or a value isn't finite.
    """
    if n_resamples < 1:
        raise ValueError("n_resamples must be at least 1")
    if not 0 < confidence < 1:
        raise ValueError("confidence must be between 0 and 1")
    data = np.asarray(values, dtype=np.float64)
    if not np.isfinite(data).all():
        raise ValueError("values must be finite")
    if data.size == 0:
        return None
    rng = np.random.default_rng(seed)
    picks = rng.integers(0, data.size, size=(n_resamples, data.size))
    means = data[picks].mean(axis=1)
    tail = (1 - confidence) / 2 * 100
    low, high = np.percentile(means, [tail, 100 - tail])
    return Interval(float(data.mean()), float(low), float(high))
