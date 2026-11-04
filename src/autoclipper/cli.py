"""The ``autoclipper`` command-line interface.

The CLI is a thin layer over the library (docs/development/conventions.md §7): a subcommand
parses its arguments, calls one library function and turns failures into an `ExitCode`.
Subcommands are verbs (``ingest``, ``export``, ...) registered on `app` with ``@app.command()``.
"""

import re
from enum import IntEnum
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
    out_dir = out if out is not None else default_output_dir(get_settings().data_dir, seed)
    options = VideoOptions(duration_s=video_duration) if video else None
    try:
        dataset = generate_dataset(out_dir, seed=seed, video=options, force=force)
    except OutputDirError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(ExitCode.USER_ERROR) from None
    except FFmpegError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(ExitCode.EXTERNAL_FAILURE) from None
    typer.echo(f"Wrote {len(dataset.files)} files to {dataset.out_dir}")


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
    force: Annotated[bool, typer.Option(help="Replace an existing report.")] = False,
) -> None:
    """Score candidates against viewer-clip moments: hit rate@10 and top-clip recall@10."""
    name = run_id or default_run_id(scorer)
    if not RUN_ID.fullmatch(name):
        typer.echo(
            "Error: --run-id must start with a letter or digit and use only letters, digits, "
            "'.', '_' and '-'",
            err=True,
        )
        raise typer.Exit(ExitCode.USER_ERROR)
    out_dir = get_settings().artifacts_dir / "reports" / name
    try:
        result, metrics_path, _ = evaluate_files(
            candidates,
            moments,
            clips,
            out_dir=out_dir,
            run_id=name,
            dataset_version=dataset_version,
            scorer=scorer,
            seed=seed,
            git_sha=current_git_sha(Path.cwd()),
            force=force,
        )
    except (FileNotFoundError, ReportExistsError, ValueError) as exc:
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
