"""Check viewer clips' reported VOD positions against their own audio (#36, exp-002).

For about 50 of the most-viewed pilot clips (`autoclipper.labels.clip_offsets.select_clips`),
download the clip with TwitchDownloaderCLI ``clipdownload``, extract 16 kHz mono audio with
FFmpeg and find it in the VOD's WAV near the reported ``vod_offset`` (`match_clip`). Reports the
shift (true minus reported start) and the end residual (true end minus reported position) over
strong matches. When either is consistent (`rerun_rule` takes the tighter one), reruns exp-001's
measures with every clip moved accordingly: by a constant, or back by its own length.

Writes:

- ``<artifacts>/cache/clip_offsets/``: the downloaded clips (``clips/``) and their audio
  (``audio/``), reused on later runs. Git-ignored; never commit them.
- ``<artifacts>/reports/<experiment>/``: ``matches.parquet`` (one row per clip, with clip IDs)
  and ``summary.json``. Git-ignored.
- ``--summary`` (default: next to the config): the summary, aggregates only (no clip IDs).

Usage, with ``CONFIG`` = ``experiments/exp-002-clip-offset-check/config.yaml``::

    uv run python scripts/clip_offset_check.py --config CONFIG
"""

import hashlib
import json
import logging
import math
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Annotated, Any, Final

import polars as pl
import typer
import yaml
from run_pilot import read_vod_list

from autoclipper.cli import ExitCode
from autoclipper.config import ConfigError, get_settings
from autoclipper.eval.harness import current_git_sha
from autoclipper.features.chat import LexiconError, load_lexicon
from autoclipper.ffmpeg import FFmpegCommand, FFmpegError, ffmpeg_command, run_command
from autoclipper.ingest.chat import ChatDownloadError, find_twitchdownloader
from autoclipper.labels import clip_offsets as co
from autoclipper.labels.alignment_study import (
    ALL,
    StudyConfig,
    StudyError,
    StudyResult,
    VodData,
    load_collection,
    rounded,
    run_study,
)
from autoclipper.logging import configure_logging

REPO_ROOT: Final = Path(__file__).resolve().parents[1]
ALIGNMENT_KEYS: Final = (
    "chance_near_moment",
    "chat_lift",
    "chat_lift_unshifted",
    "audio_lift",
    "top_with_chat",
    "top_with_audio",
    "top_with_both",
    "top_with_neither",
)
log = logging.getLogger("clip_offset_check")
app = typer.Typer(add_completion=False)


@dataclass(frozen=True, slots=True)
class CheckConfig:
    """``experiments/exp-002-clip-offset-check/config.yaml`` (see its comments)."""

    experiment: str
    exp001_config: str
    per_channel: int
    live_per_channel: int
    max_per_vod: int
    max_silent_fraction: float
    search_s: float
    hop_s: float
    min_corr: float
    min_margin: float
    consistent_iqr_s: float
    quality: str
    download_timeout_s: float
    seed: int
    bootstrap_resamples: int

    @classmethod
    def from_yaml(cls, path: Path) -> "CheckConfig":
        """Read a config; every field is required and no other key is allowed.

        Raises:
            StudyError: A key is missing or unknown, or a value doesn't convert.
        """
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise StudyError(f"{path} must be a YAML mapping")
        names = {f.name: f.type for f in fields(cls)}
        unknown = sorted(set(raw) - set(names))
        missing = sorted(set(names) - set(raw))
        if unknown or missing:
            raise StudyError(f"{path}: unknown keys {unknown}, missing keys {missing}")
        # Field annotations are the types themselves (no postponed annotations here).
        kinds: dict[object, type] = {str: str, int: int, float: float}
        try:
            values = {name: kinds[kind](raw[name]) for name, kind in names.items()}
        except (TypeError, ValueError) as exc:
            raise StudyError(f"{path}: {exc}") from None
        return cls(**values)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fail(message: str) -> typer.Exit:
    typer.echo(f"Error: {message}", err=True)
    return typer.Exit(ExitCode.USER_ERROR)


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    path.write_text(text, encoding="utf-8", newline="\n")


def fetch_clip_audio(clip_id: str, cache_dir: Path, config: CheckConfig) -> Path:
    """The clip's 16 kHz mono WAV, downloading and extracting it on first use.

    Raises:
        ChatDownloadError: TwitchDownloaderCLI isn't installed.
        FFmpegError: The download or the extraction failed or timed out.
    """
    wav = cache_dir / "audio" / f"{clip_id}.wav"
    if wav.is_file():
        return wav
    clip = cache_dir / "clips" / f"{clip_id}.mp4"
    clip.parent.mkdir(parents=True, exist_ok=True)
    wav.parent.mkdir(parents=True, exist_ok=True)
    if not clip.is_file():
        staging = clip.with_suffix(".download.mp4")
        command = FFmpegCommand(
            (
                str(find_twitchdownloader()),
                "clipdownload",
                "--id",
                clip_id,
                "-o",
                str(staging),
                "-q",
                config.quality,
                "--encode-metadata",
                "false",
                "--collision",
                "Overwrite",
                "--banner",
                "false",
                "--log-level",
                "Error",
            )
        )
        run_command(command, timeout_s=config.download_timeout_s)
        staging.replace(clip)
    staging_wav = wav.with_suffix(".part.wav")
    extract = ffmpeg_command(
        "-y", "-i", clip, "-vn", "-ac", "1", "-ar", str(co.SAMPLE_RATE_HZ), "-c:a", "pcm_s16le",
        staging_wav,
    )  # fmt: skip
    run_command(extract, timeout_s=config.download_timeout_s)
    staging_wav.replace(wav)
    return wav


