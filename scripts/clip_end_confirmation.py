"""Confirm on new clips that Helix ``vod_offset`` is the clip's end (#36, exp-003).

exp-002 found, after looking at its clips, that the reported position is where a clip ends.
This run tests that rule on 50 clips exp-002 didn't use, with pass criteria fixed in
``experiments/exp-003-clip-end-confirmation/config.yaml`` before any clip was selected:

- per channel 10 clips in four cells (top or low-view, live or post-stream;
  `autoclipper.labels.clip_offsets.select_stratified_clips`), counts 3/2/2/3 for the 1st, 3rd
  and 5th channel and 2/3/3/2 for the others, as the config states;
- each clip downloaded and matched with exp-002's code (`clip_offset_check.fetch_clip_audio`,
  `clip_offset_check.check_clips`);
- PASS or FAIL by `autoclipper.labels.clip_offsets.evaluate_end_rule`.

Writes the per-clip table (with clip IDs) to ``<artifacts>/reports/<experiment>/`` and the
summary (aggregates only) there and next to the config. Clip media go to the shared clip cache
``<artifacts>/cache/clip_offsets/``. Git-ignored.

Usage, with ``CONFIG`` = ``experiments/exp-003-clip-end-confirmation/config.yaml``::

    uv run python scripts/clip_end_confirmation.py --config CONFIG
"""

import hashlib
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Annotated, Any, Final

import clip_offset_check as coc
import numpy as np
import polars as pl
import typer
import yaml
from run_pilot import read_vod_list

from autoclipper.cli import ExitCode
from autoclipper.config import ConfigError, get_settings
from autoclipper.eval.harness import current_git_sha
from autoclipper.features.chat import LexiconError, load_lexicon
from autoclipper.labels import clip_offsets as co
from autoclipper.labels.alignment_study import (
    StudyConfig,
    StudyError,
    VodData,
    load_collection,
    rounded,
)
from autoclipper.logging import configure_logging

REPO_ROOT: Final = Path(__file__).resolve().parents[1]
CELL_COUNTS: Final = ((3, 2, 2, 3), (2, 3, 3, 2))
"""(top-live, top-post, low-live, low-post) for channels 1, 3, 5 and channels 2, 4 (config)."""

app = typer.Typer(add_completion=False)


@dataclass(frozen=True, slots=True)
class EndConfig:
    """``experiments/exp-003-clip-end-confirmation/config.yaml`` (see its comments)."""

    experiment: str
    rule: str
    tolerance_s: float
    max_median_abs_error_s: float
    min_share_within_tolerance: float
    min_corr: float
    min_margin: float
    exp001_config: str
    exclude_experiment: str
    per_channel: int
    max_per_vod: int
    max_silent_fraction: float
    seed: int
    search_s: float
    hop_s: float
    quality: str
    download_timeout_s: float
    bootstrap_resamples: int

    @classmethod
    def from_yaml(cls, path: Path) -> "EndConfig":
        """Read a config; every field is required and no other key is allowed.

        Raises:
            StudyError: A key is missing or unknown, a value doesn't convert, or the cell
                counts don't add up to ``per_channel``.
        """
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise StudyError(f"{path} must be a YAML mapping")
        names = {f.name: f.type for f in fields(cls)}
        unknown = sorted(set(raw) - set(names))
        missing = sorted(set(names) - set(raw))
        if unknown or missing:
            raise StudyError(f"{path}: unknown keys {unknown}, missing keys {missing}")
        kinds: dict[object, type] = {str: str, int: int, float: float}
        try:
            config = cls(**{name: kinds[kind](raw[name]) for name, kind in names.items()})
        except (TypeError, ValueError) as exc:
            raise StudyError(f"{path}: {exc}") from None
        if any(sum(counts) != config.per_channel for counts in CELL_COUNTS):
            raise StudyError(f"{path}: the cell counts must add up to per_channel")
        return config

    def check_config(self) -> coc.CheckConfig:
        """The exp-002 settings the reused download and matching code reads."""
        return coc.CheckConfig(
            experiment=self.experiment,
            exp001_config=self.exp001_config,
            per_channel=self.per_channel,
            live_per_channel=0,
            max_per_vod=self.max_per_vod,
            max_silent_fraction=self.max_silent_fraction,
            search_s=self.search_s,
            hop_s=self.hop_s,
            min_corr=self.min_corr,
            min_margin=self.min_margin,
            consistent_iqr_s=0.0,
            quality=self.quality,
            download_timeout_s=self.download_timeout_s,
            seed=self.seed,
            bootstrap_resamples=self.bootstrap_resamples,
        )


