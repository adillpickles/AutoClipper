"""The ``autoclipper`` command-line interface.

The CLI is a thin layer over the library (docs/development/conventions.md §7): a subcommand
parses its arguments, calls one library function and turns failures into an `ExitCode`.
Subcommands are verbs (``ingest``, ``export``, ...) registered on `app` with ``@app.command()``.
"""

import json
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


@app.command()
def export(
    video: Annotated[
        Path, typer.Argument(help="The video file; it must start at the VOD's start.")
    ],
    start: Annotated[float, typer.Option(min=0.0, help="Start, seconds from the VOD start.")],
    end: Annotated[float, typer.Option(min=0.0, help="End (exclusive), seconds from the start.")],
    layout: Annotated[
        ExportLayout,
        typer.Option(help="center_crop, or configured_region (AUTOCLIPPER_EXPORT_REGION)."),
    ] = ExportLayout.CENTER_CROP,
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

    if end <= start:
        typer.echo("Error: --end must be after --start", err=True)
        raise typer.Exit(ExitCode.USER_ERROR)
    settings = get_settings()
    target = output or settings.artifacts_dir / "exports" / _export_name(video, start, end)
    spec = ClipSpec(
        source=video,
        interval=Interval(start, end),
        output=target,
        layout=layout.value,
        captions=captions,
        overwrite=overwrite,
    )
    try:
        result = render_clip(spec, region=settings.export_region)
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
def serve(
    host: Annotated[str, typer.Option(help="Address to listen on.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(min=1, max=65535, help="Port to listen on.")] = 8000,
    step_s: Annotated[
        float, typer.Option(min=0.0, help="Seconds each mock job or render stage takes.")
    ] = 0.5,
) -> None:
    """Run the backend API with the mock job runner (open /docs for the endpoints)."""
    import uvicorn  # Deferred: only this command needs the web server.

    from autoclipper.api.app import create_app
    from autoclipper.api.service import MockService, ensure_sample_media

    settings = get_settings()
    typer.echo("Preparing the mock sample videos (first run takes a few seconds)...")
    try:
        samples = ensure_sample_media(settings.artifacts_dir / "mock-media")
    except FFmpegError as exc:
        typer.echo(f"Error: can't generate the sample videos: {exc}", err=True)
        raise typer.Exit(ExitCode.EXTERNAL_FAILURE) from None
    service = MockService(
        samples=samples,
        output_dir=settings.artifacts_dir,
        step_s=step_s,
        export_region=settings.export_region,
    )
    api = create_app(service, cors_origins=settings.cors_origins)
    typer.echo(f"AutoClipper API (mock) on http://{host}:{port} (docs at /docs)")
    uvicorn.run(api, host=host, port=port, log_level="warning")
