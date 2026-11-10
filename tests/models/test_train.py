"""Tests for ``autoclipper train``: a trained model on the synthetic pilot dataset."""

import json
from pathlib import Path

import polars as pl
import pytest
import yaml
from typer.testing import CliRunner

from autoclipper.cli import app
from autoclipper.eval.harness import evaluate, evaluate_mean
from autoclipper.labels.dataset import build_dataset
from autoclipper.models.base import Scorer, load_scorer
from autoclipper.models.baselines import random_scorers
from autoclipper.models.candidates import candidates_frame, generate_candidates
from autoclipper.models.train import RunExistsError, TrainConfig, load_config, train
from autoclipper.synthetic import generate_dataset

LEXICON = Path(__file__).resolve().parents[2] / "configs" / "lexicon.yaml"
VERSION = "smoke-v1"


@pytest.fixture(scope="module")
def pilot(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Five fake streamers with planted highlights, built into a labeled dataset.

    The highlights are dense: only the VOD of streamer 2, which has no viewer clips, has windows
    60 s away from any moment, so only folds that train on streamer 2 have negatives. Split seed
    1 puts streamer 2 in fold 0's training set (the primary fold), where the tests train.
    """
    root = tmp_path_factory.mktemp("pilot")
    generate_dataset(root, seed=1, pilot=True, video=None)
    build_dataset(root, version=VERSION, seed=1, lexicon_path=LEXICON)
    return root


def folds(pilot: Path) -> list[dict[str, list[str]]]:
    manifest = json.loads((pilot / "splits" / f"{VERSION}.json").read_text(encoding="utf-8"))
    return manifest["folds"]


def config(model: str, **extra: object) -> TrainConfig:
    return TrainConfig(dataset_version=VERSION, model=model, **extra)  # pyright: ignore[reportArgumentType]


def run(pilot: Path, tmp_path: Path, cfg: TrainConfig, run_id: str = "r1", **kw: bool) -> Path:
    return train(cfg, data_dir=pilot, artifacts_dir=tmp_path / "artifacts", run_id=run_id, **kw)


def dataset_table(pilot: Path, name: str) -> pl.DataFrame:
    return pl.read_parquet(pilot / "processed" / "datasets" / VERSION / f"{name}.parquet")


@pytest.mark.parametrize("model", ["logreg", "lightgbm"])
def test_a_run_saves_everything_needed_to_reproduce_it(
    pilot: Path, tmp_path: Path, model: str
) -> None:
    out = run(pilot, tmp_path, config(model, seed=5))
    assert out == tmp_path / "artifacts" / "models" / "r1"
    assert {p.name for p in out.iterdir()} >= {
        "scorer.json",
        "features.json",
        "config.json",
        "run.json",
    }
    info = json.loads((out / "run.json").read_text(encoding="utf-8"))
    manifest = json.loads((pilot / "splits" / f"{VERSION}.json").read_text(encoding="utf-8"))
    assert info["dataset_version"] == VERSION
    assert info["seed"] == 5
    assert info["fold"] == 0
    assert info["streamers"] == manifest["folds"][0]
    assert info["git_sha"]
    assert len(info["dataset_manifest_sha256"]) == 64
    assert info["windows"]["train"]["positives"] > 0
    assert info["n_features"] == len(json.loads((out / "features.json").read_text("utf-8")))
    assert set(info["feature_importance"]) == set(json.loads((out / "features.json").read_text()))
    assert json.loads((out / "config.json").read_text(encoding="utf-8"))["model"] == model
    assert load_scorer(out).name == model


def test_the_model_only_trains_on_confident_windows_of_the_training_streamers(
    pilot: Path, tmp_path: Path
) -> None:
    out = run(pilot, tmp_path, config("logreg"))
    info = json.loads((out / "run.json").read_text(encoding="utf-8"))
    labels = dataset_table(pilot, "window_labels")
    windows = dataset_table(pilot, "windows")
    vods = dataset_table(pilot, "vods")
    owner = windows.join(vods.select("video_id", "broadcaster_id"), on="video_id").join(
        labels, on="window_id"
    )
    fold = info["streamers"]
    eligible = owner.filter(pl.col("train_eligible"))
    train_rows = eligible.filter(pl.col("broadcaster_id").is_in(fold["train"]))
    assert info["windows"]["train"]["windows"] == train_rows.height
    assert info["windows"]["train"]["positives"] == int(train_rows["label_pos"].sum())
    # The test streamer's windows are never counted.
    assert (
        info["windows"]["validation"]["windows"]
        == eligible.filter(pl.col("broadcaster_id").is_in(fold["val"])).height
    )


@pytest.mark.parametrize("model", ["logreg", "lightgbm"])
def test_the_same_config_and_seed_give_the_same_model(
    pilot: Path, tmp_path: Path, model: str
) -> None:
    one = run(pilot, tmp_path, config(model, seed=2), "one")
    two = run(pilot, tmp_path, config(model, seed=2), "two")
    features = dataset_table(pilot, "features_chat").drop("feature_version")
    features = features.join(
        dataset_table(pilot, "features_audio").drop("feature_version"), on="window_id"
    )
    assert load_scorer(one).score(features)["score"].to_list() == pytest.approx(
        load_scorer(two).score(features)["score"].to_list()
    )


def test_feature_families_and_exclusions_choose_the_columns(pilot: Path, tmp_path: Path) -> None:
    chat = run(pilot, tmp_path, config("logreg", feature_families=["chat"]), "chat")
    names = json.loads((chat / "features.json").read_text(encoding="utf-8"))
    assert names
    assert all(n.startswith("chat_") for n in names)
    live = run(
        pilot,
        tmp_path,
        config("logreg", exclude_features=["chat_msg_rate_lead1", "chat_msg_rate_lead2"]),
        "live",
    )
    live_names = json.loads((live / "features.json").read_text(encoding="utf-8"))
    assert "chat_msg_rate_lead1" not in live_names
    assert "chat_msg_rate_z" in live_names
    assert any(n.startswith("audio_") for n in live_names)


def test_unknown_exclusions_and_an_existing_run_are_errors(pilot: Path, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unknown columns: nope"):
        run(pilot, tmp_path, config("logreg", exclude_features=["nope"]))
    run(pilot, tmp_path, config("logreg"))
    with pytest.raises(RunExistsError):
        run(pilot, tmp_path, config("logreg"))
    run(pilot, tmp_path, config("logreg", seed=1), force=True)
    # A failed run doesn't leave staging folders behind.
    assert [p.name for p in (tmp_path / "artifacts" / "models").iterdir()] == ["r1"]


def test_a_changed_dataset_is_refused(pilot: Path, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="can't read"):
        train(
            TrainConfig(dataset_version="missing", model="logreg"),
            data_dir=pilot,
            artifacts_dir=tmp_path,
            run_id="r",
        )
    copy = tmp_path / "copy"
    generate_dataset(copy, seed=1, pilot=True, video=None)
    build_dataset(copy, version=VERSION, seed=1, lexicon_path=LEXICON)
    table = copy / "processed" / "datasets" / VERSION / "features_audio.parquet"
    table.write_bytes(table.read_bytes() + b"\0")
    with pytest.raises(ValueError, match="changed since dataset"):
        train(config("logreg"), data_dir=copy, artifacts_dir=tmp_path, run_id="r")


def test_the_config_file_is_validated(tmp_path: Path) -> None:
    path = tmp_path / "train.yaml"
    path.write_text(
        yaml.safe_dump({"dataset_version": "v1", "model": "lightgbm", "params": {"seed": 1}}),
        encoding="utf-8",
    )
    assert load_config(path).fold == 0
    for bad in ({"dataset_version": "v1"}, {"dataset_version": "v1", "model": "svm"}, [1]):
        path.write_text(yaml.safe_dump(bad), encoding="utf-8")
        with pytest.raises(ValueError, match="can't use training config"):
            load_config(path)
    path.write_text(
        yaml.safe_dump({"dataset_version": "v1", "model": "logreg", "colour": 1}), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="colour"):
        load_config(path)
    with pytest.raises(ValueError, match="can't use training config"):
        load_config(tmp_path / "absent.yaml")


def test_a_fold_without_negative_windows_says_so(pilot: Path, tmp_path: Path) -> None:
    fold = next(i for i, f in enumerate(folds(pilot)) if "9000000002" not in f["train"])
    with pytest.raises(ValueError, match="no negative training windows"):
        run(pilot, tmp_path, config("logreg", fold=fold))


def test_conflicting_seeds_are_rejected(pilot: Path, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="seed"):
        run(pilot, tmp_path, config("lightgbm", seed=1, params={"seed": 2}))


def top10_hit_rate(pilot: Path, scorer: Scorer | list[Scorer], video_ids: list[str]) -> float:
    """Hit rate@10 over the given VODs, through the real candidates → evaluation chain."""
    windows = dataset_table(pilot, "windows")
    vods = dataset_table(pilot, "vods").filter(pl.col("video_id").is_in(video_ids))
    moments = dataset_table(pilot, "moments").filter(pl.col("video_id").is_in(video_ids))
    clips = dataset_table(pilot, "clips").filter(pl.col("video_id").is_in(video_ids))
    chat = dataset_table(pilot, "features_chat").drop("feature_version")
    features = chat.join(
        dataset_table(pilot, "features_audio").drop("feature_version"), on="window_id"
    )
    scorers = scorer if isinstance(scorer, list) else [scorer]
    runs = []
    for one in scorers:
        frames = []
        for vod in vods.iter_rows(named=True):
            win = windows.filter(pl.col("video_id") == vod["video_id"])
            feats = features.join(win.select("window_id"), on="window_id")
            scores = win.select("window_id", "start_s", "end_s").join(
                one.score(feats), on="window_id"
            )
            found = generate_candidates(scores, duration_s=vod["duration_s"], features=feats)
            frames.append(candidates_frame(vod["video_id"], found))
        runs.append(pl.concat(frames))
    result = (
        evaluate_mean(runs, moments, clips, seed=0)
        if len(runs) > 1
        else evaluate(runs[0], moments, clips, seed=0)
    )
    hit = result.summary["hit_rate_at_10"]
    assert hit is not None
    return hit.mean


@pytest.mark.parametrize("model", ["logreg", "lightgbm"])
def test_a_trained_model_finds_the_planted_peaks_better_than_random(
    pilot: Path, tmp_path: Path, model: str
) -> None:
    """The sanity check from the issue: trained on three streamers, tested on the held-out two."""
    out = run(pilot, tmp_path, config(model))
    info = json.loads((out / "run.json").read_text(encoding="utf-8"))
    vods = dataset_table(pilot, "vods")
    held_out = vods.filter(
        pl.col("broadcaster_id").is_in(info["streamers"]["val"] + info["streamers"]["test"])
    )["video_id"].to_list()
    assert held_out
    trained = top10_hit_rate(pilot, load_scorer(out), held_out)
    random = top10_hit_rate(pilot, list(random_scorers(range(20))), held_out)
    assert trained > random, (trained, random)
    fitted = info["metrics"]["train"]
    assert fitted["average_precision"] > fitted["positive_rate"]
    # Streamer 4's confident windows are all positive, so validation can't be scored.
    assert info["metrics"]["validation"]["average_precision"] is None


def test_the_train_command_runs_a_config_file(
    pilot: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTOCLIPPER_DATA_DIR", str(pilot))
    monkeypatch.setenv("AUTOCLIPPER_ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    path = tmp_path / "train.yaml"
    path.write_text(
        yaml.safe_dump({"dataset_version": VERSION, "model": "logreg"}), encoding="utf-8"
    )
    runner = CliRunner()
    result = runner.invoke(app, ["train", "--config", str(path), "--run-id", "cli-run"])
    assert result.exit_code == 0, result.output
    assert "train average precision" in result.output
    assert "validation average precision: n/a" in result.output
    assert (tmp_path / "artifacts" / "models" / "cli-run" / "run.json").is_file()
    again = runner.invoke(app, ["train", "--config", str(path), "--run-id", "cli-run"])
    assert again.exit_code == 1
    assert "already exists" in again.output
    bad = runner.invoke(app, ["train", "--config", str(path), "--run-id", "../x"])
    assert bad.exit_code == 1
    missing = runner.invoke(app, ["train", "--config", str(tmp_path / "nope.yaml")])
    assert missing.exit_code == 1
