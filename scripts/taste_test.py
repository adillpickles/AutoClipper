"""Blind taste test of two methods' clips (#69, docs/ml/evaluation.md § 7).

``build`` pairs two analysis results per VOD (``--ours`` and ``--baseline``, repeated once per
VOD, each with its ``--video``), renders the clips (or reuses earlier renders), shuffles and
anonymizes the pairs and writes ``<artifacts>/taste-tests/<test_id>/``:

- ``page/``: the rating page (``index.html``) and the clips as ``media/pair-NN-a.mp4``. Zip and
  send this folder to raters; they open ``index.html`` in a browser and send back the JSON
  file it downloads.
- ``key.json``: which method is A and B in every pair. Private: never send it to raters.
Clips are rendered into the shared cache ``<artifacts>/taste-tests/clips/`` and reused by later
builds with the same interval, layout and captions.

``score`` reads the key and the answer files and writes ``summary.json``, ``summary.md`` and
``ratings.csv`` (no names, no notes) to an ``experiments/`` folder; notes go to ``notes.md`` next
to the key. Everything under ``artifacts/`` is git-ignored.

Usage::

    uv run python scripts/taste_test.py build --ours artifacts/analysis/<id>.json \
        --baseline artifacts/analysis/<id>-chat_spike.json --video data/raw/video/<id>.mp4 \
        --test-id tt-1 --seed 7
    uv run python scripts/taste_test.py score --key artifacts/taste-tests/tt-1/key.json \
        --answers artifacts/taste-tests/tt-1/answers --out experiments/exp-NNN-blind-taste-test
"""

import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, cast

import typer
from pydantic import ValidationError

from autoclipper.analysis import AnalysisResult
from autoclipper.cli import ExitCode
from autoclipper.config import ConfigError, get_settings
from autoclipper.eval import taste_test as tt
from autoclipper.eval.harness import current_git_sha
from autoclipper.logging import configure_logging

app = typer.Typer(add_completion=False, help=__doc__.split("\n\n")[0] if __doc__ else None)

_TEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def _fail(message: str) -> typer.Exit:
    typer.echo(f"Error: {message}", err=True)
    return typer.Exit(ExitCode.USER_ERROR)