def channel_order(vods: Sequence[VodData]) -> list[str]:
    """Channels in order of first appearance in the VOD list: the order the cells follow."""
    return list(dict.fromkeys(v.login for v in vods))


def cell_count_check(clips: pl.DataFrame, *, channel_order: Sequence[str]) -> dict[str, Any]:
    """Whether each channel got the cell counts `CELL_COUNTS` gives its position.

    Returns:
        ``{"channel_order", "expected", "selected", "match"}`` with counts as
        ``[top-live, top-post, low-live, low-post]`` per channel.
    """
    expected = {
        login: list(CELL_COUNTS[i % len(CELL_COUNTS)]) for i, login in enumerate(channel_order)
    }
    selected = {
        login: [
            clips.filter(
                (pl.col("login") == login) & (pl.col("stratum") == s) & (pl.col("clip_class") == c)
            ).height
            for s, c in co.CELLS
        ]
        for login in channel_order
    }
    return {
        "channel_order": list(channel_order),
        "expected": expected,
        "selected": selected,
        "match": expected == selected,
    }


def _views(frame: pl.DataFrame) -> dict[str, Any] | None:
    if frame.height == 0:
        return None
    views = frame["view_count"].to_numpy()
    return {
        "median": float(np.median(views)),
        "min": int(views.min()),
        "max": int(views.max()),
    }


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fail(message: str) -> typer.Exit:
    typer.echo(f"Error: {message}", err=True)
    return typer.Exit(ExitCode.USER_ERROR)


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    path.write_text(text, encoding="utf-8", newline="\n")


