"""The analyze pipeline: one VOD in, ranked candidate moments out (#44).

`analyze_vod` runs everything for one VOD and returns — and writes — the contract's
`AnalysisResult` (docs/architecture/api-contract.md § 4), the same JSON ``autoclipper
analyze`` writes and the API will return (#45):

1. **Ingest** (`ingest_vod`): metadata, the viewer-clip snapshot, chat and audio; resumable,
   recorded in the catalog. A video file the user already has (`video_file`) replaces the
   audio download. The chosen scorer is validated first, and the VOD's type and the file's
   length against the VOD's (the contract's ``video_mismatch``, 2 s, § 3.1) are checked in
   ``ingest_vod``'s ``after_vod_meta`` hook, at the ``resolving_vod`` stage — before
   anything is downloaded, so a rejected video or scorer costs nothing and leaves nothing.
2. **Inputs as warnings** (§ 3.5): a signal that couldn't be obtained degrades the result
   with `chat_missing`, `audio_missing` or `viewer_clips_missing` (the clip list being
   incomplete adds `viewer_clips_incomplete`), and Twitch having muted part of the audio
   adds `audio_partly_muted`. The analysis carries on with what arrived. The VOD itself
   being unresolvable or not a past broadcast fails with the contract's codes, and so does
   media Twitch refuses to serve without a user-provided file (deleted, expired or
   subscriber-only: `vod_unavailable`); nothing to rank fails with `no_usable_signals`, or
   `upstream_unavailable` when an input the ranking needs failed transiently.
3. **Features**: the VOD's windows, then its chat and audio features per window.
4. **Scoring**: a baseline by name (`baseline`, default ``chat_loudness``) or a saved model
   run (`model_dir`, `load_scorer`). The composite default degrades to whatever families
   arrived — ``loudness`` without chat, ``chat_spike`` without audio — and with neither,
   only viewer-clip moments rank, recorded as the ``no_signals`` scorer. A named
   single-signal baseline or a trained model that can't run fails with the § 3.5 codes
   (`upstream_unavailable` when an input it needs failed transiently, else
   `no_usable_signals`).
   With neither family and no viewer-clip moment, no scorer — ``random`` included — has
   anything to rank, and the § 3.5 precedence decides the code.
5. **Candidates** (`generate_candidates`): ranked, merged with the snapshot's moments.

How long each stage took is logged. The result JSON is written atomically to
``<artifacts_dir>/analysis/<video_id>.json`` (or `out_path`), where ``autoclipper
highlights`` (#71) and the frontend fixtures read it.
"""

import json
import logging
import math
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Literal

import polars as pl

from autoclipper.analysis import (
    MAX_CANDIDATES,
    AnalysisResult,
    AnalysisWarning,
    Candidate,
    ScorerInfo,
    VodSummary,
)
from autoclipper.export.timeline import corrected_duration_s
from autoclipper.features.audio import FEATURE_COLUMNS as AUDIO_FEATURE_COLUMNS
from autoclipper.features.audio import compute_audio_features
from autoclipper.features.chat import FEATURE_COLUMNS as CHAT_FEATURE_COLUMNS
from autoclipper.features.chat import Lexicon, compute_chat_features
from autoclipper.ffmpeg import FFmpegError, MediaInfo, Runner, probe
from autoclipper.ingest.catalog import CatalogEntry, read_catalog
from autoclipper.ingest.media import normalized_audio_path, validate_video_id
from autoclipper.ingest.twitch_api import Vod
from autoclipper.ingest.vod import (
    IngestTools,
    StepResult,
    chat_path,
    clips_dir,
    ingest_vod,
    read_vod,
)
from autoclipper.labels.moments import merge_clips_into_moments
from autoclipper.labels.windows import make_windows
from autoclipper.models.base import Scorer, ScorerError, load_scorer, make_scorer
from autoclipper.models.baselines import ChatLoudness, ChatSpike, Loudness
from autoclipper.models.candidates import MomentInput, generate_candidates

log = logging.getLogger(__name__)

DEFAULT_SCORER: Final = "chat_loudness"
"""The baseline used without --scorer or --model (the issue's chat + loudness default)."""

