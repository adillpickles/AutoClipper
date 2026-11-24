import hashlib
import json
from collections.abc import Callable
from pathlib import Path

import polars as pl
import pytest
from typer.testing import CliRunner

from autoclipper.cli import app
from autoclipper.labels import dataset as dataset_module
from autoclipper.labels.dataset import build_dataset
from autoclipper.synthetic import generate_dataset

LEXICON = Path(__file__).resolve().parents[2] / "configs" / "lexicon.yaml"


@pytest.fixture
def pilot(tmp_path: Path) -> Path:
    root = tmp_path / "pilot"
    generate_dataset(root, seed=1, pilot=True)
    return root


def build(root: Path, version: str = "v1", snapshots: list[str] | None = None) -> dict:
    path = build_dataset(
        root, version=version, seed=42, lexicon_path=LEXICON, snapshot_ids=snapshots
    )
    return json.loads(path.read_text(encoding="utf-8"))


def output(root: Path, name: str, version: str = "v1") -> pl.DataFrame:
    return pl.read_parquet(root / "processed" / "datasets" / version / f"{name}.parquet")


def ingest_manifests(root: Path) -> list[str]:
    """Replace only synthetic metadata with per-VOD manifests in the ingest layout."""
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    combined = root / "processed" / "clips" / "synthetic-seed-1.parquet"
    clips = pl.read_parquet(combined)
    ids = []
    for video_id in pl.read_parquet(root / "processed" / "vods.parquet")["video_id"]:
        snapshot_id = f"{video_id}-capture"
        path = combined.with_name(f"{snapshot_id}.parquet")
        clips.filter(pl.col("video_id") == video_id).write_parquet(path)
        path.with_suffix(".json").write_text(
            json.dumps(
                {
                    **manifest["clips_snapshot"],
                    "snapshot_id": snapshot_id,
                    "video_id": video_id,
                    "complete": True,
                }
            ),
            encoding="utf-8",
        )
        ids.append(snapshot_id)
    (root / "manifest.json").unlink()
    combined.unlink()
    return ids


def test_pilot_generator_is_deterministic_and_keeps_default_scenario(tmp_path: Path) -> None:
    one = generate_dataset(tmp_path / "one", seed=7, pilot=True)
    two = generate_dataset(tmp_path / "two", seed=7, pilot=True)
    assert {p: (one.out_dir / p).read_bytes() for p in one.files} == {
        p: (two.out_dir / p).read_bytes() for p in two.files
    }
    vods = pl.read_parquet(one.out_dir / "processed" / "vods.parquet")
    assert vods.height == 6
    assert vods["broadcaster_id"].n_unique() == 5


def test_build_pins_lineage_features_labels_and_primary_split(pilot: Path) -> None:
    manifest = build(pilot)
    assert len(manifest["folds"]) == 5
    assert manifest["primary_fold"] == 0
    windows, labels = output(pilot, "windows"), output(pilot, "window_labels")
    assert labels.height == windows.height
    assert output(pilot, "features_chat").height == windows.height
    assert output(pilot, "features_audio").height == windows.height
    assert not set(labels.columns) & set(output(pilot, "features_chat").columns) - {"window_id"}
    assigned = labels.join(windows, on="window_id").join(
        output(pilot, "vods").select("video_id", "broadcaster_id"), on="video_id"
    )
    assert (
        assigned.group_by("broadcaster_id").agg(pl.col("split").n_unique())["split"].to_list()
        == [1] * 5
    )
    for split, ids in manifest["folds"][0].items():
        assert set(assigned.filter(pl.col("split") == split)["broadcaster_id"]) == set(ids)
    for entry in manifest["inputs"]:
        path = Path(entry["path"])
        if not path.is_absolute():
            path = pilot / path
        assert hashlib.sha256(path.read_bytes()).hexdigest() == entry["sha256"]
    for entry in manifest["outputs"]:
        assert hashlib.sha256((pilot / entry["path"]).read_bytes()).hexdigest() == entry["sha256"]
    quality = manifest["quality"]
    assert quality["null_offset_clips"] == 2
    assert len(quality["moments_per_channel"]) == 5
    assert quality["moment_lengths_s"]["count"] == 14
    assert len(quality["positive_rates_by_fold"]) == 5
    assert 0 < quality["chat_coverage_fraction"] < 1
    assert quality["manual_review"]["status"] == "pending"
    assert len(quality["manual_review"]["samples"]["positive"]) == 10
    assert all(c["mature"] for c in manifest["coverage"])


