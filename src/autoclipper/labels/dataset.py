"""Build immutable labeled datasets from ingested (or generated synthetic) inputs."""

import hashlib
import json
import logging
import platform
import random
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from statistics import median

import polars as pl
import pyarrow as pa
import pyarrow.parquet as pq
from pydantic import AwareDatetime, BaseModel, ConfigDict

from autoclipper import __version__, tables
from autoclipper.eval.harness import current_git_sha
from autoclipper.features.audio import compute_audio_features
from autoclipper.features.chat import compute_chat_features, load_lexicon
from autoclipper.labels.annotations import (
    LABEL_RULES_VERSION,
    LABELS_SCHEMA,
    NEG_BUFFER_S,
    WEIGHT_VERSION,
    label_windows,
)
from autoclipper.labels.moments import GAP_S, MOMENTS_SCHEMA, merge_clips_into_moments
from autoclipper.labels.splits import Fold, make_folds, split_for, validate_version
from autoclipper.labels.windows import STRIDE_S, WINDOW_S, windows_for_vods

log = logging.getLogger(__name__)


class Snapshot(BaseModel):
    """Metadata required to pin a clip capture; other ingest metadata stays in its manifest."""

    model_config = ConfigDict(extra="ignore")
    snapshot_id: str
    video_id: str | None = None
    capture_started_at: AwareDatetime
    capture_ended_at: AwareDatetime
    complete: bool


@dataclass
class _Inputs:
    root: Path
    files: dict[Path, str] = field(default_factory=dict)

    def pin(self, path: Path) -> None:
        """Record the content hash before consuming an input."""
        self.files.setdefault(path, _sha256(path))

    def json(self, path: Path) -> dict:
        """Read a pinned JSON object."""
        self.pin(path)
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError(f"{path}: expected a JSON object")
        return value

    def table(self, path: Path, schema: pa.Schema) -> pl.DataFrame:
        """Read a pinned table and validate required fields."""
        self.pin(path)
        try:
            source = pq.read_table(path)
            # Casting a numeric Twitch ID to a string, or assuming UTC for naive times,
            # would hide a broken upstream contract. Arrow may normalize string widths.
            for expected in schema:
                actual = source.schema.field(expected.name).type
                if pa.types.is_string(expected.type) and not (
                    pa.types.is_string(actual) or pa.types.is_large_string(actual)
                ):
                    raise ValueError(f"{expected.name} must contain strings")
                if pa.types.is_timestamp(expected.type) and (
                    not pa.types.is_timestamp(actual) or actual.tz is None
                ):
                    raise ValueError(f"{expected.name} must be a timezone-aware timestamp")
            table = source.cast(schema)
        except (pa.ArrowException, ValueError, KeyError) as exc:
            raise ValueError(f"{path}: invalid table: {exc}") from exc
        if any(c.null_count for c, f in zip(table.columns, schema, strict=True) if not f.nullable):
            raise ValueError(f"{path}: null in a required column")
        result = pl.from_arrow(table)
        assert isinstance(result, pl.DataFrame)
        return result

    def lineage(self) -> list[dict[str, str]]:
        """Recheck consumed inputs and return their portable lineage."""
        for path, digest in self.files.items():
            if _sha256(path) != digest:
                raise ValueError(f"input changed during dataset build: {path}")
        return [
            {
                "path": path.relative_to(self.root).as_posix()
                if path.is_relative_to(self.root)
                else str(path),
                "sha256": digest,
            }
            for path, digest in sorted(self.files.items())
        ]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _snapshots(inputs: _Inputs, selected: list[str] | None) -> list[Snapshot]:
    directory = inputs.root / "processed" / "clips"
    synthetic = inputs.root / "manifest.json"
    if synthetic.is_file():
        manifest = inputs.json(synthetic)
        if manifest.get("generator") == "autoclipper.synthetic":
            info = manifest["clips_snapshot"]
            snapshot = Snapshot.model_validate({**info, "complete": True})
            if selected is not None and selected != [snapshot.snapshot_id]:
                raise ValueError("selected snapshots do not match the synthetic manifest")
            return [snapshot]
    if selected is None:
        paths = sorted(directory.glob("*.json"))
    else:
        for name in selected:
            validate_version(name)
        if len(set(selected)) != len(selected):
            raise ValueError("snapshot IDs must be unique")
        paths = [directory / f"{name}.json" for name in sorted(selected)]
    snapshots = [Snapshot.model_validate(inputs.json(path)) for path in paths]
    for path, snapshot in zip(paths, snapshots, strict=True):
        if snapshot.snapshot_id != path.stem or snapshot.video_id is None:
            raise ValueError(f"{path}: manifest must identify its snapshot and VOD")
    return snapshots