VIDEO_MISMATCH_TOLERANCE_S: Final = 2.0
"""How far a --video file's duration may be from the VOD's (api-contract.md § 3.1)."""

type FailureCode = Literal[
    "invalid_video_path",
    "vod_unavailable",
    "vod_unsupported",
    "video_mismatch",
    "no_usable_signals",
    "upstream_unavailable",
]
"""The failure codes of api-contract.md § 3.5 the CLI maps to exit codes (§ 4.6)."""

# The steps whose failure means an input was fetched but broke (§ 3.5: tool and network
# errors count as transient, so their failure makes a rankless result upstream_unavailable).
INPUT_STEPS: Final = ("clips", "chat", "audio")

# An ingest step's completion is the next analysis stage's beginning (api-contract.md § 3.4);
# a skipped step completes too, so cached stages are passed over. The audio step is absent:
# the inputs are classified between it and the features (an expired VOD fails the run before
# anything is computed), so `computing_features` begins at its explicit call below instead.
_STEP_TO_STAGE: Final = {
    "vod_meta": "fetching_clips",
    "clips": "downloading_chat",
    "chat": "downloading_audio",
}
"""The stage that begins when each ingest step completes (`on_step` in `ingest_vod`)."""


class AnalyzeError(Exception):
    """The analysis can't produce a meaningful result (api-contract.md § 3.5).

    Attributes:
        code: The contract's failure code; § 4.6 maps it to a CLI exit code (1 or 2).
        message: What to tell the user, without chat content.
    """

    def __init__(self, code: FailureCode, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def analyze_vod(
    video_id: str,
    *,
    data_dir: Path,
    artifacts_dir: Path,
    tools: IngestTools,
    lexicon: Lexicon,
    video_file: Path | None = None,
    baseline: str = DEFAULT_SCORER,
    model_dir: Path | None = None,
    out_path: Path | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(tz=UTC),
    on_stage: Callable[[str], None] | None = None,
    on_warnings: Callable[[Sequence[AnalysisWarning]], None] | None = None,
) -> AnalysisResult:
    """Run the whole pipeline for one VOD and write its ranked candidates.

    Args:
        video_id: The Twitch video ID.
        data_dir: The data directory the ingest steps read and write (``Settings.data_dir``).
        artifacts_dir: Where the result JSON goes (``analysis/<video_id>.json``).
        tools: The API client, salt and downloaders for the ingest steps (`IngestTools`).
        lexicon: From `load_lexicon`, for the chat features.
        video_file: A video file of this VOD the user already has; its audio is used instead
            of downloading, and it is checked like the API's ``video_path`` (§ 3.1).
        baseline: A scorer name from `BASELINES`. The default degrades to the families that
            arrived; a named single-signal baseline fails when its signal is missing.
        model_dir: A saved model run (``artifacts/models/<run_id>``); overrides `baseline`.
        out_path: Where to write the result JSON instead of the default under `artifacts_dir`.
        clock: Returns the current time as an aware UTC datetime (``generated_at``).
        on_stage: Called with each of the six analysis stage names of api-contract.md § 3.4
            as the stage begins, including stages the ingest skips over. An exception it
            raises propagates and fails the run. The API worker uses it for job progress.
        on_warnings: Called with the warnings found so far (§ 3.5), each time a new one is
            known: an input step that failed or was refused (while the next download still
            runs), then the full set once the inputs are classified. The API worker uses it
            so a running job's ``warnings`` field means "degradations found so far".

    Returns:
        The written `AnalysisResult`, ranked candidates first.

    Raises:
        ValueError: `video_id` isn't a Twitch video ID.
        AnalyzeError: The analysis failed with a contract code (§ 3.5); nothing is written.
        ScorerError: `baseline` isn't a known scorer name, or `model_dir` isn't a saved run.
    """
    validate_video_id(video_id)
    # The chosen scorer is validated before anything is downloaded: a bad name or model
    # directory is an input error, not something minutes of fetching should precede.
    chosen = load_scorer(model_dir) if model_dir is not None else make_scorer(baseline)
    del chosen  # _score_windows builds the scorer it runs; here it only had to be validated
    video_info = _probe_video_file(video_file, tools.runner)

    def stage(name: str) -> None:
        if on_stage is not None:
            on_stage(name)

    stage("resolving_vod")

    def check_vod(vod: Vod | None) -> None:
        """§ 3.5's `resolving_vod` checks, before any other step downloads anything."""
        if vod is None:
            return  # a failed or missing metadata step is classified after the ingest
        if vod.type != "archive":
            raise AnalyzeError(
                "vod_unsupported", f"the video is a {vod.type}, not a past broadcast"
            )
        if video_info is not None:
            assert video_file is not None
            # ffprobe's container duration spans the first to the last raw timestamp, so a
            # source whose timestamps restart reports only its first span; re-measure on the
            # analysis timeline only when the cheap ffprobe duration disagrees (#95).
            duration_s = video_info.duration_s
            if abs(duration_s - vod.duration_s) > VIDEO_MISMATCH_TOLERANCE_S:
                try:
                    duration_s = corrected_duration_s(video_file, runner=tools.runner)
                except FFmpegError as exc:
                    raise AnalyzeError(
                        "invalid_video_path",
                        f"{video_file} is not a readable video file: {exc}",
                    ) from None
            if abs(duration_s - vod.duration_s) > VIDEO_MISMATCH_TOLERANCE_S:
                raise AnalyzeError(
                    "video_mismatch",
                    f"{video_file} is {duration_s:.1f} s long but the VOD is "
                    f"{vod.duration_s:.1f} s; pass a file of the whole VOD, starting at its start",
                )

    # Warnings are published as they are found (§ 3.5's "found so far"), starting with the
    # input steps: a failed download is known while the next one still runs.
    warnings: list[AnalysisWarning] = []

    def publish_warnings() -> None:
        if on_warnings is not None:
            on_warnings(tuple(warnings))

    def add_warning(code: str, message: str) -> None:
        if all(warning.code != code for warning in warnings):
            warnings.append(AnalysisWarning(code=code, message=message))
            publish_warnings()

    def step_stage(step: StepResult) -> None:
        # The next analysis stage begins when an ingest step completes (a skipped step
        # completes too, so cached stages are passed over, § 3.4).
        if step.step in _STEP_TO_STAGE:
            stage(_STEP_TO_STAGE[step.step])
        # An input that didn't arrive is a degradation the job can already report (§ 3.5),
        # published before the next download runs. A skipped step never publishes: it stands
        # for an earlier ok (nothing to report) or an earlier unavailable (the classification
        # follows in moments, with no long download left to hide behind).
        if step.step in ("chat", "audio") and step.outcome in ("failed", "unavailable"):
            add_warning(
                f"{step.step}_missing", _missing_message(step.step, step.outcome, step.detail)
            )

    _timed(
        "ingest",
        lambda: ingest_vod(
            video_id,
            data_dir=data_dir,
            tools=tools,
            media_file=video_file,
            after_vod_meta=check_vod,
            on_step=None if (on_stage is None and on_warnings is None) else step_stage,
        ),
    )
    catalog = read_catalog(data_dir)
    started = time.monotonic()

    # --- Warnings: which inputs arrived (§ 3.5, published before the classification can raise,
    # so a job that fails on a fresh or a cached catalog carries what was already found) --------
    chat_entry = catalog.get(("chat", video_id))
    chat_ok = chat_entry is not None and chat_entry.status == "ok"
    if not chat_ok:
        add_warning("chat_missing", _missing_reason("chat", chat_entry))
    audio_entry = catalog.get(("audio", video_id))
    audio_ok = audio_entry is not None and audio_entry.status == "ok"
    if not audio_ok:
        add_warning("audio_missing", _missing_reason("audio", audio_entry))
        # The audio step is `unavailable` only when the download tool was refused the media
        # (deleted, expired or subscriber-only; `VodUnavailableError`, `VodAccessRestrictedError`),
        # so without a user-provided file the VOD itself isn't accessible (§ 3.5).
        if audio_entry is not None and audio_entry.status == "unavailable" and video_file is None:
            raise AnalyzeError(
                "vod_unavailable",
                "Twitch has no accessible video with this ID: its media is deleted, expired "
                "or not publicly viewable (e.g. subscriber-only); pass a file of the VOD you "
                "already have with --video to analyze it anyway",
            )

    # --- The VOD itself (§ 3.5: resolving it is required) --------------------------------------
    meta = catalog.get(("vod_meta", video_id))
    if meta is not None and meta.status == "unavailable":
        raise AnalyzeError("vod_unavailable", "Twitch has no published video with this ID")
    if meta is None or meta.status != "ok":
        detail = "no details" if meta is None else (meta.error or "no details")
        raise AnalyzeError(
            "upstream_unavailable", f"the VOD's metadata couldn't be fetched: {detail}"
        )
    vod = read_vod(data_dir, video_id)
    if vod is None:  # the catalog says ok, so this row exists; a guard against corruption
        raise AnalyzeError(
            "upstream_unavailable", f"the VOD's row is missing from {data_dir / 'processed'}"
        )
    if vod.muted_segments:
        spans = ", ".join(f"{s.offset_s:.0f}s+{s.duration_s:.0f}s" for s in vod.muted_segments)
        warnings.append(
            AnalysisWarning(
                code="audio_partly_muted",
                message=f"Twitch muted part of the VOD's audio ({spans}); audio signals are "
                "unreliable there",
            )
        )

    snapshot = _read_snapshot(data_dir, video_id)
    clips_entry = catalog.get(("clips", video_id))
    clips_ok = clips_entry is not None and clips_entry.status == "ok" and snapshot is not None
    moments: list[MomentInput] = []
    n_viewer_clips_total: int | None = None
    if clips_ok and snapshot is not None:
        n_viewer_clips_total = int(
            snapshot.clips.filter(
                (pl.col("video_id") == video_id) & pl.col("vod_offset_s").is_not_null()
            )["clip_id"].n_unique()
        )
        moments = _moments_of(snapshot.clips, video_id)
        incomplete = snapshot.manifest.get("complete") is False or bool(
            snapshot.manifest.get("saturated_ranges")
        )
        if incomplete:
            warnings.append(
                AnalysisWarning(
                    code="viewer_clips_incomplete",
                    message="the clip list hit a query limit, so some viewer clips may be missing",
                )
            )
    else:
        if clips_entry is not None and clips_entry.status == "ok":
            # The snapshot is recorded done but can't be read. Ingest considers a snapshot
            # with both files present done and never re-takes it, so the warning names what
            # to remove rather than suggesting a plain retry.
            warnings.append(
                AnalysisWarning(
                    code="viewer_clips_missing",
                    message="the clip snapshot on disk couldn't be read; remove the "
                    f"{video_id}-* files from {clips_dir(data_dir)} and analyze again to "
                    "re-take it",
                )
            )
        else:
            warnings.append(
                AnalysisWarning(
                    code="viewer_clips_missing",
                    message=_missing_reason("the viewer clip list", clips_entry)
                    + "; no candidate is built from viewer clips",
                )
            )

    publish_warnings()  # the full set, including the snapshot-based warnings

    # --- Features and scoring ------------------------------------------------------------------
    stage("computing_features")
    windows = _timed(
        "windows",
        lambda: make_windows(
            video_id, vod.duration_s, tuple((s.offset_s, s.duration_s) for s in vod.muted_segments)
        ),
    )
    chat_features = (
        _timed(
            "chat_features",
            lambda: compute_chat_features(
                windows, pl.read_parquet(chat_path(data_dir, video_id)), lexicon=lexicon
            ),
        )
        if chat_ok
        else None
    )
    audio_features = (
        _timed(
            "audio_features",
            lambda: compute_audio_features(windows, normalized_audio_path(data_dir, video_id)),
        )
        if audio_ok
        else None
    )
    # § 3.5 precedence step 3, whatever scorer was chosen: with neither signal family and
    # no viewer-clip moment, no scorer — random included — has anything to rank. The
    # contract's rule is scorer-independent here: a retry is worth it when ANY missing
    # input failed transiently. (A chosen scorer whose OWN inputs are missing, but which
    # could have run, is classified through _ScorerUnavailable below instead.)
    if chat_features is None and audio_features is None and not moments:
        raise _nothing_to_rank(catalog, video_id)
    stage("ranking")
    try:
        scored = _timed(
            "scoring",
            lambda: _score_windows(
                windows,
                chat_features,
                audio_features,
                baseline=baseline,
                model_dir=model_dir,
            ),
        )
    except _ScorerUnavailable as exc:
        # A chosen scorer's inputs are missing; the code still follows § 3.5 (a transient
        # fetch failure of an input the scorer needs keeps upstream_unavailable's "try again
        # later"; anything else can't be fixed by retrying).
        raise _nothing_to_rank(catalog, video_id, exc.reason, exc.needed) from None
    if scored is None:  # neither signal family arrived, and moments exist
        # Viewer-clip moments alone rank, in stream order; the scorer field records that
        # no signal scored anything (a zero score everywhere would invent a peak at 0 s).
        scorer_info = ScorerInfo(kind="heuristic", name="no_signals")
        candidates = _timed("candidates", lambda: _moments_only(moments, vod.duration_s))
    else:
        scores, scorer, run_id = scored
        scorer_info = ScorerInfo(kind=scorer.kind, name=scorer.name, run_id=run_id)
        features = _feature_frame(chat_features, audio_features)
        candidates = _timed(
            "candidates",
            lambda: generate_candidates(
                scores, duration_s=vod.duration_s, features=features, moments=moments
            ),
        )
    result = AnalysisResult(
        generated_at=clock(),
        vod=VodSummary(
            video_id=video_id,
            title=vod.title,
            broadcaster_id=vod.broadcaster_id,
            broadcaster_display_name=_broadcaster_display_name(vod.broadcaster_id, tools),
            duration_s=vod.duration_s,
            created_at=vod.created_at,
        ),
        scorer=scorer_info,
        n_viewer_clips_total=n_viewer_clips_total,
        warnings=tuple(warnings),
        candidates=tuple(candidates),
    )
    path = result_path(artifacts_dir, video_id, out_path)
    _write_result(path, result)
    log.info(
        "analyzed video_id=%s candidates=%d warnings=%d elapsed_s=%.1f scorer=%s out=%s",
        video_id,
        len(result.candidates),
        len(result.warnings),
        time.monotonic() - started,
        scorer_info.name,
        path,
    )
    return result


# --- Pieces -------------------------------------------------------------------------------------


def _timed[T](stage: str, work: Callable[[], T]) -> T:
    """Run `work` and log how long the pipeline stage took."""
    started = time.monotonic()
    result = work()
    log.info("analyze stage=%s elapsed_s=%.1f", stage, time.monotonic() - started)
    return result


def _probe_video_file(video_file: Path | None, runner: Runner | None) -> MediaInfo | None:
    """Check the optional local video file the way the API's request handler does (§ 3.1)."""
    if video_file is None:
        return None
    if not video_file.is_file():
        raise AnalyzeError("invalid_video_path", f"video file not found: {video_file}")
    try:
        info = probe(video_file, runner=runner)
    except FFmpegError as exc:
        raise AnalyzeError(
            "invalid_video_path", f"{video_file} is not a readable video file: {exc}"
        ) from None
    if not info.video_streams or not math.isfinite(info.duration_s) or info.duration_s <= 0:
        raise AnalyzeError(
            "invalid_video_path", f"{video_file} has no video stream or no known duration"
        )
    return info


def _missing_reason(what: str, entry: CatalogEntry | None) -> str:
    """A `*_missing` warning message: what happened, and whether a retry may help."""
    return _missing_message(
        what, None if entry is None else entry.status, None if entry is None else entry.error
    )


def _missing_message(what: str, status: str | None, error: str | None) -> str:
    """The same message, from a step's outcome and detail as well as a catalog entry."""
    if status is None or status == "unavailable":
        return f"{what} couldn't be obtained for this VOD; trying again later won't help"
    return f"{what} failed: {error or 'no details'}; trying again later may help"


@dataclass(frozen=True, slots=True)
class _Snapshot:
    """A VOD's clip snapshot: its Parquet rows and its manifest."""

    clips: pl.DataFrame
    manifest: dict[str, object]


def _read_snapshot(data_dir: Path, video_id: str) -> _Snapshot | None:
    """The VOD's latest readable clip snapshot (newest first), or None when there is none."""
    manifests = sorted(clips_dir(data_dir).glob(f"{validate_video_id(video_id)}-*.json"))
    for manifest in reversed(manifests):
        parquet = manifest.with_suffix(".parquet")
        if not parquet.is_file():
            continue
        try:
            data: object = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("clip snapshot manifest unreadable path=%s error=%s", manifest, exc)
            continue
        if not isinstance(data, dict):
            log.warning("clip snapshot manifest malformed path=%s", manifest)
            continue
        try:
            clips = pl.read_parquet(parquet)
        except Exception as exc:  # polars raises ComputeError and more on damaged files
            log.warning("clip snapshot parquet unreadable path=%s error=%s", parquet, exc)
            continue
        return _Snapshot(clips, data)
    return None


def _moments_of(clips: pl.DataFrame, video_id: str) -> list[MomentInput]:
    """The snapshot's viewer-clip moments of this VOD, for candidate generation."""
    merged = merge_clips_into_moments(clips.filter(pl.col("video_id") == video_id))
    skipped = merged.n_without_vod_offset + merged.n_without_video
    if skipped:
        log.info("clips without a VOD position skipped video_id=%s n=%d", video_id, skipped)
    return [
        MomentInput(
            moment_id=row["moment_id"],
            start_s=row["start_s"],
            end_s=row["end_s"],
            clip_ids=tuple(row["clip_ids"]),
        )
        for row in merged.moments.iter_rows(named=True)
    ]


def _feature_frame(
    chat_features: pl.DataFrame | None, audio_features: pl.DataFrame | None
) -> pl.DataFrame | None:
    """The per-window features that arrived: both families joined, one, or None."""
    if chat_features is None and audio_features is None:
        return None
    if chat_features is None:
        assert audio_features is not None
        return audio_features
    if audio_features is None:
        return chat_features
    chat = chat_features.select("window_id", *CHAT_FEATURE_COLUMNS)
    audio = audio_features.select("window_id", *AUDIO_FEATURE_COLUMNS)
    return chat.join(audio, on="window_id", how="full", coalesce=True)


def _with_window_bounds(scores: pl.DataFrame, windows: pl.DataFrame) -> pl.DataFrame:
    """The scorer's window scores with the windows' times, as generate_candidates needs."""
    return scores.join(windows.select("window_id", "start_s", "end_s"), on="window_id")


class _ScorerUnavailable(Exception):
    """A chosen scorer's inputs are missing; the caller classifies it per § 3.5.

    Attributes:
        reason: What the scorer was missing, for the error message.
        needed: The ingest steps whose data the scorer needs (``chat``, ``audio``).
    """

    def __init__(self, reason: str, needed: tuple[str, ...]) -> None:
        super().__init__(reason)
        self.reason = reason
        self.needed = needed


def _needed_steps(scorer: Scorer) -> tuple[str, ...]:
    """The ingest steps a scorer's required features come from, sorted."""
    return tuple(
        sorted(
            {
                "chat" if feature.startswith("chat_") else "audio"
                for feature in scorer.required_features
            }
        )
    )


def _score_windows(
    windows: pl.DataFrame,
    chat_features: pl.DataFrame | None,
    audio_features: pl.DataFrame | None,
    *,
    baseline: str,
    model_dir: Path | None,
) -> tuple[pl.DataFrame, Scorer, str | None] | None:
    """Score the windows with a scorer that can run, or None when no signal arrived.

    Raises:
        _ScorerUnavailable: A named scorer's required features are missing; the caller
            classifies it per § 3.5.
        ScorerError: `baseline` isn't a known scorer name, or `model_dir` isn't a saved run.
    """
    features = _feature_frame(chat_features, audio_features)
    scoreable = features if features is not None else windows.select("window_id")
    if model_dir is not None:
        scorer = load_scorer(model_dir)
        try:
            scores = scorer.score(scoreable)
        except ScorerError as exc:
            raise _ScorerUnavailable(
                f"the {scorer.name} model can't score this VOD: {exc}", _needed_steps(scorer)
            ) from None
        return _with_window_bounds(scores, windows), scorer, model_dir.name
    name = baseline
    if name == ChatLoudness.name:
        # The composite default degrades to whichever family arrived; a named scorer runs
        # as named and fails with the § 3.5 codes when its signal is missing.
        if chat_features is None and audio_features is None:
            return None
        if chat_features is None:
            name = Loudness.name
        elif audio_features is None:
            name = ChatSpike.name
    scorer = make_scorer(name)
    try:
        scores = scorer.score(scoreable)
    except ScorerError as exc:
        raise _ScorerUnavailable(
            f"the {scorer.name} scorer can't score this VOD: {exc}", _needed_steps(scorer)
        ) from None
    return _with_window_bounds(scores, windows), scorer, None


def _moments_only(moments: Sequence[MomentInput], duration_s: float) -> list[Candidate]:
    """Viewer-clip moments as candidates in stream order, when no signal scored anything.

    Each keeps its own span, clamped to the VOD; a moment that no longer fits is dropped.
    At most `MAX_CANDIDATES` rank (the first ones in stream order).
    """
    kept = [
        moment
        for moment in sorted(moments, key=lambda m: (m.start_s, m.moment_id))
        if moment.start_s < duration_s and min(moment.end_s, duration_s) > moment.start_s
    ][:MAX_CANDIDATES]
    return [
        Candidate(
            candidate_id=f"c{rank:03d}",
            rank=rank,
            score=0.0,
            start_s=float(moment.start_s),
            end_s=float(min(moment.end_s, duration_s)),
            source="viewer_clips",
            n_viewer_clips=len(moment.clip_ids),
            top_signals=(),
        )
        for rank, moment in enumerate(kept, start=1)
    ]


def _nothing_to_rank(
    catalog: dict[tuple[str, str], CatalogEntry],
    video_id: str,
    scorer_reason: str | None = None,
    needed: tuple[str, ...] = INPUT_STEPS,
) -> AnalyzeError:
    """The § 3.5 failure when nothing can be ranked (precedence step 3).

    `scorer_reason` names what a chosen scorer was missing, and `needed` the ingest steps
    whose data that scorer uses: only a NEEDED step failing while fetching (tool and
    network errors count as transient, and when unsure, treat as transient) keeps
    `upstream_unavailable`'s "trying again later may help" — a retry can't bring back an
    input that is gone for good, whatever else failed.
    """
    transient = [
        step
        for step in needed
        if (entry := catalog.get((step, video_id))) is not None and entry.status == "failed"
    ]
    if transient:
        trail = f"{', '.join(transient)} failed while being fetched"
        reason = scorer_reason if scorer_reason is not None else "chat and audio are both missing"
        return AnalyzeError(
            "upstream_unavailable", f"{reason} and {trail}; trying again later may work"
        )
    if scorer_reason is not None:
        return AnalyzeError("no_usable_signals", scorer_reason)
    return AnalyzeError(
        "no_usable_signals",
        "nothing to rank: chat and audio are both missing and no viewer clip has a VOD position",
    )


def _broadcaster_display_name(broadcaster_id: str, tools: IngestTools) -> str:
    """The channel's display name (Get Users by ID), or its ID when that can't be fetched."""
    if tools.helix is None:
        return broadcaster_id
    try:
        (user,) = tools.helix.get_users_by_id([broadcaster_id])
    except Exception as exc:  # a header nicety; the analysis doesn't depend on it
        log.warning(
            "broadcaster display name unavailable broadcaster_id=%s error=%s: %s",
            broadcaster_id,
            type(exc).__name__,
            exc,
        )
        return broadcaster_id
    return user.display_name


def result_path(artifacts_dir: Path, video_id: str, out_path: Path | None = None) -> Path:
    """Where an analysis JSON goes: `out_path`, or ``<artifacts_dir>/analysis/<video_id>.json``."""
    return (
        out_path
        if out_path is not None
        else artifacts_dir / "analysis" / f"{validate_video_id(video_id)}.json"
    )


def _write_result(path: Path, result: AnalysisResult) -> None:
    """Write the result JSON atomically, so a reader never sees a partial file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.part")
    try:
        temp.write_text(result.model_dump_json(indent=2) + "\n", encoding="utf-8", newline="\n")
        temp.replace(path)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


__all__ = [
    "DEFAULT_SCORER",
    "VIDEO_MISMATCH_TOLERANCE_S",
    "AnalyzeError",
    "FailureCode",
    "analyze_vod",
    "result_path",
]