type Fetcher = Callable[[str, Path, CheckConfig], Path]


def check_clips(
    selection: pl.DataFrame,
    *,
    wav_paths: Mapping[str, Path],
    cache_dir: Path,
    config: CheckConfig,
    fetch: Fetcher | None = None,
) -> pl.DataFrame:
    """Match every selected clip; a clip that can't be fetched gets null match columns.

    `fetch` defaults to `fetch_clip_audio`, looked up at call time (tests replace it).
    """
    fetch = fetch or fetch_clip_audio
    rows: list[dict[str, Any]] = []
    for clip in selection.iter_rows(named=True):
        match = None
        error = None
        try:
            audio = fetch(clip["clip_id"], cache_dir, config)
            samples, _ = co.read_wav(audio)
            match = co.match_clip(
                samples,
                wav_paths[clip["video_id"]],
                reported_s=clip["reported_s"],
                search_s=config.search_s,
                hop_s=config.hop_s,
            )
        except (FFmpegError, ChatDownloadError, co.ClipAudioError, OSError) as exc:
            error = type(exc).__name__
            log.warning("clip check failed video_id=%s error=%s", clip["video_id"], error)
        rows.append(
            {
                **clip,
                "error": error,
                "true_start_s": match.true_start_s if match else None,
                "shift_s": match.shift_s if match else None,
                "corr": match.corr if match else None,
                "runner_up": match.runner_up if match else None,
                "margin": match.margin if match else None,
                "at_search_edge": match.at_search_edge if match else None,
            }
        )
    return pl.DataFrame(rows, infer_schema_length=None, schema_overrides={"error": pl.String})


def rerun_shifts(groups: Mapping[str, Any]) -> dict[str, float] | None:
    """The value per clip class to move clips by, or None when no group is consistent.

    `groups` is ``summary["shift"]`` or ``summary["end_residual"]``. A class uses its own median
    when it has at least 5 strong matches and is consistent; else the overall median when that
    is consistent; else the class isn't moved.
    """
    overall = groups["all"]
    out: dict[str, float] = {}
    for clip_class in co.CLIP_CLASSES:
        own = groups[clip_class]
        if own and own["n_clips"] >= 5 and own["consistent"]:
            out[clip_class] = own["median_s"]
        elif overall and overall["consistent"]:
            out[clip_class] = overall["median_s"]
    return out or None


def rerun_rule(summary: Mapping[str, Any]) -> dict[str, Any] | None:
    """How to move clips for the rerun: the consistent rule with the smaller spread.

    ``constant``: every clip moves by its class's median shift. ``end_anchored``: the reported
    position is read as the clip's end, so a clip moves back by its own length plus its class's
    median end residual. None when neither rule is consistent.
    """
    options = []
    for rule, key, per_duration in (
        ("constant", "shift", 0.0),
        ("end_anchored", "end_residual", -1.0),
    ):
        shifts = rerun_shifts(summary[key])
        if shifts is not None:
            spread = summary[key]["all"]["iqr_s"] if summary[key]["all"] else math.inf
            options.append((spread, rule, shifts, per_duration))
    if not options:
        return None
    spread, rule, shifts, per_duration = min(options, key=lambda o: o[0])
    return {"rule": rule, "shift_s": shifts, "per_duration": per_duration, "iqr_s": spread}


def _alignment(result: StudyResult) -> dict[str, Any]:
    summary = result.summary
    profiles = summary["profiles"]

    def peak(signal: str, anchor: str) -> dict[str, Any] | None:
        entry = profiles[signal][anchor][ALL][ALL]
        if entry is None:
            return None
        keys = ("peak_offset_s", "peak_low_s", "peak_high_s", "half_height_from_s")
        return {k: entry[k] for k in (*keys, "half_height_to_s")}

    return {
        "alignment": {k: summary["alignment"][ALL][k] for k in ALIGNMENT_KEYS},
        "chat_moment_start": peak("chat", "moment_start"),
        "loudness_moment_start": peak("loudness", "moment_start"),
        "loudness_top_clip_start": peak("loudness", "top_clip_start"),
        "padding_loud": summary["padding"][ALL]["loud"] if summary["padding"][ALL] else None,
        "n_moments": summary["inputs"]["n_moments"],
    }