def build_dataset(
    data_dir: Path,
    *,
    version: str,
    seed: int,
    lexicon_path: Path,
    snapshot_ids: list[str] | None = None,
) -> Path:
    """Write windows, moments, annotations, features, QC and five frozen streamer folds.

    A version is never replaced. Clip captures must be unambiguous (select IDs explicitly
    when a VOD has several); missing or incomplete coverage leaves that VOD unlabeled.
    All VODs need parsed chat and normalized audio. No network or real collection occurs.
    Outputs are staged; the split JSON is the final commit marker, written only on success.
    """
    validate_version(version)
    root = data_dir.resolve()
    out = root / "processed" / "datasets" / version
    split_path = root / "splits" / f"{version}.json"
    if out.exists() or split_path.exists():
        raise ValueError(f"dataset version {version} already exists; choose a new version")
    inputs = _Inputs(root)
    vods = inputs.table(root / "processed" / "vods.parquet", tables.VODS).sort("video_id")
    folds = make_folds(vods["broadcaster_id"].to_list(), seed=seed)
    windows = windows_for_vods(vods)
    inputs.pin(lexicon_path.resolve())
    lexicon = load_lexicon(lexicon_path)
    snapshots = _snapshots(inputs, snapshot_ids)
    clips_by_vod: dict[str, tuple[Snapshot, pl.DataFrame]] = {}
    raw_clips: list[pl.DataFrame] = []
    for snapshot in snapshots:
        validate_version(snapshot.snapshot_id)
        if snapshot.capture_ended_at < snapshot.capture_started_at:
            raise ValueError("snapshot capture ends before it starts")
        clip_path = root / "processed" / "clips" / f"{snapshot.snapshot_id}.parquet"
        clips = inputs.table(clip_path, tables.CLIPS)
        if clips["clip_id"].n_unique() != clips.height:
            raise ValueError(f"{clip_path}: duplicate clip_id in pinned snapshot")
        assigned = [snapshot.video_id] if snapshot.video_id else vods["video_id"].to_list()
        # A combined synthetic capture explicitly covers every generated VOD, including no-clips.
        for video_id in assigned:
            if video_id not in vods["video_id"].to_list():
                raise ValueError(f"snapshot refers to unknown VOD {video_id}")
            if video_id in clips_by_vod:
                raise ValueError(f"multiple snapshots for VOD {video_id}; select with --snapshot")
            clips_by_vod[video_id] = (snapshot, clips)
        raw_clips.append(clips)

    frames: dict[str, list[pl.DataFrame]] = {
        name: [] for name in ("moments", "window_labels", "features_chat", "features_audio")
    }
    coverage = []
    for vod in vods.iter_rows(named=True):
        video_id = vod["video_id"]
        win = windows.filter(pl.col("video_id") == video_id)
        chat = inputs.table(root / "interim" / "chat" / f"{video_id}.parquet", tables.CHAT_MESSAGES)
        if chat.filter(pl.col("video_id") != video_id).height:
            raise ValueError(f"chat file contains another VOD: {video_id}")
        wav = root / "interim" / "audio" / f"{video_id}.wav"
        inputs.pin(wav)
        frames["features_chat"].append(compute_chat_features(win, chat, lexicon=lexicon))
        frames["features_audio"].append(compute_audio_features(win, wav))
        pinned = clips_by_vod.get(video_id)
        record = {
            "video_id": video_id,
            "snapshot_id": None,
            "complete": False,
            "mature": False,
            "reason": "missing clip snapshot",
        }
        if pinned:
            snapshot, clips = pinned
            mature = snapshot.capture_started_at - vod["created_at"] >= timedelta(days=7)
            record.update(
                snapshot_id=snapshot.snapshot_id,
                complete=snapshot.complete,
                mature=mature,
                reason=None if snapshot.complete else "incomplete capture",
            )
            if snapshot.complete:
                placed = clips.filter(pl.col("video_id") == video_id)
                if placed.filter(pl.col("broadcaster_id") != vod["broadcaster_id"]).height:
                    raise ValueError(f"clip broadcaster disagrees with VOD {video_id}")
                moments = merge_clips_into_moments(placed).moments
                frames["moments"].append(moments)
                frames["window_labels"].append(
                    label_windows(
                        win,
                        moments,
                        clips,
                        broadcaster_id=vod["broadcaster_id"],
                        split=split_for(vod["broadcaster_id"], folds[0]),
                        mature=mature,
                    )
                )
            if not mature:
                record["reason"] = "VOD under seven days old at capture"
        coverage.append(record)
        log.info(
            "dataset VOD built video_id=%s windows=%d labeled=%s",
            video_id,
            win.height,
            record["complete"],
        )
    outputs = {name: pl.concat(parts) for name, parts in frames.items() if parts}
    outputs.setdefault("moments", MOMENTS_SCHEMA.to_frame())
    outputs.setdefault("window_labels", LABELS_SCHEMA.to_frame())
    outputs["windows"] = windows
    outputs["vods"] = vods
    # One row per observation, even if distinct VOD snapshots observed the same clip.
    if raw_clips:
        outputs["clips"] = pl.concat(
            [
                clips.with_columns(snapshot_id=pl.lit(snapshot.snapshot_id))
                for snapshot, clips in zip(snapshots, raw_clips, strict=True)
            ]
        )
    quality = _quality(vods, outputs, raw_clips, folds, seed)
    metadata = {
        "dataset_version": version,
        "seed": seed,
        "primary_fold": 0,
        "folds": folds,
        "label_rules_version": LABEL_RULES_VERSION,
        "weight_version": WEIGHT_VERSION,
        "parameters": {
            "window_s": WINDOW_S,
            "stride_s": STRIDE_S,
            "gap_s": GAP_S,
            "neg_buffer_s": NEG_BUFFER_S,
        },
        "environment": {
            "python": platform.python_version(),
            "polars": pl.__version__,
            "pyarrow": pa.__version__,
            "autoclipper": __version__,
            "git_sha": current_git_sha(),
        },
        "coverage": coverage,
        "inputs": inputs.lineage(),
        "feature_versions": {
            name: frame["feature_version"].unique().sort().to_list()
            for name, frame in outputs.items()
            if name.startswith("features_")
        },
        "quality": quality,
    }
    _publish(out, split_path, outputs, metadata)
    return split_path