def test_rebuild_never_overwrites_frozen_version(pilot: Path) -> None:
    before = build(pilot)
    with pytest.raises(ValueError, match="already exists"):
        build(pilot)
    assert json.loads((pilot / "splits" / "v1.json").read_text()) == before
    assert build(pilot, "v2")["folds"] == before["folds"]


def test_missing_capture_stays_unlabeled_but_covered_empty_capture_is_negative(pilot: Path) -> None:
    ids = ingest_manifests(pilot)
    # First VOD has no capture; third VOD has a complete, empty capture.
    for suffix in (".json", ".parquet"):
        (pilot / "processed" / "clips" / f"{ids[0]}{suffix}").unlink()
    manifest = build(pilot)
    frame = output(pilot, "window_labels").join(output(pilot, "windows"), on="window_id")
    assert not frame.filter(pl.col("video_id") == "9100000001").height
    empty = frame.filter(pl.col("video_id") == "9100000003")
    assert empty.height
    assert not empty["label_pos"].any()
    assert empty["train_eligible"].all()
    assert manifest["coverage"][0]["reason"] == "missing clip snapshot"


def test_incomplete_and_immature_captures_are_flagged(pilot: Path) -> None:
    ids = ingest_manifests(pilot)
    for index, updates in (
        (0, {"complete": False}),
        (
            1,
            {
                "capture_started_at": "2026-09-09T18:00:00+00:00",
                "capture_ended_at": "2026-09-09T18:01:00+00:00",
            },
        ),
    ):
        path = pilot / "processed" / "clips" / f"{ids[index]}.json"
        data = json.loads(path.read_text())
        path.write_text(json.dumps({**data, **updates}))
    manifest = build(pilot)
    frame = output(pilot, "window_labels").join(output(pilot, "windows"), on="window_id")
    assert not frame.filter(pl.col("video_id") == "9100000001").height
    immature = frame.filter(pl.col("video_id") == "9100000002")
    assert immature.height
    assert not immature["train_eligible"].any()
    assert immature["sample_weight"].null_count() == immature.height
    assert not manifest["coverage"][0]["complete"]
    assert not manifest["coverage"][1]["mature"]


def test_ambiguous_snapshots_require_explicit_selection(pilot: Path) -> None:
    ids = ingest_manifests(pilot)
    original = pilot / "processed" / "clips" / f"{ids[0]}.json"
    copy_id = f"{ids[0]}-another"
    original.with_name(f"{copy_id}.json").write_text(
        json.dumps({**json.loads(original.read_text()), "snapshot_id": copy_id})
    )
    original.with_name(f"{copy_id}.parquet").write_bytes(
        original.with_suffix(".parquet").read_bytes()
    )
    with pytest.raises(ValueError, match="multiple snapshots"):
        build(pilot)
    assert not (pilot / "splits" / "v1.json").exists()
    assert build(pilot, snapshots=ids)["dataset_version"] == "v1"


def test_missing_input_does_not_publish_partial_dataset(pilot: Path) -> None:
    (pilot / "interim" / "audio" / "9100000006.wav").unlink()
    with pytest.raises(FileNotFoundError):
        build(pilot)
    assert not (pilot / "splits" / "v1.json").exists()
    assert not (pilot / "processed" / "datasets" / "v1").exists()


