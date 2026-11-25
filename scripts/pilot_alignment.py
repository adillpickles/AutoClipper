"""Run the chat delay and clip alignment study (#36) on the pilot collection.

Reads the experiment config (``experiments/exp-001-chat-lag-alignment/config.yaml``), the active
VOD list it names and the data directory, then writes:

- ``<out-dir>/`` (default ``<artifacts>/reports/<experiment>/``): ``summary.json`` and the tables
  the notebook plots (``per_vod``, ``xcorr``, ``profiles``, ``padding``, ``sensitivity`` as
  Parquet). Git-ignored.
- ``--summary`` (default ``summary.json`` next to the config): the same summary, for the
  experiment's record.

Per-second series are cached under ``<artifacts>/cache/alignment/`` and rebuilt when their inputs
change. Only VODs in the list are read; a VOD in the config's ``exclude_video_ids`` (the
historical zero-clip VOD) makes the run fail instead of being studied. The output holds counts
and statistics only.

Usage, with ``CONFIG`` = ``experiments/exp-001-chat-lag-alignment/config.yaml``::

    uv run python scripts/pilot_alignment.py --config CONFIG
"""

import hashlib
import json
import sys
from pathlib import Path
from typing import Annotated, Final

import typer
from run_pilot import read_vod_list

from autoclipper.cli import ExitCode
from autoclipper.config import ConfigError, get_settings
from autoclipper.eval.harness import current_git_sha
from autoclipper.features.chat import LexiconError, load_lexicon
from autoclipper.labels.alignment_study import (
    ALL,
    StudyConfig,
    StudyError,
    load_collection,
    rounded,
    run_study,
)
from autoclipper.logging import configure_logging

REPO_ROOT: Final = Path(__file__).resolve().parents[1]

app = typer.Typer(add_completion=False)


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
    config_path: Annotated[Path, typer.Option("--config", help="The experiment config.")],
    data_dir: Annotated[
        Path | None, typer.Option(help="Data directory. Default: AUTOCLIPPER_DATA_DIR.")
    ] = None,
    out_dir: Annotated[
        Path | None,
        typer.Option(help="Report directory. Default: <artifacts>/reports/<experiment>."),
    ] = None,
    cache_dir: Annotated[
        Path | None,
        typer.Option(help="Series cache. Default: <artifacts>/cache/alignment."),
    ] = None,
    summary_path: Annotated[
        Path | None,
        typer.Option("--summary", help="Also write the summary here. Default: next to config."),
    ] = None,
) -> None:
    """Measure chat delay and clip alignment on the listed VODs."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")

    try:
        config = StudyConfig.from_yaml(config_path)
        settings = get_settings()
        configure_logging(settings.log_level)
    except (OSError, StudyError, ConfigError) as exc:
        raise _fail(str(exc)) from None
    data_dir = data_dir or settings.data_dir
    out_dir = out_dir or settings.artifacts_dir / "reports" / config.experiment
    cache_dir = cache_dir or settings.artifacts_dir / "cache" / "alignment"
    summary_path = summary_path or config_path.parent / "summary.json"
    vod_list = REPO_ROOT / config.vod_list
    lexicon_path = REPO_ROOT / config.lexicon
    try:
        video_ids = read_vod_list(vod_list)
        excluded = sorted(set(video_ids) & set(config.exclude_video_ids))
        if excluded:
            raise StudyError(f"{vod_list} lists excluded VODs: {', '.join(excluded)}")
        lexicon = load_lexicon(lexicon_path)
        vods = load_collection(data_dir, video_ids, lexicon=lexicon, cache_dir=cache_dir)
        result = run_study(vods, config)
    except (OSError, ValueError, StudyError, LexiconError) as exc:
        raise _fail(str(exc)) from None

    summary = rounded(
        {
            **result.summary,
            "provenance": {
                "git_commit": current_git_sha(),
                "vod_list": config.vod_list,
                "vod_list_sha256": _sha256(vod_list),
                "lexicon_sha256": _sha256(lexicon_path),
                "config_sha256": _sha256(config_path),
            },
        }
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    _write_json(out_dir / "summary.json", summary)
    for name, frame in (
        ("per_vod", result.per_vod),
        ("xcorr", result.xcorr),
        ("profiles", result.profiles),
        ("padding", result.padding),
        ("sensitivity", result.sensitivity),
    ):
        frame.write_parquet(out_dir / f"{name}.parquet")
    _write_json(summary_path, summary)

    delay = summary["chat_delay"]["loud_event"][ALL]
    typer.echo(
        f"{summary['inputs']['n_vods']} VODs, {summary['inputs']['n_moments']} moments, "
        f"{summary['inputs']['n_loud_events']} loud events."
    )
    if delay is not None:
        typer.echo(
            f"Chat peaks {delay['peak_offset_s']} s after a loud event "
            f"(95% CI {delay['peak_low_s']} to {delay['peak_high_s']} s)."
        )
    typer.echo(f"Report: {out_dir}")
    typer.echo(f"Summary: {summary_path}")


if __name__ == "__main__":
    app()
