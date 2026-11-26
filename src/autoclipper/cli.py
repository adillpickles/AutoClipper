"""The ``autoclipper`` command-line interface.

The CLI is a thin layer over the library (docs/development/conventions.md §7): a subcommand
parses its arguments, calls one library function and turns failures into an `ExitCode`.
Subcommands are verbs (``ingest``, ``export``, ...) registered on `app` with ``@app.command()``.
"""

import json
import logging
import re
from enum import IntEnum, StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import typer

# Typer vendors Click as a private module and doesn't re-export UsageError, the base class of
# every bad-input error (unknown command/option, invalid value, missing argument). It's the only
# way to tell those apart from errors a command raises on purpose, so the import is kept here,
# in one place; tests/test_cli.py fails if a Typer upgrade moves it.
from typer._click.exceptions import UsageError
from typer.core import TyperGroup

from autoclipper import __version__
from autoclipper.analyze import DEFAULT_SCORER
from autoclipper.config import ConfigError, get_settings
from autoclipper.eval.harness import (
    ReportExistsError,
    current_git_sha,
    default_run_id,
    evaluate_files,
)
from autoclipper.ffmpeg import FFmpegError
from autoclipper.labels.dataset import build_dataset as build_labeled_dataset
from autoclipper.logging import configure_logging
from autoclipper.synthetic import (
    OutputDirError,
    VideoOptions,
    default_output_dir,
    generate_dataset,
)

if TYPE_CHECKING:
    # Only needed to match the signatures overridden in _RootGroup.
    from typer._click import Context


# Report folder names: one path segment, safe on every OS.
RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")


class ExitCode(IntEnum):
    """Process exit codes, as defined in docs/development/conventions.md §7."""

    SUCCESS = 0
    # Bad command-line input: unknown command or option, invalid value, missing argument.
    USER_ERROR = 1
    # A network call or external tool (Twitch API, FFmpeg, TwitchDownloaderCLI) failed.
    EXTERNAL_FAILURE = 2
    # A bug (api-contract.md § 4.6): unexpected exceptions from the analyze pipeline.
    INTERNAL_ERROR = 3


class _RootGroup(TyperGroup):
    """Root command group that reports command-line usage errors as `ExitCode.USER_ERROR`.

    Typer exits with status 2 on usage errors, but the conventions reserve 2 for external
    failures. Every error Typer raises for bad input subclasses `UsageError`, both while parsing
    the root options (`make_context`) and while resolving and parsing a subcommand (`invoke`), so
    both entry points remap it before Typer prints the error and exits. Other exceptions,
    including Click exceptions a command raises with its own `exit_code`, pass through unchanged.
    """

    def make_context(
        self,
        info_name: str | None,
        args: list[str],
        parent: "Context | None" = None,
        **extra: Any,  # noqa: ANN401 - forwarded unchanged to Typer's own signature.
    ) -> "Context":
        try:
            return super().make_context(info_name, args, parent, **extra)
        except UsageError as exc:
            exc.exit_code = ExitCode.USER_ERROR
            raise

    def invoke(self, ctx: "Context") -> Any:  # noqa: ANN401 - returns whatever the command returns.
        try:
            return super().invoke(ctx)
        except UsageError as exc:
            exc.exit_code = ExitCode.USER_ERROR
            raise


app = typer.Typer(
    name="autoclipper",
    cls=_RootGroup,
    # Show the help instead of an error when run without a command (exit code USER_ERROR).
    no_args_is_help=True,
    # Shell-completion install options only clutter the help of a `uv run` tool.
    add_completion=False,
    # Local variables in tracebacks may hold tokens or chat data (conventions §6).
    pretty_exceptions_show_locals=False,
)


def _show_version(value: bool) -> None:
    if value:
        typer.echo(f"autoclipper {__version__}")
        raise typer.Exit(ExitCode.SUCCESS)


# A callback keeps `app` a command group even while it has zero or one subcommands, so adding
# subcommands never changes how existing invocations are parsed.
@app.callback()
def root(
    # Unused here: `_show_version` handles the flag before any command runs.
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_show_version,
            is_eager=True,
            help="Show the version and exit.",
        ),
    ] = False,
) -> None:
    """Find, rank and export the highlights of a long Twitch VOD."""
    # Runs before every subcommand (not for --help or --version), so all commands log the same
    # way. Invalid settings are a user error: report them without a traceback.
    try:
        configure_logging()
    except ConfigError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(ExitCode.USER_ERROR) from None


@app.command()
def synth(
    out: Annotated[
        Path | None,
        typer.Option(
            help="Directory to create. Default: <data dir>/synthetic/seed-<seed>.",
            show_default=False,
        ),
    ] = None,
    seed: Annotated[int, typer.Option(min=0, help="The same seed gives the same data.")] = 0,
    pilot: Annotated[
        bool, typer.Option(help="Generate six VODs from five fake streamers for dataset builds.")
    ] = False,
    video: Annotated[
        bool, typer.Option(help="Generate a test-pattern video per VOD (needs FFmpeg).")
    ] = True,
    video_duration: Annotated[
        float | None,
        typer.Option(min=0.1, help="Video length in seconds. Default: each VOD's duration."),
    ] = None,
    force: Annotated[
        bool, typer.Option(help="Replace a synthetic dataset already in the directory.")
    ] = False,
) -> None:
    """Generate a deterministic synthetic dataset of fake VODs, clips, chat, audio and video."""
    default = default_output_dir(get_settings().data_dir, seed)
    if pilot:
        default = default.with_name(f"pilot-seed-{seed}")
    out_dir = out if out is not None else default
    options = VideoOptions(duration_s=video_duration) if video else None
    try:
        dataset = generate_dataset(out_dir, seed=seed, pilot=pilot, video=options, force=force)
    except OutputDirError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(ExitCode.USER_ERROR) from None
    except FFmpegError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(ExitCode.EXTERNAL_FAILURE) from None
    typer.echo(f"Wrote {len(dataset.files)} files to {dataset.out_dir}")