def test_changed_source_aborts_instead_of_pinning_different_bytes(
    pilot: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = dataset_module.compute_audio_features
    changed = pilot / "interim" / "chat" / "9100000001.parquet"

    def mutate_after_read(windows: pl.DataFrame, wav: Path) -> pl.DataFrame:
        result = original(windows, wav)
        if wav.stem == "9100000001":
            changed.write_bytes(changed.read_bytes() + b"changed")
        return result

    monkeypatch.setattr(dataset_module, "compute_audio_features", mutate_after_read)
    with pytest.raises(ValueError, match="input changed"):
        build(pilot)
    assert not (pilot / "splits" / "v1.json").exists()


def test_failed_split_publication_cleans_outputs(
    pilot: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = Path.open

    def fail_split(path: Path, *args: object, **kwargs: object) -> object:
        if path == pilot / "splits" / "v1.json":
            raise OSError("simulated disk error")
        # Preserve Path.open's overloaded return type through this test-only forwarding shim.
        forward: Callable = original
        return forward(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_split)
    with pytest.raises(OSError, match="simulated disk error"):
        build(pilot)
    assert not (pilot / "processed" / "datasets" / "v1").exists()
    assert not (pilot / "splits" / "v1.json").exists()


def test_no_capture_does_not_invent_any_negative_labels(pilot: Path) -> None:
    ingest_manifests(pilot)
    manifest = build(pilot, snapshots=[])
    assert output(pilot, "window_labels").is_empty()
    assert output(pilot, "moments").is_empty()
    assert output(pilot, "windows").height > 0
    assert all(not row["complete"] for row in manifest["coverage"])
    assert manifest["quality"]["positive_rates_by_fold"][0]["test"]["positive_rate"] is None


@pytest.mark.parametrize("column", ["video_id", "created_at"])
def test_broken_id_or_timestamp_types_are_not_silently_coerced(pilot: Path, column: str) -> None:
    path = pilot / "processed" / "vods.parquet"
    frame = pl.read_parquet(path)
    value = (
        pl.col(column).cast(pl.Int64)
        if column == "video_id"
        else (pl.col(column).dt.replace_time_zone(None))
    )
    frame.with_columns(value).write_parquet(path)
    with pytest.raises(ValueError, match="must"):
        build(pilot)
    assert not (pilot / "splits" / "v1.json").exists()


def test_cli_smoke_and_user_errors(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["synth", "--pilot", "--seed", "1", "--no-video"])
    assert result.exit_code == 0, result.output
    root = tmp_path / "data" / "synthetic" / "pilot-seed-1"
    args = ["build-dataset", "--data-dir", str(root), "--lexicon", str(LEXICON)]
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    assert '"positive_rates_by_fold"' in result.stdout
    assert '"status": "pending"' in result.stdout
    assert runner.invoke(app, args).exit_code == 1
    assert runner.invoke(app, [*args, "--dataset-version", "../bad"]).exit_code == 1
    standard = generate_dataset(tmp_path / "standard", seed=1)
    result = runner.invoke(
        app, ["build-dataset", "--data-dir", str(standard.out_dir), "--lexicon", str(LEXICON)]
    )
    assert result.exit_code == 1
    assert "synth --pilot" in result.stderr


def test_dataset_rejects_malformed_manifests_and_snapshots(pilot: Path) -> None:
    # Mismatched snapshot selection on synthetic manifest.
    with pytest.raises(ValueError, match="selected snapshots do not match"):
        build(pilot, snapshots=["wrong-snapshot"])

    # Non-JSON-object manifest fails clearly.
    manifest_path = pilot / "manifest.json"
    original_manifest = manifest_path.read_text(encoding="utf-8")
    manifest_path.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="expected a JSON object"):
        build(pilot)
    manifest_path.write_text(original_manifest, encoding="utf-8")

    # Non-synthetic manifest falls through to processed/clips/*.json; duplicate selection fails.
    ids = ingest_manifests(pilot)
    manifest_path.write_text(json.dumps({"generator": "custom"}), encoding="utf-8")
    with pytest.raises(ValueError, match="snapshot IDs must be unique"):
        build(pilot, snapshots=[ids[0], ids[0]])

    # Snapshot manifest whose snapshot_id disagrees with filename (or missing video_id) is rejected.
    first_manifest = pilot / "processed" / "clips" / f"{ids[0]}.json"
    first_data = json.loads(first_manifest.read_text(encoding="utf-8"))
    first_manifest.write_text(
        json.dumps({**first_data, "snapshot_id": "mismatch"}), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="must identify its snapshot and VOD"):
        build(pilot, snapshots=[ids[0]])

    # Snapshot ending before it starts is rejected.
    first_manifest.write_text(
        json.dumps(
            {
                **first_data,
                "capture_started_at": "2026-10-10T12:00:00+00:00",
                "capture_ended_at": "2026-10-10T11:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="ends before it starts"):
        build(pilot, snapshots=[ids[0]])

    # Snapshot referring to an unknown VOD is rejected.
    first_manifest.write_text(
        json.dumps({**first_data, "video_id": "9999999999"}), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="unknown VOD 9999999999"):
        build(pilot, snapshots=[ids[0]])
    first_manifest.write_text(json.dumps(first_data), encoding="utf-8")

    # Duplicate clip_id in a pinned snapshot parquet is rejected.
    first_clips_path = pilot / "processed" / "clips" / f"{ids[0]}.parquet"
    first_clips = pl.read_parquet(first_clips_path)
    pl.concat([first_clips, first_clips.head(1)]).write_parquet(first_clips_path)
    with pytest.raises(ValueError, match="duplicate clip_id"):
        build(pilot, snapshots=[ids[0]])
    first_clips.write_parquet(first_clips_path)

    # Clip broadcaster disagreeing with VOD broadcaster is rejected.
    first_clips.with_columns(broadcaster_id=pl.lit("wrong-broadcaster")).write_parquet(
        first_clips_path
    )
    with pytest.raises(ValueError, match="clip broadcaster disagrees"):
        build(pilot, snapshots=[ids[0]])
    first_clips.write_parquet(first_clips_path)

    # Chat file containing another VOD's messages is rejected.
    chat_path = pilot / "interim" / "chat" / "9100000001.parquet"
    chat = pl.read_parquet(chat_path)
    chat.with_columns(video_id=pl.lit("other-vod")).write_parquet(chat_path)
    with pytest.raises(ValueError, match="chat file contains another VOD"):
        build(pilot, snapshots=[ids[0]])
    chat.write_parquet(chat_path)

    # Null in a required (non-nullable) column of vods.parquet is rejected.
    vods_path = pilot / "processed" / "vods.parquet"
    vods = pl.read_parquet(vods_path)
    vods.with_columns(broadcaster_id=pl.lit(None, dtype=pl.String)).write_parquet(vods_path)
    with pytest.raises(ValueError, match=r"broadcaster_id.*non-nullable|null in a required column"):
        build(pilot, snapshots=[ids[0]])


def test_dataset_publish_checks_races_and_cleans_up_partial_split_writes(
    pilot: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = pilot / "processed" / "datasets" / "v-race"
    split_path = pilot / "splits" / "v-race.json"
    out.mkdir(parents=True)
    with pytest.raises(ValueError, match="already exists"):
        dataset_module._publish(
            out,
            split_path,
            {"vods": pl.DataFrame({"video_id": ["v1"]})},
            {"quality": {}},
        )
    out.rmdir()

    # Simulate a failure while writing the split file after exclusive open succeeded.
    original_open = Path.open

    class _FailingWriter:
        def __init__(self, inner: object) -> None:
            self._inner = inner

        def __enter__(self) -> "_FailingWriter":
            return self

        def __exit__(self, *args: object) -> None:
            close = getattr(self._inner, "close", None)
            if callable(close):
                close()

        def write(self, _text: str) -> int:
            raise OSError("disk full mid-write")

    def _open_with_midwrite_error(path: Path, *args: object, **kwargs: object) -> object:
        forward: Callable = original_open
        handle = forward(path, *args, **kwargs)
        if path == split_path:
            return _FailingWriter(handle)
        return handle

    monkeypatch.setattr(Path, "open", _open_with_midwrite_error)
    with pytest.raises(OSError, match="disk full mid-write"):
        dataset_module._publish(
            out,
            split_path,
            {"vods": pl.DataFrame({"video_id": ["v1"]})},
            {"quality": {}},
        )
    assert not split_path.exists()
    assert not out.exists()