@app.command()
def main(
    config_path: Annotated[Path, typer.Option("--config", help="The exp-003 config.")],
    data_dir: Annotated[
        Path | None, typer.Option(help="Data directory. Default: AUTOCLIPPER_DATA_DIR.")
    ] = None,
    artifacts_dir: Annotated[
        Path | None, typer.Option(help="Artifacts root. Default: AUTOCLIPPER_ARTIFACTS_DIR.")
    ] = None,
    summary_path: Annotated[
        Path | None,
        typer.Option("--summary", help="Also write the summary here. Default: next to config."),
    ] = None,
) -> None:
    """Test "true clip end = vod_offset" on new clips against the pre-registered criteria."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")
    try:
        config = EndConfig.from_yaml(config_path)
        study_path = REPO_ROOT / config.exp001_config
        study = StudyConfig.from_yaml(study_path)
        settings = get_settings()
        configure_logging(settings.log_level)
    except (OSError, StudyError, ConfigError) as exc:
        raise _fail(str(exc)) from None
    data_dir = data_dir or settings.data_dir
    artifacts_dir = artifacts_dir or settings.artifacts_dir
    out_dir = artifacts_dir / "reports" / config.experiment
    summary_path = summary_path or config_path.parent / "summary.json"
    exclude_path = artifacts_dir / "reports" / config.exclude_experiment / "matches.parquet"
    vod_list = REPO_ROOT / study.vod_list
    try:
        if not exclude_path.is_file():
            raise StudyError(f"the clips to exclude aren't there: {exclude_path}")
        excluded = sorted(pl.read_parquet(exclude_path, columns=["clip_id"])["clip_id"].to_list())
        video_ids = read_vod_list(vod_list)
        listed_excluded = sorted(set(video_ids) & set(study.exclude_video_ids))
        if listed_excluded:
            raise StudyError(f"{vod_list} lists excluded VODs: {', '.join(listed_excluded)}")
        vods = load_collection(
            data_dir,
            video_ids,
            lexicon=load_lexicon(REPO_ROOT / study.lexicon),
            cache_dir=artifacts_dir / "cache" / "alignment",
        )
        wav_paths = {v.video_id: data_dir / "interim" / "audio" / f"{v.video_id}.wav" for v in vods}
        muted = {
            str(r["video_id"]): [(m["offset_s"], m["duration_s"]) for m in r["muted_segments"]]
            for r in pl.read_parquet(data_dir / "processed" / "vods.parquet").iter_rows(named=True)
        }
        selection = co.select_stratified_clips(
            vods,
            wav_paths=wav_paths,
            muted=muted,
            exclude=set(excluded),
            cell_counts=CELL_COUNTS,
            max_per_vod=config.max_per_vod,
            max_silent_fraction=config.max_silent_fraction,
            seed=config.seed,
        )
        overlap = set(selection.clips["clip_id"].to_list()) & set(excluded)
        if overlap:
            raise StudyError(f"{len(overlap)} selected clips were used in exp-002")
        matches = coc.check_clips(
            selection.clips,
            wav_paths=wav_paths,
            cache_dir=artifacts_dir / "cache" / "clip_offsets",
            config=config.check_config(),
        )
    except (OSError, ValueError, StudyError, LexiconError) as exc:
        raise _fail(str(exc)) from None

    evaluation = co.evaluate_end_rule(
        matches,
        tolerance_s=config.tolerance_s,
        max_median_abs_error_s=config.max_median_abs_error_s,
        min_share_within_tolerance=config.min_share_within_tolerance,
        min_corr=config.min_corr,
        min_margin=config.min_margin,
        seed=config.seed,
        n_resamples=config.bootstrap_resamples,
    )
    # Not part of the registered criterion: the same test with the downloaded clips' own
    # audio length instead of the metadata duration_s.
    evaluation_audio = co.evaluate_end_rule(
        matches,
        tolerance_s=config.tolerance_s,
        max_median_abs_error_s=config.max_median_abs_error_s,
        min_share_within_tolerance=config.min_share_within_tolerance,
        min_corr=config.min_corr,
        min_margin=config.min_margin,
        seed=config.seed,
        n_resamples=config.bootstrap_resamples,
        length="audio",
    )
    context = co.summarize_matches(
        matches,
        min_corr=config.min_corr,
        min_margin=config.min_margin,
        consistent_iqr_s=10.0,
        seed=config.seed,
        n_resamples=config.bootstrap_resamples,
    )
    clips = selection.clips
    check = cell_count_check(clips, channel_order=channel_order(vods))
    summary: dict[str, Any] = rounded(
        {
            "experiment": config.experiment,
            "rule": config.rule,
            "config": {f.name: getattr(config, f.name) for f in fields(config)},
            "selection": {
                "n_selected": clips.height,
                "by_channel": {
                    login: {
                        f"{s}_{c}": clips.filter(
                            (pl.col("login") == login)
                            & (pl.col("stratum") == s)
                            & (pl.col("clip_class") == c)
                        ).height
                        for s, c in co.CELLS
                    }
                    for login in dict.fromkeys(clips["login"].to_list())
                },
                "n_vods": clips["video_id"].n_unique(),
                "cell_counts": check,
                "views": {
                    stratum: _views(clips.filter(pl.col("stratum") == stratum))
                    for stratum in ("top", "low")
                },
                "n_excluded_from_exp002": len(excluded),
                # The run stops if any selected clip was in exp-002, so this is always 0.
                "n_overlap_with_exp002": len(set(clips["clip_id"].to_list()) & set(excluded)),
                "excluded_ids_sha256": hashlib.sha256("\n".join(excluded).encode()).hexdigest(),
                "n_skipped_muted": selection.n_skipped_muted,
                "n_skipped_silent": selection.n_skipped_silent,
                "n_fetch_failed": int(matches["error"].is_not_null().sum()),
            },
            "evaluation": evaluation,
            "sensitivity_audio_length": evaluation_audio,
            "context": {
                "shift": context["shift"],
                "shift_vs_duration": context["shift_vs_duration"],
            },
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

    overall = evaluation["overall"]
    typer.echo(f"{evaluation['result']}: {config.rule}")
    if overall is not None:
        typer.echo(
            f"{evaluation['n_strong']} of {evaluation['n_checked']} clips matched strongly. "
            f"Median |true end - vod_offset| {overall['median_abs_error_s']:.2f} s "
            f"(<= {config.max_median_abs_error_s} s); "
            f"{overall['share_within_tolerance']:.0%} within {config.tolerance_s} s "
            f"(>= {config.min_share_within_tolerance:.0%})."
        )
    typer.echo(f"Summary: {summary_path}")


if __name__ == "__main__":
    app()
