"""Train a window scorer from a built dataset and save it as a reproducible run.

``autoclipper train --config <file>`` reads a `TrainConfig`, trains one scorer on the training
streamers of one saved fold (labeling.md § 8) and writes ``artifacts/models/<run_id>/``:

- ``scorer.json`` and the model file(s): the scorer, loadable with
  `autoclipper.models.base.load_scorer`.
- ``config.json``: the resolved training config.
- ``features.json``: the feature columns, in model order.
- ``run.json``: dataset version and manifest hash, git commit, seed, fold, window counts,
  library versions, validation metrics and feature importances.

Only windows that are `train_eligible` are used (confident positives and negatives; buffer
windows and immature captures are left out), weighted by ``sample_weight`` and then class
balanced. The validation streamer's metrics are reported; the test streamer's windows are never
read, so nothing here can be tuned on test (modeling-plan.md § 4). Results are produced and
reported through `autoclipper evaluate` (experiments.md), not here.
"""

import hashlib
import json
import logging
import platform
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Literal

import lightgbm
import numpy as np
import polars as pl
import sklearn
import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sklearn.metrics import average_precision_score

from autoclipper import __version__
from autoclipper.eval.harness import current_git_sha
from autoclipper.labels.splits import validate_version
from autoclipper.models.base import Scorer, ScorerError, make_scorer
from autoclipper.models.trained import TrainedScorer

log = logging.getLogger(__name__)

FAMILIES: Final = {"chat": "chat_", "audio": "audio_"}
"""Feature families and the column prefix of each."""

_NON_FEATURES: Final = ("window_id", "feature_version")


class TrainConfig(BaseModel):
    """What to train: the dataset, the fold, the model and its settings.

    Attributes:
        dataset_version: A version built by ``autoclipper build-dataset``.
        model: A trained scorer's name.
        params: The scorer's constructor arguments (the defaults when empty).
        fold: Which saved fold's train/validation streamers to use; 0 is the primary split.
        seed: Seed for the model's randomness (recorded for every model, used by LightGBM).
        feature_families: Which feature families the model sees (chat/audio ablations).
        exclude_features: Columns to drop, e.g. the ``chat_msg_rate_lead*`` look-ahead features
            for a live-mode experiment.
        data_dir: The data root; default is the configured one.
    """

    model_config = ConfigDict(extra="forbid")

    dataset_version: str
    model: Literal["logreg", "lightgbm"]
    params: dict[str, object] = Field(default_factory=dict)
    fold: int = Field(default=0, ge=0, le=4)
    seed: int = Field(default=0, ge=0)
    feature_families: list[Literal["chat", "audio"]] = Field(
        default_factory=lambda: ["chat", "audio"], min_length=1
    )
    exclude_features: list[str] = Field(default_factory=list)
    data_dir: Path | None = None


def load_config(path: Path) -> TrainConfig:
    """Read a YAML training config.

    Raises:
        ValueError: The file can't be read, isn't a mapping or has invalid or unknown settings.
    """
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        return TrainConfig.model_validate(raw)
    except (OSError, UnicodeDecodeError, yaml.YAMLError, ValidationError) as exc:
        raise ValueError(f"can't use training config {path}: {exc}") from exc


class RunExistsError(FileExistsError):
    """A model run folder with that ID already exists."""


