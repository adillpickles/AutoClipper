"""Write the synthetic dataset to disk: Parquet tables, WAVs, videos and a manifest.

Layout under the output directory (it mirrors the data dir, docs/architecture/overview.md §5)::

    manifest.json                          seed, versions, scenario, SHA-256 of every file
    catalog.parquet
    processed/broadcasters.parquet
    processed/vods.parquet
    processed/clips/synthetic-seed-<seed>.parquet
    interim/chat/<video_id>.parquet
    interim/audio/<video_id>.wav           16 kHz mono 16-bit PCM
    raw/video/<video_id>.mp4               only when video is generated

The same seed gives byte-identical tables, WAVs and manifest with the same locked dependencies
(``uv.lock``). Parquet files embed the pyarrow version (``created_by``), so another pyarrow
version gives other bytes; the manifest records the versions.
"""

import hashlib
import json
import logging
import platform
import shutil
import tempfile
import uuid
from dataclasses import asdict, dataclass
from datetime import timedelta
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from autoclipper import __version__
from autoclipper.ffmpeg import Runner, ffmpeg_version, run_command
from autoclipper.synthetic import schemas
from autoclipper.synthetic.media import VideoOptions, render_audio, video_command, write_wav
from autoclipper.synthetic.scenario import BASE_TIME, Scenario, build_scenario

log = logging.getLogger(__name__)

GENERATOR = "autoclipper.synthetic"
GENERATOR_VERSION = "1"
"""Bump when the same seed would give different output (scenario, schemas or file layout)."""

MANIFEST = "manifest.json"

NOTE = (
    "Synthetic development data: fake VODs, clips and chat, generated audio and video. No real "
    "Twitch data. Tables follow the PROPOSED tables in docs/architecture/data-model.md for "
    "prototype use; they are not adopted contracts (ADR-0009). Hashes use a public synthetic salt."
)


class OutputDirError(Exception):
    """The output directory can't be used: it holds other files, or it isn't a directory."""


@dataclass(frozen=True, slots=True)
class GeneratedDataset:
    """What `generate_dataset` wrote.

    Attributes:
        out_dir: The absolute output directory.
        files: Paths relative to `out_dir` (POSIX style), sorted, including the manifest.
    """

    out_dir: Path
    files: tuple[str, ...]


def default_output_dir(data_dir: Path, seed: int) -> Path:
    """Where ``autoclipper synth`` writes without ``--out``: apart from real data."""
    return data_dir / "synthetic" / f"seed-{seed}"


def generate_dataset(
    out_dir: Path,
    *,
    seed: int,
    video: VideoOptions | None = None,
    force: bool = False,
    runner: Runner | None = None,
) -> GeneratedDataset:
    """Generate the synthetic dataset for `seed` into `out_dir`.

    Everything is written to a temporary sibling directory first and moved into place at the
    end, so a failure leaves no partial dataset behind. An earlier dataset being replaced is
    moved aside first and deleted only once the new one is in place; if the swap fails, it is
    put back.

    Args:
        out_dir: Directory to create. It may exist only if it is empty, or with `force` if it
            holds an earlier synthetic dataset and nothing else (only files its manifest lists).
            Any other content is never deleted, so a mistaken path can't clobber real data.
        seed: Non-negative seed; the same seed gives the same data.
        video: Generate a video per VOD with FFmpeg using these options; None for no video.
        force: Replace an earlier synthetic dataset in `out_dir`.
        runner: Runs FFmpeg; defaults to `autoclipper.ffmpeg.run_subprocess`.

    Returns:
        The output directory and the files written.

    Raises:
        ValueError: `seed` is negative.
        OutputDirError: `out_dir` is a file, or holds files and `force` doesn't apply, or the
            earlier dataset couldn't be swapped out (it is then left as it was).
        FFmpegError: Generating a video failed (only with `video`).
    """
    out_dir = out_dir.absolute()
    _check_output_dir(out_dir, force=force)
    scenario = build_scenario(seed)
    out_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=f".{out_dir.name}-", suffix=".partial", dir=out_dir.parent)
    )
    try:
        files = _write_all(scenario, staging, video, runner)
        _check_output_dir(out_dir, force=force)
        _publish(staging, out_dir)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    log.info("synthetic dataset written seed=%d files=%d out=%s", seed, len(files), out_dir)
    return GeneratedDataset(out_dir, files)


def _check_output_dir(out_dir: Path, *, force: bool) -> None:
    if not out_dir.exists():
        return
    if not out_dir.is_dir():
        raise OutputDirError(f"{out_dir} exists and is not a directory")
    if not any(out_dir.iterdir()):
        return
    if not _is_synthetic_dataset(out_dir):
        raise OutputDirError(
            f"{out_dir} is not empty and holds files that aren't part of a synthetic dataset; "
            "choose an empty or new directory (it is never overwritten, even with --force)"
        )
    if not force:
        raise OutputDirError(
            f"{out_dir} already holds a synthetic dataset; pass --force to replace it"
        )