def _quality(
    vods: pl.DataFrame,
    outputs: dict[str, pl.DataFrame],
    clips: list[pl.DataFrame],
    folds: list[Fold],
    seed: int,
) -> dict:
    labels, moments, windows = (outputs[name] for name in ("window_labels", "moments", "windows"))
    n_clips = sum(frame.height for frame in clips)
    null_offsets = sum(frame["vod_offset_s"].null_count() for frame in clips)
    fraction = null_offsets / n_clips if n_clips else None
    lengths = sorted((moments["end_s"] - moments["start_s"]).to_list())
    annotated = labels.join(windows, on="window_id").join(
        vods.select("video_id", "broadcaster_id"), on="video_id"
    )
    rates = []
    for fold in folds:
        rates.append(
            {
                split: {
                    "windows": subset.height,
                    "positive_rate": sum(subset["label_pos"].to_list()) / subset.height
                    if subset.height
                    else None,
                    "train_eligible": subset.filter(pl.col("train_eligible")).height,
                }
                for split, ids in fold.items()
                for subset in [annotated.filter(pl.col("broadcaster_id").is_in(ids))]
            }
        )
    channels = []
    for (broadcaster_id,), channel in vods.group_by("broadcaster_id", maintain_order=True):
        n = moments.filter(pl.col("video_id").is_in(channel["video_id"].implode())).height
        hours = sum(channel["duration_s"].to_list()) / 3600
        channels.append(
            {
                "broadcaster_id": broadcaster_id,
                "moments": n,
                "vod_hours": hours,
                "moments_per_hour": n / hours if hours else None,
            }
        )
    chat = outputs["features_chat"]
    rng = random.Random(seed)
    samples = {}
    for name, frame in (
        ("positive", annotated.filter(pl.col("label_pos"))),
        ("confident_negative", annotated.filter(~pl.col("label_pos") & pl.col("train_eligible"))),
    ):
        rows = (
            frame.select("window_id", "video_id", "start_s", "end_s").sort("window_id").to_dicts()
        )
        samples[name] = rng.sample(rows, min(10, len(rows)))
    return {
        "clips": n_clips,
        "null_offset_clips": null_offsets,
        "without_video_clips": sum(frame["video_id"].null_count() for frame in clips),
        "null_offset_fraction": fraction,
        "null_offset_alert": fraction is not None and fraction > 0.2,
        "moments_per_channel": channels,
        "moment_lengths_s": {
            "count": len(lengths),
            "min": lengths[0] if lengths else None,
            "median": median(lengths) if lengths else None,
            "max": lengths[-1] if lengths else None,
            "over_90_s": sum(length > 90 for length in lengths),
        },
        "chat_coverage_fraction": sum((chat["chat_msg_rate"] > 0).to_list()) / chat.height
        if chat.height
        else None,
        "positive_rates_by_fold": rates,
        "manual_review": {"status": "pending", "requested_per_class": 10, "samples": samples},
    }