def _read_result(path: Path) -> AnalysisResult:
    try:
        return AnalysisResult.model_validate_json(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise _fail(f"can't read {path}: {exc}") from None
    except ValidationError as exc:
        raise _fail(f"{path} isn't an analysis result: {exc}") from None


def _clip_renderer(layout: str, captions: bool) -> tt.Renderer:
    """Renders clips exactly like ``autoclipper export`` with the given layout and captions."""

    def render(video: Path, start_s: float, end_s: float, output: Path) -> None:
        from autoclipper.export.clip import ClipSpec, LayoutName, render_clip
        from autoclipper.export.cut import Interval
        from autoclipper.export.facecam import FacecamCache
        from autoclipper.export.facecam import detect_facecam as find_facecam

        settings = get_settings()
        cache = FacecamCache(settings.artifacts_dir / "cache" / "facecam")
        spec = ClipSpec(
            source=video,
            interval=Interval(start_s, end_s),
            output=output,
            layout=cast(LayoutName, layout),  # checked in `build`
            captions=captions,
        )
        render_clip(
            spec,
            region=settings.export_region,
            facecam_region=settings.export_facecam_region,
            facecam_detector=lambda source, interval: find_facecam(source, interval, cache=cache),
        )
        typer.echo(f"Rendered {output.name}")

    return render


@app.command()
def build(
    ours: Annotated[
        list[Path], typer.Option(help="Analysis result of the method under test; once per VOD.")
    ],
    baseline: Annotated[
        list[Path], typer.Option(help="Analysis result it is compared with, in --ours' order.")
    ],
    video: Annotated[
        list[Path] | None,
        typer.Option(
            help="Each VOD's video file, in --ours' order. Leave out only when every clip is "
            "already in the clip cache.",
            show_default=False,
        ),
    ] = None,
    test_id: Annotated[
        str | None,
        typer.Option(
            help="Name shown to raters; don't hint at the methods. Default: tt-<date>-<seed>.",
            show_default=False,
        ),
    ] = None,
    seed: Annotated[int, typer.Option(help="Seeds the pair order and the A/B sides.")] = 0,
    clips: Annotated[
        int, typer.Option(min=1, help="Top candidates of each result to consider.")
    ] = tt.DEFAULT_CLIPS_PER_METHOD,
    shared_iou: Annotated[
        float,
        typer.Option(min=0.0, max=1.0, help="IoU at which two candidates are the same moment."),
    ] = tt.SHARED_IOU,
    layout: Annotated[
        str, typer.Option(help="center_crop, configured_region or stacked (as export).")
    ] = "center_crop",
    captions: Annotated[bool, typer.Option(help="Burn in captions on every clip.")] = False,
    clip_cache: Annotated[
        Path | None,
        typer.Option(
            help="Render cache. Default: <artifacts>/taste-tests/clips (shared by tests).",
            show_default=False,
        ),
    ] = None,
    out: Annotated[
        Path | None,
        typer.Option(
            help="The test's folder. Default: <artifacts>/taste-tests/<test_id>.",
            show_default=False,
        ),
    ] = None,
) -> None:
    """Pair, render and anonymize two methods' clips and write the rating page and the key."""
    configure_logging()
    if len(ours) != len(baseline):
        raise _fail("give one --baseline per --ours")
    videos = video or []
    if videos and len(videos) != len(ours):
        raise _fail("give one --video per --ours, or none")
    if layout not in ("center_crop", "configured_region", "stacked"):
        raise _fail(f"unknown layout {layout!r}")
    name = test_id or f"tt-{datetime.now(UTC):%Y%m%d}-{seed}"
    if not _TEST_ID.match(name):
        raise _fail("--test-id may use letters, digits, '-' and '_' (at most 64)")
    try:
        settings = get_settings()
    except ConfigError as exc:
        raise _fail(str(exc)) from None
    root = settings.artifacts_dir / "taste-tests"
    inputs = [
        tt.MethodPair(
            ours=_read_result(o),
            baseline=_read_result(b),
            video=videos[i] if videos else None,
            ours_path=o.as_posix(),
            baseline_path=b.as_posix(),
        )
        for i, (o, b) in enumerate(zip(ours, baseline, strict=True))
    ]
    options = tt.BuildOptions(
        test_id=name,
        seed=seed,
        clips_per_method=clips,
        shared_iou=shared_iou,
        layout=layout,
        captions=captions,
    )
    try:
        built = tt.build_test(
            inputs,
            out or root / name,
            options,
            renderer=_clip_renderer(layout, captions) if videos else None,
            clip_cache=clip_cache or root / "clips",
            git_sha=current_git_sha(),
        )
    except tt.TasteTestError as exc:
        raise _fail(str(exc)) from None
    typer.echo(
        f"{built.n_pairs} pairs ({built.n_shared} shared moments left out; "
        f"{built.n_rendered} clips rendered, {built.n_reused} reused)"
    )
    typer.echo(f"Rating page: {built.page}  (send the whole {built.page.parent.name}/ folder)")
    typer.echo(f"Private key: {built.key}  (don't send this)")


@app.command()
def score(
    key: Annotated[Path, typer.Option(help="The test's key.json.")],
    answers: Annotated[
        list[Path], typer.Option(help="Answer files or folders of them; repeatable.")
    ],
    out: Annotated[Path, typer.Option(help="Where to write the summary, e.g. experiments/<id>.")],
    seed: Annotated[
        int | None,
        typer.Option(help="Bootstrap seed. Default: the key's seed.", show_default=False),
    ] = None,
) -> None:
    """Report each comparison's win rate with intervals from the raters' answer files."""
    configure_logging()
    try:
        scored = tt.score_test(key, answers, out, seed=seed, git_sha=current_git_sha())
    except tt.TasteTestError as exc:
        raise _fail(str(exc)) from None
    typer.echo(tt.summary_markdown(scored.summary))
    typer.echo(
        f"Wrote {scored.summary_path}, {scored.markdown_path.name}, {scored.ratings_path.name}"
    )
    if scored.notes_path is not None:
        typer.echo(f"Rater notes (keep private): {scored.notes_path}")


if __name__ == "__main__":
    sys.exit(app())