def train(
    config: TrainConfig,
    *,
    data_dir: Path,
    artifacts_dir: Path,
    run_id: str,
    force: bool = False,
) -> Path:
    """Train the configured scorer and save the run; returns the run folder.

    Args:
        config: The training config.
        data_dir: The data root, used when the config doesn't name one.
        artifacts_dir: The artifacts root; the run goes in ``models/<run_id>/``.
        run_id: The run folder's name.
        force: Replace an existing run with this ID.

    Raises:
        ValueError: The dataset is missing, changed or can't be trained on (no positive
            windows, a bad parameter, an unknown feature to exclude).
        RunExistsError: The run folder exists and `force` is false.
    """
    validate_version(config.dataset_version)
    root = (config.data_dir or data_dir).resolve()
    out = artifacts_dir / "models" / run_id
    if out.exists() and not force:
        raise RunExistsError(f"{out} already exists; choose another --run-id or pass --force")
    split_path = root / "splits" / f"{config.dataset_version}.json"
    manifest = _read_json(split_path)
    folds: list[dict[str, list[str]]] = manifest["folds"]
    fold = folds[config.fold]

    tables = _load_tables(root, config.dataset_version, manifest)
    columns = _feature_columns(tables["features"], config)
    # Only the training and validation streamers' windows are read past this point.
    frame = (
        tables["windows"]
        .select("window_id", "video_id")
        .join(tables["vods"].select("video_id", "broadcaster_id"), on="video_id")
        .join(
            tables["labels"].select("window_id", "label_pos", "train_eligible", "sample_weight"),
            on="window_id",
        )
        .join(tables["features"].select("window_id", *columns), on="window_id")
        .filter(
            pl.col("train_eligible") & pl.col("broadcaster_id").is_in(fold["train"] + fold["val"])
        )
        .sort("window_id")
    )
    train_rows = frame.filter(pl.col("broadcaster_id").is_in(fold["train"]))
    val_rows = frame.filter(pl.col("broadcaster_id").is_in(fold["val"]))
    positives = int(train_rows["label_pos"].sum())
    if positives == 0 or positives == train_rows.height:
        kind = "positive" if positives == 0 else "negative"
        raise ValueError(
            f"fold {config.fold} has no {kind} training windows; build the dataset from VODs "
            "with mature, complete clip snapshots and enough un-clipped time"
        )

    params = dict(config.params)
    if config.model == "lightgbm" and params.setdefault("seed", config.seed) != config.seed:
        raise ValueError("params.seed conflicts with the config's seed; set only seed")
    try:
        scorer = make_scorer(config.model, **params)
        if not isinstance(scorer, TrainedScorer):
            raise ValueError(f"{config.model} isn't a trained model")
        scorer.fit(
            train_rows.select("window_id", *columns),
            train_rows["label_pos"],
            weights=train_rows["sample_weight"],
        )
    except ScorerError as exc:
        raise ValueError(str(exc)) from exc

    metrics = {
        "train": _metrics(scorer, train_rows, columns),
        "validation": _metrics(scorer, val_rows, columns),
    }
    log.info(
        "trained run_id=%s model=%s features=%d train_windows=%d val_ap=%s",
        run_id,
        config.model,
        len(columns),
        train_rows.height,
        metrics["validation"]["average_precision"],
    )
    run = {
        "run_id": run_id,
        "created_at": datetime.now(tz=UTC).isoformat(),
        "model": config.model,
        "dataset_version": config.dataset_version,
        "dataset_manifest_sha256": _sha256(split_path),
        "git_sha": current_git_sha(),
        "seed": config.seed,
        "fold": config.fold,
        "streamers": fold,
        "n_features": len(columns),
        "windows": {
            "train": _counts(train_rows),
            "validation": _counts(val_rows),
        },
        "metrics": metrics,
        "feature_importance": scorer.importances(),
        "versions": {
            "feature_versions": manifest["feature_versions"],
            "label_rules_version": manifest["label_rules_version"],
            "weight_version": manifest["weight_version"],
            "autoclipper": __version__,
            "python": platform.python_version(),
            "polars": pl.__version__,
            "scikit_learn": sklearn.__version__,
            "lightgbm": lightgbm.__version__,
        },
    }
    _publish(out, scorer, config, run, force=force)
    return out


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"can't read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_tables(root: Path, version: str, manifest: dict) -> dict[str, pl.DataFrame]:
    """Read the dataset tables after checking them against the hashes the build pinned."""
    pinned = {Path(o["path"]).name: o["sha256"] for o in manifest["outputs"]}
    directory = root / "processed" / "datasets" / version
    tables: dict[str, pl.DataFrame] = {}
    for name, file in (
        ("windows", "windows"),
        ("vods", "vods"),
        ("labels", "window_labels"),
        ("chat", "features_chat"),
        ("audio", "features_audio"),
    ):
        path = directory / f"{file}.parquet"
        if not path.is_file():
            raise ValueError(f"dataset {version} is missing {path}")
        if pinned.get(path.name) != _sha256(path):
            raise ValueError(f"{path} changed since dataset {version} was built")
        tables[name] = pl.read_parquet(path)
    tables["features"] = (
        tables.pop("chat")
        .drop("feature_version")
        .join(tables.pop("audio").drop("feature_version"), on="window_id")
    )
    return tables


def _feature_columns(features: pl.DataFrame, config: TrainConfig) -> tuple[str, ...]:
    """The columns the model sees: the chosen families minus the excluded ones."""
    unknown = sorted(set(config.exclude_features) - set(features.columns))
    if unknown:
        raise ValueError(f"exclude_features names unknown columns: {', '.join(unknown)}")
    prefixes = tuple(FAMILIES[family] for family in config.feature_families)
    columns = tuple(
        c
        for c in features.columns
        if c not in _NON_FEATURES and c.startswith(prefixes) and c not in config.exclude_features
    )
    if not columns:
        raise ValueError("no feature columns left after choosing families and exclusions")
    return columns


def _counts(rows: pl.DataFrame) -> dict[str, int]:
    positives = int(rows["label_pos"].sum())
    return {"windows": rows.height, "positives": positives, "negatives": rows.height - positives}


def _metrics(
    scorer: Scorer, rows: pl.DataFrame, columns: tuple[str, ...]
) -> dict[str, float | None]:
    """Average precision (PR-AUC) over confident windows, next to the chance level.

    A model that ranks windows at random scores about the positive rate, so the gap between
    the two is what counts. None where the split lacks positive or negative windows: with one
    class, average precision says nothing.
    """
    positives = int(rows["label_pos"].sum())
    rate = positives / rows.height if rows.height else None
    if positives == 0 or positives == rows.height:
        return {"average_precision": None, "positive_rate": rate}
    scores = scorer.score(rows.select("window_id", *columns))["score"].to_numpy()
    labels = rows["label_pos"].cast(pl.Int64).to_numpy()
    return {
        "average_precision": float(average_precision_score(labels, np.asarray(scores))),
        "positive_rate": rate,
    }


def _publish(out: Path, scorer: Scorer, config: TrainConfig, run: dict, *, force: bool) -> None:
    """Write the run into a staging folder, then move it into place."""
    out.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{out.name}-", dir=out.parent))
    try:
        scorer.save(staging)
        (staging / "config.json").write_text(
            config.model_dump_json(indent=2, exclude={"data_dir"}) + "\n", encoding="utf-8"
        )
        (staging / "run.json").write_text(
            json.dumps(run, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8"
        )
        if out.exists():
            if not force:
                raise RunExistsError(f"{out} already exists")
            shutil.rmtree(out)
        staging.rename(out)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