@app.command()
def build_dataset(
    data_dir: Annotated[Path | None, typer.Option(help="Ingest/synth data root.")] = None,
    dataset_version: Annotated[str, typer.Option(help="New immutable dataset version.")] = "v0",
    seed: Annotated[
        int, typer.Option(min=0, help="Seed for streamer folds and review samples.")
    ] = 0,
    lexicon: Annotated[Path, typer.Option(help="Versioned chat lexicon.")] = Path(
        "configs/lexicon.yaml"
    ),
    snapshot: Annotated[
        list[str] | None, typer.Option(help="Pinned snapshot ID; repeat to select several.")
    ] = None,
) -> None:
    """Label windows, extract features and freeze five 3/1/1 streamer folds."""
    root = data_dir if data_dir is not None else get_settings().data_dir
    try:
        path = build_labeled_dataset(
            root,
            version=dataset_version,
            seed=seed,
            lexicon_path=lexicon,
            snapshot_ids=snapshot,
        )
    except (ValueError, OSError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(ExitCode.USER_ERROR) from None
    manifest = json.loads(path.read_text(encoding="utf-8"))
    typer.echo(json.dumps(manifest["quality"], indent=2, sort_keys=True))
    if manifest["quality"]["null_offset_alert"]:
        typer.echo("ALERT: more than 20% of clips lack a VOD offset", err=True)
    typer.echo(f"Saved dataset and frozen splits: {path}")


@app.command()
def evaluate(
    candidates: Annotated[
        Path, typer.Option(help="Parquet: video_id, candidate_id, start_s, end_s, score.")
    ],
    moments: Annotated[Path, typer.Option(help="Parquet: the moments table of the snapshot.")],
    clips: Annotated[Path, typer.Option(help="Parquet: the clips table of the same snapshot.")],
    dataset_version: Annotated[str, typer.Option(help="Dataset version, e.g. v1.")],
    scorer: Annotated[str, typer.Option(help="Scorer name, e.g. chat_spike.")],
    seed: Annotated[int, typer.Option(min=0, help="Seed for the bootstrap.")] = 0,
    run_id: Annotated[
        str | None,
        typer.Option(
            help="Report folder name. Default: eval-<scorer>-<UTC time>.", show_default=False
        ),
    ] = None,
    vods: Annotated[
        Path | None,
        typer.Option(
            help="Text file with the VOD IDs to evaluate (the split's test VODs), one per line. "
            "Default: every VOD with candidates.",
            show_default=False,
        ),
    ] = None,
    flagged: Annotated[
        Path | None,
        typer.Option(
            help="Text file with VOD IDs whose clip snapshot is flagged (left out of "
            "top-clip recall), one per line.",
            show_default=False,
        ),
    ] = None,
    split: Annotated[
        str | None,
        typer.Option(help="Split file and fold, recorded in the report.", show_default=False),
    ] = None,
    force: Annotated[bool, typer.Option(help="Replace an existing report.")] = False,
) -> None:
    """Score candidates against viewer-clip moments: hit rate@10 and top-clip recall@10."""
    if not RUN_ID.fullmatch(scorer) or _is_reserved_name(scorer):
        typer.echo("Error: --scorer must be a short name such as chat_spike", err=True)
        raise typer.Exit(ExitCode.USER_ERROR)
    name = run_id or default_run_id(scorer)
    if not RUN_ID.fullmatch(name) or _is_reserved_name(name):
        typer.echo(
            "Error: --run-id must start with a letter or digit and use only letters, digits, "
            "'.', '_' and '-'",
            err=True,
        )
        raise typer.Exit(ExitCode.USER_ERROR)
    out_dir = get_settings().artifacts_dir / "reports" / name
    try:
        video_ids = None if vods is None else _read_ids(vods)
        flagged_ids = () if flagged is None else _read_ids(flagged)
        result, metrics_path, _ = evaluate_files(
            candidates,
            moments,
            clips,
            out_dir=out_dir,
            run_id=name,
            dataset_version=dataset_version,
            scorer=scorer,
            seed=seed,
            git_sha=current_git_sha(),
            split=split,
            video_ids=video_ids,
            flagged_video_ids=flagged_ids,
            force=force,
        )
    except (FileNotFoundError, ReportExistsError, ValueError, UnicodeDecodeError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(ExitCode.USER_ERROR) from None
    for metric, interval in result.summary.items():
        shown = (
            "n/a"
            if interval is None
            else (f"{interval.mean:.3f} (95% CI {interval.low:.3f} to {interval.high:.3f})")
        )
        typer.echo(f"{metric}: {shown}")
    typer.echo(f"Wrote {metrics_path}")


@app.command()
def train(
    config: Annotated[Path, typer.Option(help="YAML training config (dataset, fold, model).")],
    run_id: Annotated[
        str | None,
        typer.Option(
            help="Model folder name. Default: train-<model>-<UTC time>.", show_default=False
        ),
    ] = None,
    force: Annotated[bool, typer.Option(help="Replace an existing run.")] = False,
) -> None:
    """Train logistic regression or LightGBM on a built dataset and save the run."""
    # Imported here: scikit-learn and LightGBM would slow down every other command.
    from autoclipper.models.train import RunExistsError, load_config
    from autoclipper.models.train import train as train_model

    try:
        settings = load_config(config)
        name = run_id or f"train-{settings.model}-{default_run_id('x').rsplit('-', 1)[1]}"
        if not RUN_ID.fullmatch(name) or _is_reserved_name(name):
            typer.echo(
                "Error: --run-id must start with a letter or digit and use only letters, "
                "digits, '.', '_' and '-'",
                err=True,
            )
            raise typer.Exit(ExitCode.USER_ERROR)
        runtime = get_settings()
        out = train_model(
            settings,
            data_dir=runtime.data_dir,
            artifacts_dir=runtime.artifacts_dir,
            run_id=name,
            force=force,
        )
    except (ValueError, OSError, RunExistsError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(ExitCode.USER_ERROR) from None
    run = json.loads((out / "run.json").read_text(encoding="utf-8"))
    for split, metrics in run["metrics"].items():
        ap, rate = metrics["average_precision"], metrics["positive_rate"]
        shown = (
            "n/a (needs positive and negative windows)"
            if ap is None
            else f"{ap:.3f} (chance {rate:.3f})"
        )
        typer.echo(f"{split} average precision: {shown}")
    typer.echo(f"Saved model run: {out}")


# Device names Windows refuses as file or folder names, with or without an extension.
_RESERVED_NAMES = frozenset(
    {
        "con",
        "prn",
        "aux",
        "nul",
        *(f"com{i}" for i in range(1, 10)),
        *(f"lpt{i}" for i in range(1, 10)),
    }
)


def _is_reserved_name(name: str) -> bool:
    return name.split(".")[0].lower() in _RESERVED_NAMES


def _read_ids(path: Path) -> list[str]:
    """Non-blank lines of a UTF-8 text file, stripped; '#' starts a comment line."""
    if not path.is_file():
        raise FileNotFoundError(f"input not found: {path}")
    lines = (line.strip() for line in path.read_text(encoding="utf-8").splitlines())
    return [line for line in lines if line and not line.startswith("#")]


class ExportLayout(StrEnum):
    """The export layouts of docs/architecture/api-contract.md § 6.1."""

    CENTER_CROP = "center_crop"
    CONFIGURED_REGION = "configured_region"
    STACKED = "stacked"


def _parse_region_option(value: str) -> tuple[float, float, float, float]:
    """Parse ``x,y,width,height`` normalized coordinates from a CLI flag."""
    parts = value.split(",")
    try:
        x, y, width, height = (float(part) for part in parts)
    except ValueError:
        raise ValueError(
            "expected four numbers x,y,width,height (fractions 0-1), e.g. 0.02,0.04,0.22,0.28"
        ) from None
    return (x, y, width, height)


@app.command()
def export(
    video: Annotated[
        Path, typer.Argument(help="The video file; it must start at the VOD's start.")
    ],
    start: Annotated[float, typer.Option(min=0.0, help="Start, seconds from the VOD start.")],
    end: Annotated[float, typer.Option(min=0.0, help="End (exclusive), seconds from the start.")],
    layout: Annotated[
        ExportLayout,
        typer.Option(
            help="center_crop, configured_region (AUTOCLIPPER_EXPORT_REGION), or "
            "stacked (facecam over gameplay)."
        ),
    ] = ExportLayout.CENTER_CROP,
    facecam_region: Annotated[
        str | None,
        typer.Option(
            help="Facecam x,y,width,height fractions (0-1) for --layout stacked; overrides "
            "AUTOCLIPPER_EXPORT_FACECAM_REGION and --channel.",
            show_default=False,
        ),
    ] = None,
    channel: Annotated[
        str | None,
        typer.Option(
            help="Pilot channel login from --channels (default configs/channels.yaml) for "
            "--layout stacked.",
            show_default=False,
        ),
    ] = None,
    channels: Annotated[
        Path,
        typer.Option(help="Channels YAML file with per-channel stacked_layout presets."),
    ] = Path("configs/channels.yaml"),
    captions: Annotated[
        bool,
        typer.Option(help="Burn in captions of the speech (needs `uv sync --extra captions`)."),
    ] = False,
    output: Annotated[
        Path | None,
        typer.Option(
            help="Where to write the MP4. Default: <artifacts dir>/exports/"
            "<video>_<start>-<end>.mp4.",
            show_default=False,
        ),
    ] = None,
    overwrite: Annotated[bool, typer.Option(help="Replace an existing output file.")] = False,
) -> None:
    """Cut an exact interval of a video into a vertical 1080x1920 clip, optionally captioned."""
    from autoclipper.export.captions import CaptioningError
    from autoclipper.export.clip import ClipSpec, render_clip
    from autoclipper.export.cut import Interval
    from autoclipper.export.errors import ExportError
    from autoclipper.export.reframe import stacked_layout_for_channel, stacked_layout_from_region

    if end <= start:
        typer.echo("Error: --end must be after --start", err=True)
        raise typer.Exit(ExitCode.USER_ERROR)
    settings = get_settings()
    stacked = None
    if facecam_region is not None:
        try:
            stacked = stacked_layout_from_region(_parse_region_option(facecam_region))
        except (ValueError, ExportError) as exc:
            typer.echo(f"Error: invalid --facecam-region: {exc}", err=True)
            raise typer.Exit(ExitCode.USER_ERROR) from None
    elif channel is not None:
        try:
            stacked = stacked_layout_for_channel(channel, channels_path=channels)
        except ExportError as exc:
            typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(ExitCode.USER_ERROR) from None
        if stacked is None:
            typer.echo(
                f"Error: no stacked_layout preset found for channel {channel!r} in {channels}",
                err=True,
            )
            raise typer.Exit(ExitCode.USER_ERROR)
    target = output or settings.artifacts_dir / "exports" / _export_name(video, start, end)
    spec = ClipSpec(
        source=video,
        interval=Interval(start, end),
        output=target,
        layout=layout.value,
        captions=captions,
        overwrite=overwrite,
        stacked_layout=stacked,
    )
    try:
        result = render_clip(
            spec,
            region=settings.export_region,
            facecam_region=settings.export_facecam_region,
        )
    except CaptioningError as exc:  # a missing install or a failing model: an external tool
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(ExitCode.EXTERNAL_FAILURE) from None
    except FileExistsError:
        typer.echo(f"Error: {target} exists; pass --overwrite to replace it", err=True)
        raise typer.Exit(ExitCode.USER_ERROR) from None
    except ExportError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(ExitCode.USER_ERROR) from None
    except FFmpegError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(ExitCode.EXTERNAL_FAILURE) from None
    typer.echo(f"Wrote {result.output} ({result.duration_s:.2f} s, {result.width}x{result.height})")


class HighlightsOrder(StrEnum):
    """The highlights video's segment orders (api-contract.md § 7.1)."""

    CHRONOLOGICAL = "chronological"
    RANK = "rank"


@app.command()
def highlights(
    result: Annotated[
        Path, typer.Argument(help="A saved AnalysisResult JSON (autoclipper analyze's output).")
    ],
    video: Annotated[
        Path,
        typer.Option(
            help="The VOD's video file; it must start at the VOD's start.",
            show_default=False,
        ),
    ],
    count: Annotated[int, typer.Option(min=1, max=20, help="Top candidates to include.")] = 10,
    order: Annotated[
        HighlightsOrder, typer.Option(help="chronological (stream order) or rank (best first).")
    ] = HighlightsOrder.CHRONOLOGICAL,
    captions: Annotated[
        bool, typer.Option(help="Burn in captions of each segment's speech.")
    ] = False,
    segments: Annotated[
        Path | None,
        typer.Option(
            help="A JSON file with a 'segments' list (candidate_id, start_s, end_s) instead of "
            "the result's own bounds.",
            show_default=False,
        ),
    ] = None,
    out: Annotated[
        Path | None,
        typer.Option(
            help="Where to write the MP4. Default: <artifacts dir>/highlights/"
            "autoclipper_<video_id>_highlights.mp4.",
            show_default=False,
        ),
    ] = None,
    overwrite: Annotated[bool, typer.Option(help="Replace an existing output file.")] = False,
) -> None:
    """Join an analysis result's top moments into one 16:9 highlights video."""
    import json

    from pydantic import ValidationError

    from autoclipper.analysis import AnalysisResult
    from autoclipper.export.captions import CaptioningError
    from autoclipper.export.errors import ExportError
    from autoclipper.export.highlights import (
        MAX_HIGHLIGHTS_SEGMENTS,
        HighlightsRequest,
        InvalidHighlightsBoundsError,
        Segment,
        render_highlights,
        resolve_segments,
    )

    if not result.is_file():
        typer.echo(f"Error: analysis result not found: {result}", err=True)
        raise typer.Exit(ExitCode.USER_ERROR)
    if not video.is_file():
        typer.echo(f"Error: video file not found: {video}", err=True)
        raise typer.Exit(ExitCode.USER_ERROR)
    try:
        analysis = AnalysisResult.model_validate_json(result.read_text(encoding="utf-8"))
    except ValidationError as exc:
        typer.echo(f"Error: {result} is not a valid analysis result: {exc}", err=True)
        raise typer.Exit(ExitCode.USER_ERROR) from None
    explicit: list[Segment] | None = None
    if segments is not None:
        try:
            loaded: object = json.loads(segments.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            typer.echo(f"Error: can't read {segments}: {exc}", err=True)
            raise typer.Exit(ExitCode.USER_ERROR) from None
        rows = loaded.get("segments") if isinstance(loaded, dict) else None
        if not isinstance(rows, list):
            typer.echo(f"Error: {segments} has no 'segments' list", err=True)
            raise typer.Exit(ExitCode.USER_ERROR)
        try:
            explicit = [
                Segment(str(row["candidate_id"]), float(row["start_s"]), float(row["end_s"]))
                for row in rows
            ]
        except (KeyError, TypeError, ValueError) as exc:
            typer.echo(
                f"Error: {segments} has a segment without candidate_id, start_s and end_s: {exc}",
                err=True,
            )
            raise typer.Exit(ExitCode.USER_ERROR) from None
        if not 1 <= len(explicit) <= MAX_HIGHLIGHTS_SEGMENTS:  # section 7.1's 1-20 items
            typer.echo(
                f"Error: {segments} has {len(explicit)} segments; a highlights video needs "
                f"1-{MAX_HIGHLIGHTS_SEGMENTS}",
                err=True,
            )
            raise typer.Exit(ExitCode.USER_ERROR)
    settings = get_settings()
    video_id = analysis.vod.video_id
    target = out or settings.artifacts_dir / "highlights" / f"autoclipper_{video_id}_highlights.mp4"
    try:
        chosen = resolve_segments(
            analysis,
            count=count,
            segments=explicit,
            order=order.value,  # type: ignore[arg-type]
        )
        from autoclipper.export.timeline import corrected_duration_s
        from autoclipper.ffmpeg import probe

        try:
            # The same video_mismatch check as the API's job worker (api-contract.md § 3.1).
            # ffprobe's container duration spans the first to the last raw timestamp, so a
            # source whose timestamps restart reports only its first span; a disagreement is
            # re-measured on the analysis timeline before the file is rejected.
            duration_s = probe(video).duration_s
            if abs(duration_s - analysis.vod.duration_s) > 2.0:
                duration_s = corrected_duration_s(video)
        except FFmpegError as exc:
            typer.echo(
                f"Error: invalid_video_path: {video} is not a readable video file: {exc}", err=True
            )
            raise typer.Exit(ExitCode.USER_ERROR) from None
        if abs(duration_s - analysis.vod.duration_s) > 2.0:
            typer.echo(
                f"Error: video_mismatch: {video} is {duration_s:.1f} s long but the VOD is "
                f"{analysis.vod.duration_s:.1f} s; pass a file of the whole VOD, starting at "
                "its start",
                err=True,
            )
            raise typer.Exit(ExitCode.USER_ERROR) from None
        request = HighlightsRequest(
            source=video,
            segments=chosen,
            output=target,
            order=order.value,  # type: ignore[arg-type]
            captions=captions,
            overwrite=overwrite,
        )
        rendered = render_highlights(request)
    except InvalidHighlightsBoundsError as exc:
        typer.echo(f"Error: invalid_highlights_bounds: {exc}", err=True)
        raise typer.Exit(ExitCode.USER_ERROR) from None
    except ValueError as exc:  # a segment naming a candidate that isn't in the result
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(ExitCode.USER_ERROR) from None
    except CaptioningError as exc:  # § 4.6's exit-1 codes
        typer.echo(f"Error: captioning_failed: {exc}", err=True)
        raise typer.Exit(ExitCode.USER_ERROR) from None
    except FileExistsError:
        typer.echo(f"Error: {target} exists; pass --overwrite to replace it", err=True)
        raise typer.Exit(ExitCode.USER_ERROR) from None
    except ExportError as exc:
        typer.echo(f"Error: render_failed: {exc}", err=True)
        raise typer.Exit(ExitCode.USER_ERROR) from None
    except FFmpegError as exc:
        typer.echo(f"Error: render_failed: {exc}", err=True)
        raise typer.Exit(ExitCode.USER_ERROR) from None
    except typer.Exit:
        raise  # a deliberate exit from one of the checks above, not a bug
    except Exception as exc:  # a bug: details to the log, message generic (§ 4.6)
        logging.getLogger(__name__).exception(
            "highlights failed unexpectedly result=%s", result, exc_info=exc
        )
        typer.echo(
            "Error: internal_error: something went wrong that shouldn't have; "
            "the log has the details",
            err=True,
        )
        raise typer.Exit(ExitCode.INTERNAL_ERROR) from None
    typer.echo(
        f"Wrote {rendered.output} ({rendered.duration_s:.2f} s, "
        f"{len(rendered.segments)} segments, {rendered.width}x{rendered.height})"
    )


def _export_name(video: Path, start: float, end: float) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", video.stem).strip("._") or "clip"
    # The floats as given (like the API's download name), never rounded: two trims of a long
    # VOD that differ by a fraction of a second get different names.
    return f"{stem}_{start}-{end}.mp4"


@app.command()
def ingest(
    vod: Annotated[str, typer.Argument(help="A Twitch VOD link or numeric video ID.")],
    media: Annotated[
        Path | None,
        typer.Option(
            help="A video or audio file of this VOD you already have; its audio is used instead "
            "of downloading. It must start at the VOD's start.",
            show_default=False,
        ),
    ] = None,
) -> None:
    """Download everything one VOD needs: metadata, viewer clips, chat and audio."""
    from autoclipper.config import MissingSettingError
    from autoclipper.ingest.twitch_api import HelixClient
    from autoclipper.ingest.vod import IngestTools, ingest_vod
    from autoclipper.schemas import parse_vod

    video_id = parse_vod(vod)
    if video_id is None:
        typer.echo("Error: that isn't a Twitch VOD link or video ID", err=True)
        raise typer.Exit(ExitCode.USER_ERROR)
    if media is not None and not media.is_file():
        typer.echo(f"Error: media file not found: {media}", err=True)
        raise typer.Exit(ExitCode.USER_ERROR)
    settings = get_settings()
    options: dict[str, Any] = {}
    try:
        options["salt"] = settings.require_chat_hash_salt()
    except MissingSettingError as exc:
        options["missing_salt"] = str(exc)
    helix: HelixClient | None = None
    try:
        helix = HelixClient(settings.require_twitch_credentials())
    except MissingSettingError as exc:
        options["missing_helix"] = str(exc)
    try:
        report = ingest_vod(
            video_id,
            data_dir=settings.data_dir,
            tools=IngestTools(helix=helix, **options),
            media_file=media,
        )
    finally:
        if helix is not None:
            helix.close()
    for step in report.steps:
        typer.echo(f"{step.step:>9}: {step.outcome} - {step.detail}")
    if report.failed:
        typer.echo("Some steps failed; run the same command again to retry them.", err=True)
        raise typer.Exit(ExitCode.EXTERNAL_FAILURE)


@app.command()
def analyze(
    vod: Annotated[str, typer.Argument(help="A Twitch VOD link or numeric video ID.")],
    video: Annotated[
        Path | None,
        typer.Option(
            help="A video file of this VOD you already have; its audio is used instead of "
            "downloading, and it is checked against the VOD's length.",
            show_default=False,
        ),
    ] = None,
    scorer: Annotated[
        str,
        typer.Option(
            help="Baseline scorer. The default (chat_loudness) degrades to whichever signal "
            "arrived; a named one (chat_spike, loudness, random) fails without its signal."
        ),
    ] = DEFAULT_SCORER,
    model: Annotated[
        Path | None,
        typer.Option(
            help="A trained model run (artifacts/models/<run_id>) to score with; overrides "
            "--scorer.",
            show_default=False,
        ),
    ] = None,
    lexicon: Annotated[Path, typer.Option(help="Versioned chat lexicon.")] = Path(
        "configs/lexicon.yaml"
    ),
    out: Annotated[
        Path | None,
        typer.Option(help="Where to write the result JSON.", show_default=False),
    ] = None,
) -> None:
    """Analyze one VOD: ingest it, score its windows and write the ranked candidates."""
    from autoclipper.analyze import AnalyzeError, analyze_vod, result_path
    from autoclipper.config import MissingSettingError
    from autoclipper.features.chat import LexiconError, load_lexicon
    from autoclipper.ingest.twitch_api import HelixClient
    from autoclipper.ingest.vod import IngestTools
    from autoclipper.models.base import ScorerError
    from autoclipper.schemas import parse_vod

    video_id = parse_vod(vod)
    if video_id is None:
        typer.echo("Error: invalid_vod_link: that isn't a Twitch VOD link or video ID", err=True)
        raise typer.Exit(ExitCode.USER_ERROR)
    if video is not None and not video.is_file():
        typer.echo(f"Error: invalid_video_path: video file not found: {video}", err=True)
        raise typer.Exit(ExitCode.USER_ERROR)
    if model is not None and not model.is_dir():
        typer.echo(f"Error: no trained model run at {model}", err=True)
        raise typer.Exit(ExitCode.USER_ERROR)
    settings = get_settings()
    try:
        lexicon_loaded = load_lexicon(lexicon)
    except LexiconError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(ExitCode.USER_ERROR) from None
    options: dict[str, Any] = {}
    try:
        options["salt"] = settings.require_chat_hash_salt()
    except MissingSettingError as exc:
        options["missing_salt"] = str(exc)
    helix: HelixClient | None = None
    try:
        helix = HelixClient(settings.require_twitch_credentials())
    except MissingSettingError as exc:
        options["missing_helix"] = str(exc)
    try:
        result = analyze_vod(
            video_id,
            data_dir=settings.data_dir,
            artifacts_dir=settings.artifacts_dir,
            tools=IngestTools(helix=helix, **options),
            lexicon=lexicon_loaded,
            video_file=video.resolve() if video is not None else None,
            baseline=scorer,
            model_dir=model,
            out_path=out,
        )
    except AnalyzeError as exc:
        typer.echo(f"Error: {exc}", err=True)
        code = (
            ExitCode.EXTERNAL_FAILURE if exc.code == "upstream_unavailable" else ExitCode.USER_ERROR
        )
        raise typer.Exit(code) from None
    except ScorerError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(ExitCode.USER_ERROR) from None
    except Exception as exc:  # a bug: details to the log, message generic (§ 4.6)
        log = logging.getLogger(__name__)
        log.exception("analyze failed unexpectedly video_id=%s", video_id, exc_info=exc)
        typer.echo(
            "Error: internal_error: something went wrong that shouldn't have; "
            "the log has the details",
            err=True,
        )
        raise typer.Exit(ExitCode.INTERNAL_ERROR) from None
    finally:
        if helix is not None:
            helix.close()
    for warning in result.warnings:
        typer.echo(f"Warning: {warning.code}: {warning.message}", err=True)
    top = result.candidates[0] if result.candidates else None
    best = f", best at {top.start_s:.0f}-{top.end_s:.0f} s" if top is not None else ""
    typer.echo(
        f"Analyzed {video_id} with {result.scorer.name}: {len(result.candidates)} candidates{best}"
    )
    typer.echo(f"Wrote {result_path(settings.artifacts_dir, video_id, out)}")


class JobRunner(StrEnum):
    """Who runs analysis jobs (api-contract.md § 9.5)."""

    MOCK = "mock"
    PIPELINE = "pipeline"


class SetupToolOption(StrEnum):
    """Tool names accepted by ``autoclipper setup-tools --tool``."""

    FFMPEG = "ffmpeg"
    TWITCH_DOWNLOADER = "twitch-downloader"


def _format_mb(n_bytes: int) -> str:
    return f"{n_bytes / (1024 * 1024):.1f} MB"


@app.command(name="setup-tools")
def setup_tools(
    tool: Annotated[
        list[SetupToolOption] | None,
        typer.Option(
            "--tool",
            help="Install only specific tool(s) (repeat to select several). Default: all.",
            show_default=False,
        ),
    ] = None,
    force: Annotated[
        bool,
        typer.Option(help="Re-download and reinstall even if pinned tools are already installed."),
    ] = False,
    from_dir: Annotated[
        Path | None,
        typer.Option(
            help="Install offline from pre-downloaded archives in this directory "
            "(SHA-256 verified).",
            show_default=False,
        ),
    ] = None,
    tools_dir: Annotated[
        Path | None,
        typer.Option(
            help="Target managed tools directory. Default: AUTOCLIPPER_TOOLS_DIR or "
            "per-user folder.",
            show_default=False,
        ),
    ] = None,
    show_urls: Annotated[
        bool,
        typer.Option(
            "--show-urls",
            help="Print pinned release URLs, expected filenames, SHA-256 checksums, and licenses "
            "for this platform without downloading.",
        ),
    ] = False,
) -> None:
    """Download and verify pinned FFmpeg/FFprobe and TwitchDownloaderCLI builds."""
    from autoclipper.tools.discovery import effective_tools_dir
    from autoclipper.tools.installer import InstallProgress, ToolInstallError, install_tools
    from autoclipper.tools.manifest import (
        ALL_TOOL_GROUPS,
        ToolGroup,
        UnsupportedPlatformError,
        detect_platform,
        get_tool_spec,
    )

    selected: tuple[ToolGroup, ...] = (
        ALL_TOOL_GROUPS
        if not tool
        else tuple(
            "twitch_downloader" if t == SetupToolOption.TWITCH_DOWNLOADER else "ffmpeg"
            for t in tool
        )
    )

    if show_urls:
        try:
            plat = detect_platform()
            specs = [get_tool_spec(name, plat) for name in selected]
        except UnsupportedPlatformError as exc:
            typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(ExitCode.USER_ERROR) from None
        typer.echo(f"Pinned tool downloads for {plat.key}:")
        for spec in specs:
            typer.echo(f"  {spec.label} {spec.version} ({spec.license}) - {spec.source_name}")
            for artifact in spec.artifacts:
                typer.echo(f"    file:   {artifact.filename}")
                typer.echo(f"    url:    {artifact.url}")
                typer.echo(f"    sha256: {artifact.sha256}")
        return

    if from_dir is not None and not from_dir.is_dir():
        typer.echo(f"Error: offline archive directory not found: {from_dir}", err=True)
        raise typer.Exit(ExitCode.USER_ERROR)

    settings = get_settings()
    try:
        target_dir = (
            tools_dir.expanduser().absolute()
            if tools_dir is not None
            else effective_tools_dir(settings)
        )
    except RuntimeError as exc:
        typer.echo(
            f"Error: could not determine default tools directory ({exc}); pass --tools-dir or set "
            "AUTOCLIPPER_TOOLS_DIR.",
            err=True,
        )
        raise typer.Exit(ExitCode.USER_ERROR) from None
    if target_dir.exists() and not target_dir.is_dir():
        typer.echo(
            f"Error: --tools-dir must be a directory path, not an existing file: {target_dir}",
            err=True,
        )
        raise typer.Exit(ExitCode.USER_ERROR)

    last_stage_key: tuple[str, str] | None = None
    last_pct = -1

    def _on_progress(event: InstallProgress) -> None:
        nonlocal last_stage_key, last_pct
        key = (event.tool, event.stage)
        if event.stage == "downloading" and event.bytes_downloaded is not None:
            if event.fraction is not None:
                pct = int(event.fraction * 100)
                if key == last_stage_key and pct < 100 and pct - last_pct < 10:
                    return
                last_stage_key = key
                last_pct = pct
                total_text = _format_mb(event.bytes_total) if event.bytes_total else "?"
                typer.echo(
                    f"  [{event.label}] {event.message} "
                    f"{_format_mb(event.bytes_downloaded)} / {total_text} ({pct}%)"
                )
            elif key != last_stage_key:
                last_stage_key = key
                typer.echo(f"  [{event.label}] {event.message}")
            return
        if key != last_stage_key:
            last_stage_key = key
            last_pct = -1
            typer.echo(f"  [{event.label}] {event.message}")

    typer.echo(f"Setting up managed tools in {target_dir}...")
    try:
        outcomes = install_tools(
            selected,
            tools_dir=target_dir,
            force=force,
            archive_dir=from_dir,
            on_progress=_on_progress,
        )
    except UnsupportedPlatformError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(ExitCode.USER_ERROR) from None
    except ToolInstallError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(ExitCode.EXTERNAL_FAILURE) from None

    typer.echo("Managed tools summary:")
    for outcome in outcomes:
        state_text = "installed" if outcome.status == "installed" else "already installed"
        typer.echo(
            f"  [OK] {outcome.label} {outcome.version} ({state_text}, {outcome.license}, "
            f"{outcome.source_name})"
        )
        for binary in outcome.binaries:
            typer.echo(f"       -> {binary}")


@app.command()
def doctor(
    jobs: Annotated[
        JobRunner,
        typer.Option(
            help="Check readiness for pipeline (real VODs) or mock (built-in scenarios) jobs."
        ),
    ] = JobRunner.PIPELINE,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print the preflight report as JSON."),
    ] = False,
) -> None:
    """Check local tools, optional extras and credentials before running AutoClipper."""
    from autoclipper.preflight import (
        format_preflight_summary,
        format_tool_locations,
        run_preflight,
    )

    settings = get_settings()
    report = run_preflight(settings, job_runner=jobs.value)
    if json_output:
        typer.echo(report.model_dump_json(indent=2))
    else:
        typer.echo(format_preflight_summary(report))
        typer.echo(format_tool_locations(settings))
    if report.status == "fail":
        raise typer.Exit(ExitCode.USER_ERROR)


@app.command()
def serve(
    host: Annotated[str, typer.Option(help="Address to listen on.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(min=1, max=65535, help="Port to listen on.")] = 8000,
    step_s: Annotated[
        float, typer.Option(min=0.0, help="Seconds each mock job or render stage takes.")
    ] = 0.5,
    jobs: Annotated[
        JobRunner,
        typer.Option(
            help="mock runs the built-in scenarios (frontend work, no credentials); "
            "pipeline runs the real analysis per job."
        ),
    ] = JobRunner.PIPELINE,
    lexicon: Annotated[Path, typer.Option(help="Versioned chat lexicon (pipeline jobs).")] = Path(
        "configs/lexicon.yaml"
    ),
    channels: Annotated[
        Path,
        typer.Option(help="Channels YAML file with per-channel stacked_layout presets."),
    ] = Path("configs/channels.yaml"),
) -> None:
    """Run the backend API (open /docs for the endpoints)."""
    import uvicorn  # Deferred: only this command needs the web server.

    from autoclipper.api.app import create_app
    from autoclipper.api.service import MockService, ensure_sample_media
    from autoclipper.preflight import format_preflight_summary, run_preflight

    settings = get_settings()
    report = run_preflight(settings, job_runner=jobs.value)
    typer.echo(format_preflight_summary(report))
    typer.echo("Preparing the mock sample videos (first run takes a few seconds)...")
    try:
        samples = ensure_sample_media(settings.artifacts_dir / "mock-media")
    except FFmpegError as exc:
        typer.echo(f"Error: can't generate the sample videos: {exc}", err=True)
        raise typer.Exit(ExitCode.EXTERNAL_FAILURE) from None
    options: dict[str, Any] = {"job_runner": jobs.value, "step_s": step_s}
    if jobs == JobRunner.PIPELINE:
        from autoclipper.config import MissingSettingError
        from autoclipper.features.chat import LexiconError, load_lexicon
        from autoclipper.ingest.twitch_api import HelixClient
        from autoclipper.ingest.vod import IngestTools

        try:
            options["lexicon"] = load_lexicon(lexicon)
        except LexiconError as exc:
            typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(ExitCode.USER_ERROR) from None

        def tools_for() -> IngestTools:
            # One client per job: the worker closes it when the job ends.
            built: dict[str, Any] = {}
            try:
                built["salt"] = settings.require_chat_hash_salt()
            except MissingSettingError as exc:
                built["missing_salt"] = str(exc)
            helix: HelixClient | None = None
            try:
                helix = HelixClient(settings.require_twitch_credentials())
            except MissingSettingError as exc:
                built["missing_helix"] = str(exc)
            return IngestTools(helix=helix, **built)

        from autoclipper.ingest.media import VideoSegment, download_video_segment, raw_video_dir

        segments = raw_video_dir(settings.data_dir)

        def fetch_segment(video_id: str, begin_s: float, end_s: float, /) -> VideoSegment:
            # The fetcher's own cache (by video and range) makes repeat requests cheap (#46).
            return download_video_segment(
                video_id, begin_s=begin_s, end_s=end_s, output_dir=segments / video_id
            )

        options.update(
            data_dir=settings.data_dir,
            artifacts_dir=settings.artifacts_dir,
            tools_for=tools_for,
            segment_fetcher=fetch_segment,
        )
    from autoclipper.export.reframe import load_channel_stacked_layouts

    service = MockService(
        samples=samples,
        output_dir=settings.artifacts_dir,
        export_region=settings.export_region,
        export_facecam_region=settings.export_facecam_region,
        channel_layouts=load_channel_stacked_layouts(channels),
        **options,
    )
    api = create_app(
        service,
        cors_origins=settings.cors_origins,
        preflight=lambda: run_preflight(settings, job_runner=jobs.value),
    )
    typer.echo(f"AutoClipper API ({jobs.value} jobs) on http://{host}:{port} (docs at /docs)")
    uvicorn.run(api, host=host, port=port, log_level="warning")


@app.command(name="audio-events")
def audio_events(
    wav: Annotated[
        Path,
        typer.Argument(help="Normalized 16 kHz mono 16-bit WAV for one VOD."),
    ],
    windows: Annotated[
        Path,
        typer.Option(help="Parquet file with the VOD's windows table.", show_default=False),
    ],
    scores: Annotated[
        Path | None,
        typer.Option(
            help="Optional Parquet file with window_id and score columns to score only the top "
            "windows.",
            show_default=False,
        ),
    ] = None,
    top_k: Annotated[
        int | None,
        typer.Option(
            min=1,
            help="Number of top-scoring windows to run when --scores is given "
            "(default: AUTOCLIPPER_AUDIO_EVENTS_TOP_K, 100).",
            show_default=False,
        ),
    ] = None,
    out: Annotated[
        Path | None,
        typer.Option(
            help="Where to write the Parquet table. Default: <artifacts dir>/features/"
            "<video_id>_audio_events.parquet.",
            show_default=False,
        ),
    ] = None,
    cache_dir: Annotated[
        Path | None,
        typer.Option(
            help="Directory for cached YAMNet ONNX weights. Default: <artifacts dir>/cache/"
            "audio_events.",
            show_default=False,
        ),
    ] = None,
    force: Annotated[bool, typer.Option(help="Replace an existing output Parquet file.")] = False,
) -> None:
    """Extract opt-in YAMNet audio-event probabilities (audio_evt_*) per window."""
    import tempfile

    import polars as pl

    from autoclipper.features.audio import AudioFormatError
    from autoclipper.features.audio_events import (
        AUDIO_EVENTS_FEATURE_VERSION,
        AudioEventsError,
        extract_audio_event_features,
    )

    if top_k is not None and scores is None:
        typer.echo("Error: --top-k requires --scores", err=True)
        raise typer.Exit(ExitCode.USER_ERROR)
    if not wav.is_file():
        typer.echo(f"Error: WAV file not found: {wav}", err=True)
        raise typer.Exit(ExitCode.USER_ERROR)
    if not windows.is_file():
        typer.echo(f"Error: windows Parquet not found: {windows}", err=True)
        raise typer.Exit(ExitCode.USER_ERROR)
    if scores is not None and not scores.is_file():
        typer.echo(f"Error: scores Parquet not found: {scores}", err=True)
        raise typer.Exit(ExitCode.USER_ERROR)

    settings = get_settings()
    try:
        windows_df = pl.read_parquet(windows)
        scores_df = pl.read_parquet(scores) if scores is not None else None
    except Exception as exc:
        typer.echo(f"Error: can't read input Parquet: {exc}", err=True)
        raise typer.Exit(ExitCode.USER_ERROR) from None

    if out is not None:
        target = out
    else:
        video_id = (
            str(windows_df["video_id"][0])
            if "video_id" in windows_df.columns and windows_df.height > 0
            else wav.stem
        )
        if not RUN_ID.fullmatch(video_id) or _is_reserved_name(video_id):
            typer.echo(
                f"Error: unsafe video_id {video_id!r} for default output filename; pass --out",
                err=True,
            )
            raise typer.Exit(ExitCode.USER_ERROR)
        target = settings.artifacts_dir / "features" / f"{video_id}_audio_events.parquet"
    if target.exists() and not force:
        typer.echo(f"Error: {target} exists; pass --force to replace it", err=True)
        raise typer.Exit(ExitCode.USER_ERROR)

    effective_top_k = (
        (top_k if top_k is not None else settings.audio_events_top_k)
        if scores_df is not None
        else None
    )
    resolved_cache = (
        cache_dir if cache_dir is not None else settings.artifacts_dir / "cache" / "audio_events"
    )
    total_hint = (
        min(windows_df.height, effective_top_k)
        if effective_top_k is not None
        else windows_df.height
    )
    try:
        with typer.progressbar(length=max(total_hint, 1), label="Scoring audio events") as bar:
            last_done = 0

            def _advance(done: int, total: int) -> None:
                nonlocal last_done
                bar.length = max(total, 1)
                step = max(0, done - last_done)
                if step:
                    bar.update(step)
                    last_done = done

            extraction = extract_audio_event_features(
                windows_df,
                wav,
                scores=scores_df,
                top_k=effective_top_k,
                cache_dir=resolved_cache,
                on_progress=_advance,
            )
    except (ValueError, AudioFormatError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(ExitCode.USER_ERROR) from None
    except AudioEventsError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(ExitCode.EXTERNAL_FAILURE) from None

    tmp_path: Path | None = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            dir=target.parent, prefix=f".{target.name}.", suffix=".tmp", delete=False
        ) as tmp:
            tmp_path = Path(tmp.name)
        extraction.features.write_parquet(tmp_path)
        tmp_path.replace(target)
        tmp_path = None
    except OSError as exc:
        typer.echo(f"Error: can't write {target}: {exc}", err=True)
        raise typer.Exit(ExitCode.USER_ERROR) from None
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)

    empty_note = (
        f", {extraction.empty_audio_windows} past audio end"
        if extraction.empty_audio_windows
        else ""
    )
    typer.echo(
        f"Wrote {target} ({extraction.scored_windows} of {extraction.features.height} windows "
        f"scored{empty_note}, feature_version={AUDIO_EVENTS_FEATURE_VERSION})"
    )
