"""The scorer interface every baseline and trained model shares (docs/ml/modeling-plan.md).

A scorer turns a VOD's window features into one score per window (higher = more clip-worthy).
Candidate generation (`autoclipper.models.candidates`), the evaluation and the app then treat
every scorer the same way.

- `Scorer.fit` learns from training windows; heuristics ignore it.
- `Scorer.score` returns ``window_id`` and ``score`` for the given windows, in their order.
- `Scorer.save` writes the scorer to a directory as ``scorer.json`` (kind, name, parameters);
  `load_scorer` reads it back through `register_scorer`'s registry. Trained models extend
  `save`/`load` with their own artifacts in the same directory.
"""

import json
from abc import ABC, abstractmethod
from pathlib import Path
from typing import ClassVar, Final, Literal, Self

import polars as pl

SCORER_FILE: Final = "scorer.json"

type ScorerKind = Literal["heuristic", "trained_model"]

_REGISTRY: dict[str, type["Scorer"]] = {}


class ScorerError(ValueError):
    """A scorer can't score these features, or a saved scorer can't be loaded."""


class Scorer(ABC):
    """Scores windows from their features.

    Attributes:
        name: The ID used in configs, reports and the API's ``scorer.name``, e.g. ``chat_spike``.
        kind: ``heuristic`` or ``trained_model`` (``ScorerInfo.kind`` in the API contract).
        required_features: Feature columns `score` reads.
    """

    name: ClassVar[str]
    kind: ClassVar[ScorerKind]
    required_features: ClassVar[tuple[str, ...]] = ()

    def fit(self, windows: pl.DataFrame, labels: pl.Series) -> Self:
        """Learn from training windows and their labels. Heuristics have nothing to learn."""
        return self

    @abstractmethod
    def score(self, features: pl.DataFrame) -> pl.DataFrame:
        """Return ``window_id`` and a finite Float64 ``score`` for every row, in row order.

        Raises:
            ScorerError: A required feature column is missing.
        """

    def params(self) -> dict[str, object]:
        """JSON-serializable constructor arguments; `load_scorer` passes them back."""
        return {}

    def save(self, directory: Path) -> Path:
        """Write ``scorer.json`` into `directory` (created if needed) and return its path."""
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / SCORER_FILE
        data = {"kind": self.kind, "name": self.name, "params": self.params()}
        path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return path

    @classmethod
    def load(cls, directory: Path, params: dict[str, object]) -> Self:
        """Rebuild the scorer from its saved parameters (and, for trained models, artifacts)."""
        return cls(**params)

    def _require(self, features: pl.DataFrame) -> None:
        missing = [c for c in ("window_id", *self.required_features) if c not in features.columns]
        if missing:
            raise ScorerError(f"{self.name} needs feature columns: {', '.join(missing)}")


def register_scorer[T: type[Scorer]](cls: T) -> T:
    """Class decorator: make `cls` loadable by name with `load_scorer`."""
    if cls.name in _REGISTRY and _REGISTRY[cls.name] is not cls:
        raise ValueError(f"a scorer named {cls.name!r} is already registered")
    _REGISTRY[cls.name] = cls
    return cls


def scorer_names() -> list[str]:
    """Names of every registered scorer, sorted."""
    return sorted(_REGISTRY)


def make_scorer(name: str, **params: object) -> Scorer:
    """Create a registered scorer by name.

    Raises:
        ScorerError: No scorer has that name, or the parameters don't fit it.
    """
    cls = _REGISTRY.get(name)
    if cls is None:
        raise ScorerError(f"unknown scorer {name!r}; known: {', '.join(scorer_names())}")
    try:
        return cls(**params)
    except TypeError as exc:
        raise ScorerError(f"bad parameters for scorer {name!r}: {exc}") from exc


def load_scorer(directory: Path) -> Scorer:
    """Load a scorer saved with `Scorer.save`.

    Raises:
        ScorerError: ``scorer.json`` is missing or malformed, or names an unknown scorer.
    """
    path = directory / SCORER_FILE
    try:
        data: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ScorerError(f"can't read {path}: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("name"), str):
        raise ScorerError(f"{path} has no scorer name")
    params = data.get("params", {})
    if not isinstance(params, dict):
        raise ScorerError(f"{path}: params must be an object")
    cls = _REGISTRY.get(data["name"])
    if cls is None:
        raise ScorerError(f"{path} names unknown scorer {data['name']!r}")
    try:
        return cls.load(directory, params)
    except TypeError as exc:
        raise ScorerError(f"{path}: bad parameters for {data['name']!r}: {exc}") from exc