def _is_synthetic_dataset(directory: Path) -> bool:
    """True if `directory` holds only a dataset this generator wrote: its manifest and files.

    Files the manifest lists may be missing, but any other file (or a link) means the directory
    holds something else too, and `--force` must not delete it.
    """
    try:
        manifest = json.loads((directory / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(manifest, dict) or manifest.get("generator") != GENERATOR:
        return False
    entries = manifest.get("files")
    if not isinstance(entries, list):
        return False
    listed = {MANIFEST}
    for entry in entries:
        path = entry.get("path") if isinstance(entry, dict) else None
        if not isinstance(path, str):
            return False
        listed.add(path)
    for path in directory.rglob("*"):
        if path.is_symlink() or (
            path.is_file() and path.relative_to(directory).as_posix() not in listed
        ):
            return False
    return True


def _publish(staging: Path, out_dir: Path) -> None:
    """Move `staging` to `out_dir`, replacing an earlier dataset there without risking it.

    The earlier dataset is renamed aside first (on Windows that fails while one of its files is
    open, before anything changed), the new one is renamed into place, and only then is the
    earlier one deleted. If the second rename fails, the earlier dataset is renamed back.
    """
    if out_dir.exists() and not any(out_dir.iterdir()):
        out_dir.rmdir()
    if not out_dir.exists():
        staging.rename(out_dir)
        return
    backup = out_dir.with_name(f".{out_dir.name}-{uuid.uuid4().hex}.old")
    try:
        out_dir.rename(backup)
    except OSError as exc:
        raise OutputDirError(
            f"can't replace the dataset in {out_dir}: moving it aside failed ({exc}); close "
            "programs using its files and try again. Nothing was changed."
        ) from exc
    try:
        staging.rename(out_dir)
    except BaseException as exc:
        backup.rename(out_dir)
        if isinstance(exc, OSError):
            raise OutputDirError(
                f"can't move the new dataset into {out_dir} ({exc}); the earlier dataset was "
                "put back"
            ) from exc
        raise
    try:
        shutil.rmtree(backup)
    except OSError as exc:
        log.warning("could not delete the replaced synthetic dataset path=%s error=%s", backup, exc)


def _write_all(
    scenario: Scenario, root: Path, video: VideoOptions | None, runner: Runner | None
) -> tuple[str, ...]:
    """Write every file under `root`; return their relative paths, sorted."""
    tables = {
        "catalog.parquet": (schemas.CATALOG, _catalog_rows(scenario, video is not None)),
        "processed/broadcasters.parquet": (schemas.BROADCASTERS, scenario.broadcasters),
        "processed/vods.parquet": (schemas.VODS, scenario.vod_rows),
        f"processed/clips/{scenario.snapshot_id}.parquet": (schemas.CLIPS, scenario.clips),
    }
    for video_id, rows in scenario.chat.items():
        tables[f"interim/chat/{video_id}.parquet"] = (schemas.CHAT_MESSAGES, rows)
    for name, (schema, rows) in tables.items():
        _write_parquet(pa.Table.from_pylist(rows, schema=schema), root / name)
    environment = {
        "python": platform.python_version(),
        "numpy": np.__version__,
        "pyarrow": pa.__version__,
    }
    for vod in scenario.vods:
        video_id = vod.plan.video_id
        wav = root / "interim" / "audio" / f"{video_id}.wav"
        wav.parent.mkdir(parents=True, exist_ok=True)
        write_wav(render_audio(vod, scenario.seed), wav)
        if video is not None:
            mp4 = root / "raw" / "video" / f"{video_id}.mp4"
            mp4.parent.mkdir(parents=True, exist_ok=True)
            duration_s = vod.duration_s if video.duration_s is None else video.duration_s
            command = video_command(wav, mp4, duration_s, video)
            run_command(command, timeout_s=60 + duration_s, runner=runner)
    if video is not None:
        environment["ffmpeg"] = ffmpeg_version(runner=runner)
    files = sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())
    manifest = {
        "generator": GENERATOR,
        "generator_version": GENERATOR_VERSION,
        "autoclipper_version": __version__,
        "seed": scenario.seed,
        "note": NOTE,
        "environment": environment,
        "video": None if video is None else asdict(video),
        "clips_snapshot": {
            "snapshot_id": scenario.snapshot_id,
            "capture_started_at": scenario.capture_window[0].isoformat(),
            "capture_ended_at": scenario.capture_window[1].isoformat(),
        },
        "vods": [
            {
                "video_id": vod.plan.video_id,
                "duration_s": vod.duration_s,
                "quiet_chat_s": list(vod.plan.quiet),
                "muted_s": None if vod.plan.muted is None else list(vod.plan.muted),
                "planted_events": [asdict(event) for event in vod.events],
            }
            for vod in scenario.vods
        ],
        "files": [{"path": path, "sha256": _sha256(root / path)} for path in files],
    }
    text = json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=True) + "\n"
    (root / MANIFEST).write_text(text, encoding="utf-8", newline="\n")
    return tuple(sorted([*files, MANIFEST]))


def _catalog_rows(scenario: Scenario, with_video: bool) -> list[dict[str, object]]:
    object_types = ["vod_meta", "clips", "chat", "audio"] + (["video"] if with_video else [])
    created_at = BASE_TIME + timedelta(days=11)
    return [
        {
            "object_type": object_type,
            "object_key": vod.plan.video_id,
            "status": "ok",
            "tool": f"{GENERATOR}@{GENERATOR_VERSION}",
            "stage_version": GENERATOR_VERSION,
            "created_at": created_at,
            "error": None,
        }
        for vod in scenario.vods
        for object_type in object_types
    ]


def _write_parquet(table: pa.Table, path: Path) -> None:
    """Write `table` with every byte-affecting option pinned, and nulls checked."""
    for column, field in zip(table.columns, table.schema, strict=True):
        if not field.nullable and column.null_count:
            raise ValueError(f"{path.name}: non-nullable column {field.name} has nulls")
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        table,
        path,
        version="2.6",
        compression="snappy",
        use_dictionary=False,
        write_statistics=True,
        data_page_version="1.0",
        store_schema=True,
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()
