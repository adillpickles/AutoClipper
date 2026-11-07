"""Saved, seeded leave-one-streamer-out folds (labeling.md § 8)."""

import random
import re
from typing import Literal

type Split = Literal["train", "val", "test"]
type Fold = dict[Split, list[str]]


def validate_version(version: str) -> None:
    """Require a portable single path component for an immutable dataset version."""
    reserved = {"con", "prn", "aux", "nul", *(f"{p}{i}" for p in ("com", "lpt") for i in range(10))}
    if (
        re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}", version) is None
        or version.endswith(".")
        or version.split(".")[0].lower() in reserved
    ):
        raise ValueError("dataset version must be a portable name of 1-100 characters")


def make_folds(broadcaster_ids: list[str], *, seed: int) -> list[Fold]:
    """Return five 3/1/1 folds, independent of input ordering.

    Fold i tests streamer i in a seeded permutation and validates on streamer i+1
    (wrapping around). Every streamer is test once and validation once. All VODs of a
    streamer stay together. Fewer/more than five distinct streamers is an input error.
    """
    if seed < 0:
        raise ValueError("seed must be non-negative")
    if any(not isinstance(value, str) or not value for value in broadcaster_ids):
        raise ValueError("broadcaster IDs must be non-empty strings")
    ids = sorted(set(broadcaster_ids))
    if len(ids) != 5:
        raise ValueError("dataset builds require exactly five streamers; use synth --pilot")
    random.Random(seed).shuffle(ids)
    return [
        {
            "test": [ids[i]],
            "val": [ids[(i + 1) % 5]],
            "train": sorted(set(ids) - {ids[i], ids[(i + 1) % 5]}),
        }
        for i in range(5)
    ]


def split_for(broadcaster_id: str, fold: Fold) -> Split:
    """Find a streamer's role in one saved fold."""
    for split, ids in fold.items():
        if broadcaster_id in ids:
            return split
    raise ValueError(f"no split for broadcaster {broadcaster_id}")