def _publish(
    out: Path,
    split_path: Path,
    outputs: dict[str, pl.DataFrame],
    metadata: dict,
) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    split_path.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{out.name}-", dir=out.parent))
    published = False
    split_created = False
    try:
        for name, frame in outputs.items():
            frame.write_parquet(staging / f"{name}.parquet")
        metadata["outputs"] = [
            {"path": f"processed/datasets/{out.name}/{path.name}", "sha256": _sha256(path)}
            for path in sorted(staging.glob("*.parquet"))
        ]
        (staging / "README.md").write_text(
            f"# Dataset {out.name}\n\nManual review: **pending**. Watch the positives and "
            "confident "
            "negatives listed in quality.json and record findings here. Fewer than 10 available "
            "samples in a class is reported by the sample list length.\n",
            encoding="utf-8",
        )
        (staging / "quality.json").write_text(_json(metadata["quality"]), encoding="utf-8")
        (staging / "manifest.json").write_text(_json(metadata), encoding="utf-8")
        if out.exists() or split_path.exists():
            raise ValueError(f"dataset version {out.name} already exists")
        staging.rename(out)
        published = True
        # Exclusive create prevents accidentally replacing an already frozen split.
        with split_path.open("x", encoding="utf-8") as file:
            split_created = True
            file.write(_json(metadata))
    except BaseException:
        if split_created:
            split_path.unlink()
        if published:
            shutil.rmtree(out)
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _json(value: object) -> str:
    return json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n"