@app.command()
def main(
    config_path: Annotated[Path, typer.Option("--config", help="The exp-002 config.")],
    data_dir: Annotated[
        Path | None, typer.Option(help="Data directory. Default: AUTOCLIPPER_DATA_DIR.")
    ] = None,
    out_dir: Annotated[
        Path | None,
        typer.Option(help="Report directory. Default: <artifacts>/reports/<experiment>."),
    ] = None,
    cache_dir: Annotated[
        Path | None, typer.Option(help="Clip and series cache. Default: <artifacts>/cache.")
    ] = None,
    summary_path: Annotated[
        Path | None,
        typer.Option("--summary", help="Also write the summary here. Default: next to config."),
    ] = None,
) -> None:
    """Find selected clips in their VODs by audio and report the offset shift."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")
    try:
        config = CheckConfig.from_yaml(config_path)
        study_path = REPO_ROOT / config.exp001_config
        study = StudyConfig.from_yaml(study_path)
        settings = get_settings()
        configure_logging(settings.log_level)
    except (OSError, StudyError, ConfigError) as exc:
        raise _fail(str(exc)) from None
    data_dir = data_dir or settings.data_dir
    out_dir = out_dir or settings.artifacts_dir / "reports" / config.experiment
    cache_dir = cache_dir or settings.artifacts_dir / "cache"
    summary_path = summary_path or config_path.parent / "summary.json"
    vod_list = REPO_ROOT / study.vod_list
    lexicon_path = REPO_ROOT / study.lexicon
    try:
        video_ids = read_vod_list(vod_list)
        excluded = sorted(set(video_ids) & set(study.exclude_video_ids))
        if excluded:
            raise StudyError(f"{vod_list} lists excluded VODs: {', '.join(excluded)}")
        lexicon = load_lexicon(lexicon_path)
        vods = load_collection(
            data_dir, video_ids, lexicon=lexicon, cache_dir=cache_dir / "alignment"
        )
        wav_paths = {v.video_id: data_dir / "interim" / "audio" / f"{v.video_id}.wav" for v in vods}
        muted = {
            str(r["video_id"]): [(m["offset_s"], m["duration_s"]) for m in r["muted_segments"]]
            for r in pl.read_parquet(data_dir / "processed" / "vods.parquet").iter_rows(named=True)
        }
        selection = co.select_clips(
            vods,
            wav_paths=wav_paths,
            muted=muted,
            per_channel=config.per_channel,
            live_per_channel=config.live_per_channel,
            max_per_vod=config.max_per_vod,
            max_silent_fraction=config.max_silent_fraction,
        )
        matches = check_clips(
            selection.clips,
            wav_paths=wav_paths,
            cache_dir=cache_dir / "clip_offsets",
            config=config,
        )
    except (OSError, ValueError, StudyError, LexiconError) as exc:
        raise _fail(str(exc)) from None

    result = co.summarize_matches(
        matches,
        min_corr=config.min_corr,
        min_margin=config.min_margin,
        consistent_iqr_s=config.consistent_iqr_s,
        seed=config.seed,
        n_resamples=config.bootstrap_resamples,
    )
    rule = rerun_rule(result)
    rerun: dict[str, Any] = {"rule": rule}
    if rule is not None:
        moved: list[VodData] = [
            co.shift_vod(v, rule["shift_s"], per_duration=rule["per_duration"]) for v in vods
        ]
        rerun["reported_positions"] = _alignment(run_study(vods, study))
        rerun["corrected_positions"] = _alignment(run_study(moved, study))
    selected = selection.clips
    summary = rounded(
        {
            "experiment": config.experiment,
            "config": {f.name: getattr(config, f.name) for f in fields(config)},
            "selection": {
                "n_selected": selected.height,
                "by_channel": {
                    login: {
                        c: selected.filter(
                            (pl.col("login") == login) & (pl.col("clip_class") == c)
                        ).height
                        for c in co.CLIP_CLASSES
                    }
                    for login in dict.fromkeys(selected["login"].to_list())
                },
                "n_vods": selected["video_id"].n_unique(),
                "n_skipped_muted": selection.n_skipped_muted,
                "n_skipped_silent": selection.n_skipped_silent,
                "n_fetch_failed": int(matches["error"].is_not_null().sum()),
            },
            "matching": result,
            "rerun": rerun,
            "provenance": {
                "git_commit": current_git_sha(),
                "config_sha256": _sha256(config_path),
                "exp001_config_sha256": _sha256(study_path),
                "vod_list_sha256": _sha256(vod_list),
            },
        }
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    matches.write_parquet(out_dir / "matches.parquet")
    _write_json(out_dir / "summary.json", summary)
    _write_json(summary_path, summary)

    overall = summary["matching"]["shift"]["all"]
    typer.echo(
        f"{summary['matching']['n_strong']} of {summary['matching']['n_checked']} clips matched "
        "strongly."
    )
    if overall is not None:
        typer.echo(
            f"True minus reported start: median {overall['median_s']} s "
            f"(95% CI {overall['low_s']} to {overall['high_s']} s), IQR {overall['iqr_s']} s."
        )
    typer.echo(f"Report: {out_dir}")
    typer.echo(f"Summary: {summary_path}")


if __name__ == "__main__":
    app()
