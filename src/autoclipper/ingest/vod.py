"""Gather everything one VOD needs, in one go: metadata, viewer clips, chat and audio.

`ingest_vod` runs four steps for one VOD (docs/data/collection-plan.md § Ingest) and records each
in the catalog (`autoclipper.ingest.catalog`):

1. ``vod_meta``: Get Videos (`HelixClient.get_videos`); the row goes into
   ``<data_dir>/processed/vods.parquet`` (one row per VOD, replaced on re-fetch).
2. ``clips``: the viewer-clip snapshot (`fetch_vod_clips`), written once as
   ``processed/clips/<snapshot_id>.parquet`` with a manifest ``<snapshot_id>.json`` next to it:
   capture times, the creation-time span searched, coverage, and whether the VOD was at least
   7 days old at capture (docs/data/labeling.md § 7). A snapshot is immutable: once taken, a
   re-run never takes another.
3. ``chat``: the chat replay (`download_chat`), saved pseudonymized as
   ``interim/chat/<video_id>.parquet``. The raw file with usernames is deleted after parsing,
   and also when the download or the parse fails, so no username stays on disk.
4. ``audio``: the audio (`prepare_vod_audio`), or a video or audio file the user already has
   (`normalize_audio`), as ``interim/audio/<video_id>.wav``.

A step whose catalog row is ``ok`` (and whose output is still there) or ``unavailable`` is
skipped, so running again picks up only what failed. A failing step is recorded as ``failed``
with its reason and the other steps still run; the clip snapshot needs the metadata, so it is
recorded as failed (or unavailable) when the metadata is.
"""

import dataclasses
import json
import logging
import wave
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final, Literal

import pyarrow as pa
import pyarrow.parquet as pq
from pydantic import SecretStr

from autoclipper import tables
from autoclipper.ffmpeg import Runner
from autoclipper.ingest import chat as chat_module
from autoclipper.ingest.catalog import CatalogEntry, Status, read_catalog, record
from autoclipper.ingest.catalog import write_table_atomically as _write_table
from autoclipper.ingest.clips import VodClips, fetch_vod_clips
from autoclipper.ingest.media import (
    Downloader,
    VodAccessRestrictedError,
    VodUnavailableError,
    normalize_audio,
    normalized_audio_path,
    prepare_vod_audio,
    validate_video_id,
)
from autoclipper.ingest.twitch_api import (
    HelixClient,
    MutedSegment,
    TwitchNotFoundError,
    Vod,
)

log = logging.getLogger(__name__)

STAGE_VERSION: Final = "1"
"""Bump when a step's output changes for the same input."""

STEPS: Final = ("vod_meta", "clips", "chat", "audio")
# Pilot VODs must be at least this old when their clips are captured (labeling.md § 7).
MIN_VOD_AGE: Final = timedelta(days=7)

type StepName = Literal["vod_meta", "clips", "chat", "audio"]
type Outcome = Literal["ok", "skipped", "failed", "unavailable"]


@dataclass(frozen=True, slots=True)
class StepResult:
    """How one step went in this run.

    Attributes:
        step: The catalog ``object_type``.
        outcome: ``skipped`` when an earlier run finished it (or found it unavailable).
        detail: What was written, or why it failed.
    """

    step: StepName
    outcome: Outcome
    detail: str


@dataclass(frozen=True, slots=True)
class IngestReport:
    """The steps of one `ingest_vod` run, in order."""

    video_id: str
    steps: tuple[StepResult, ...]

    @property
    def failed(self) -> bool:
        """True when any step failed in this run."""
        return any(step.outcome == "failed" for step in self.steps)


@dataclass(frozen=True, slots=True)
class IngestTools:
    """What the steps call; tests pass fakes.

    Attributes:
        helix: The Twitch API client, or None when no credentials are configured (the metadata
            and clip steps then fail with `missing_helix`).
        salt: The chat hash salt, or None when it isn't configured (clips and chat fail).
        chat_runner: Runs TwitchDownloaderCLI.
        chat_binary: The TwitchDownloaderCLI executable (default: looked up on PATH).
        downloader: Downloads the audio (default yt-dlp).
        runner: Executes FFmpeg and FFprobe.
        clock: The current time, timezone-aware.
        missing_helix: Why `helix` is None, shown in the catalog.
        missing_salt: Why `salt` is None, shown in the catalog.
    """

    helix: HelixClient | None = None
    salt: SecretStr | None = None
    chat_runner: chat_module.CommandRunner = chat_module.run_command
    chat_binary: str | Path | None = None
    downloader: Downloader | None = None
    runner: Runner | None = None
    clock: Callable[[], datetime] = lambda: datetime.now(tz=UTC)
    missing_helix: str = "Twitch API credentials are not configured"
    missing_salt: str = "AUTOCLIPPER_CHAT_HASH_SALT is not configured"


# --- Paths -------------------------------------------------------------------------------------


def vods_path(data_dir: Path) -> Path:
    """``<data_dir>/processed/vods.parquet``."""
    return data_dir / "processed" / "vods.parquet"


def clips_dir(data_dir: Path) -> Path:
    """``<data_dir>/processed/clips/``: one snapshot Parquet and manifest JSON per capture."""
    return data_dir / "processed" / "clips"


def chat_path(data_dir: Path, video_id: str) -> Path:
    """``<data_dir>/interim/chat/<video_id>.parquet``."""
    return data_dir / "interim" / "chat" / f"{validate_video_id(video_id)}.parquet"


def raw_chat_path(data_dir: Path, video_id: str) -> Path:
    """``<data_dir>/raw/chat/<video_id>.json``, deleted after parsing."""
    return data_dir / "raw" / "chat" / f"{validate_video_id(video_id)}.json"


def snapshot_id_for(video_id: str, captured_at: datetime) -> str:
    """``<video_id>-<capture start as YYYYMMDDTHHMMSSZ>``, unique per capture."""
    return f"{video_id}-{captured_at.astimezone(UTC):%Y%m%dT%H%M%SZ}"


# --- The run -----------------------------------------------------------------------------------


def ingest_vod(
    video_id: str,
    *,
    data_dir: Path,
    tools: IngestTools,
    media_file: Path | None = None,
) -> IngestReport:
    """Run the four ingest steps for one VOD, skipping the ones already done.

    Args:
        video_id: The Twitch video ID.
        data_dir: The data directory (``get_settings().data_dir``).
        tools: The API client, salt and downloaders.
        media_file: A video or audio file of this VOD the user already has; the audio step then
            converts it instead of downloading. It must start at the VOD's start.

    Raises:
        InvalidVideoIdError: `video_id` isn't a Twitch video ID.
        FileNotFoundError: `media_file` doesn't exist.
    """
    validate_video_id(video_id)
    if media_file is not None and not media_file.is_file():
        raise FileNotFoundError(f"media file not found: {media_file}")
    catalog = read_catalog(data_dir)
    results: list[StepResult] = []

    def done_before(step: StepName, output_exists: bool) -> CatalogEntry | None:
        entry = catalog.get((step, video_id))
        if entry is None or entry.status == "failed":
            return None
        if entry.status == "ok" and not output_exists:
            return None  # the output was removed: do it again
        return entry

    def finish(step: StepName, status: Status, tool: str, detail: str) -> StepResult:
        error = None if status == "ok" else detail
        entry = CatalogEntry(step, video_id, status, tool, STAGE_VERSION, tools.clock(), error)
        record(data_dir, entry)
        catalog[(step, video_id)] = entry
        log.info("ingest step=%s video_id=%s status=%s", step, video_id, status)
        result = StepResult(step, status, detail)
        results.append(result)
        return result

    def skip(step: StepName, entry: CatalogEntry) -> None:
        detail = "done in an earlier run" if entry.status == "ok" else f"unavailable: {entry.error}"
        results.append(StepResult(step, "skipped", detail))

    # 1. Metadata.
    vod: Vod | None = None
    meta_entry = done_before("vod_meta", _read_vod(data_dir, video_id) is not None)
    if meta_entry is not None:
        skip("vod_meta", meta_entry)
        vod = _read_vod(data_dir, video_id)
    elif tools.helix is None:
        finish("vod_meta", "failed", "helix", tools.missing_helix)
    else:
        try:
            (vod,) = tools.helix.get_videos([video_id])
        except TwitchNotFoundError:
            finish("vod_meta", "unavailable", "helix", "Twitch has no published video with this ID")
        except Exception as exc:  # recorded, not fatal
            finish("vod_meta", "failed", "helix", _describe(exc))
        else:
            _write_vod(data_dir, vod)
            finish("vod_meta", "ok", "helix", f"wrote {vods_path(data_dir)}")

    # 2. Viewer clips (needs the metadata).
    clips_entry = done_before("clips", _snapshot_exists(data_dir, video_id))
    if clips_entry is not None:
        skip("clips", clips_entry)
    elif vod is None:
        meta_status = catalog[("vod_meta", video_id)].status
        status: Status = "unavailable" if meta_status == "unavailable" else "failed"
        finish("clips", status, "helix", "needs the VOD's metadata, which isn't available")
    elif tools.helix is None:
        finish("clips", "failed", "helix", tools.missing_helix)
    elif tools.salt is None:
        finish("clips", "failed", "helix", tools.missing_salt)
    else:
        try:
            manifest = _take_snapshot(data_dir, vod, tools.helix, tools.salt, tools.clock)
        except Exception as exc:  # recorded, not fatal
            finish("clips", "failed", "helix", _describe(exc))
        else:
            flag = "" if manifest["mature"] else "; the VOD was under 7 days old (flagged)"
            finish(
                "clips",
                "ok",
                "helix",
                f"{manifest['n_clips']} clips in snapshot {manifest['snapshot_id']}{flag}",
            )

    # A VOD Twitch doesn't have (deleted, expired, unpublished) has no chat or audio either.
    vod_gone = catalog[("vod_meta", video_id)].status == "unavailable"
    gone = "the VOD is unavailable (Twitch has no published video with this ID)"

    # 3. Chat.
    chat_file = chat_path(data_dir, video_id)
    raw_chat = raw_chat_path(data_dir, video_id)
    # A raw replay left by an interrupted run holds usernames: remove it whatever happens next.
    raw_chat.unlink(missing_ok=True)
    chat_entry = done_before("chat", chat_file.is_file())
    tool = f"{chat_module.TOOL_NAME}@{chat_module.PINNED_TOOL_VERSION}"
    if chat_entry is not None:
        skip("chat", chat_entry)
    elif vod_gone:
        finish("chat", "unavailable", tool, gone)
    elif tools.salt is None:
        finish("chat", "failed", tool, tools.missing_salt)
    else:
        try:
            try:
                replay = chat_module.download_chat(
                    video_id,
                    raw_chat,
                    salt=tools.salt,
                    binary=tools.chat_binary,
                    runner=tools.chat_runner,
                )
                _write_chat(chat_file, replay)
            finally:
                # The raw replay holds usernames: never leave it behind, not after a failure
                # and not after an interrupt (Ctrl+C) either.
                raw_chat.unlink(missing_ok=True)
        except Exception as exc:  # recorded, not fatal
            finish("chat", "failed", tool, _describe(exc))
        else:
            version = replay.tool_version or chat_module.PINNED_TOOL_VERSION
            finish(
                "chat",
                "ok",
                f"{chat_module.TOOL_NAME}@{version}",
                f"{len(replay.messages)} messages in {chat_file}",
            )

    # 4. Audio.
    wav = normalized_audio_path(data_dir, video_id)
    audio_entry = done_before("audio", wav.is_file())
    if audio_entry is not None and audio_entry.status == "unavailable" and media_file is not None:
        audio_entry = None  # the user's own file replaces a download Twitch refused
    audio_tool = LOCAL_FILE_TOOL if media_file is not None else _ytdlp_tool()
    if audio_entry is not None:
        if audio_entry.status == "ok" and audio_entry.tool == LOCAL_FILE_TOOL and vod is not None:
            # Audio from a local file is checked against the VOD's length, which an earlier run
            # may not have known (its metadata step failed). Check the cached audio now, as a
            # first run would, instead of keeping audio that may not be this VOD's.
            try:
                _check_media_length(_wav_duration_s(wav), vod.duration_s, media_file or wav)
            except MediaLengthError as exc:
                wav.unlink(missing_ok=True)
                finish("audio", "failed", LOCAL_FILE_TOOL, _describe(exc))
            else:
                skip("audio", audio_entry)
        else:
            skip("audio", audio_entry)
    elif vod_gone and media_file is None:
        finish("audio", "unavailable", audio_tool, gone)
    else:
        try:
            if media_file is not None:
                audio = normalize_audio(
                    media_file, video_id, data_dir=data_dir, runner=tools.runner
                )
            else:
                audio = prepare_vod_audio(
                    video_id, data_dir=data_dir, downloader=tools.downloader, runner=tools.runner
                )
            if media_file is not None and vod is not None:
                _check_media_length(audio.duration_s, vod.duration_s, media_file)
        except (VodUnavailableError, VodAccessRestrictedError) as exc:
            finish("audio", "unavailable", audio_tool, _describe(exc))
        except MediaLengthError as exc:
            wav.unlink(missing_ok=True)  # its timeline doesn't match the VOD's
            finish("audio", "failed", audio_tool, _describe(exc))
        except Exception as exc:  # recorded, not fatal
            finish("audio", "failed", audio_tool, _describe(exc))
        else:
            finish("audio", "ok", audio_tool, f"{audio.duration_s:.1f} s in {audio.path}")

    return IngestReport(video_id, tuple(results))


# --- Steps -------------------------------------------------------------------------------------

# The catalog's `tool` for audio converted from a file the user gave (`--media`).
LOCAL_FILE_TOOL: Final = "local-file"


def _wav_duration_s(path: Path) -> float:
    with wave.open(str(path), "rb") as audio:
        return audio.getnframes() / audio.getframerate()


# A local file may differ from Twitch's duration by this much (whichever is larger).
MEDIA_LENGTH_TOLERANCE_S: Final = 5.0
MEDIA_LENGTH_TOLERANCE_FRACTION: Final = 0.01


class MediaLengthError(Exception):
    """A ``--media`` file's length doesn't match the VOD's, so it isn't the whole VOD."""


def _check_media_length(media_s: float, vod_s: float, media_file: Path) -> None:
    tolerance = max(MEDIA_LENGTH_TOLERANCE_S, MEDIA_LENGTH_TOLERANCE_FRACTION * vod_s)
    if abs(media_s - vod_s) > tolerance:
        raise MediaLengthError(
            f"{media_file.name} is {media_s:.1f} s long but the VOD is {vod_s:.1f} s; pass a "
            "file of the whole VOD, starting at its start"
        )


def _describe(exc: BaseException) -> str:
    log.warning("ingest step failed: %s: %s", type(exc).__name__, exc)
    message = str(exc).strip() or "no details"
    return f"{type(exc).__name__}: {message}"[:1000]


def _ytdlp_tool() -> str:
    try:
        from yt_dlp import version
    except ImportError:  # pragma: no cover - yt-dlp is a dependency
        return "yt-dlp"
    return f"yt-dlp@{version.__version__}"


def _vod_row(vod: Vod) -> dict[str, object]:
    row = dataclasses.asdict(vod)
    row["muted_segments"] = [dataclasses.asdict(s) for s in vod.muted_segments]
    row["game_ids"] = None if vod.game_ids is None else list(vod.game_ids)
    return row


def _write_vod(data_dir: Path, vod: Vod) -> None:
    path = vods_path(data_dir)
    rows = [] if not path.is_file() else pq.read_table(path, schema=tables.VODS).to_pylist()
    rows = [row for row in rows if row["video_id"] != vod.video_id] + [_vod_row(vod)]
    rows.sort(key=lambda row: str(row["video_id"]))
    _write_table(pa.Table.from_pylist(rows, schema=tables.VODS), path)


def _read_vod(data_dir: Path, video_id: str) -> Vod | None:
    path = vods_path(data_dir)
    if not path.is_file():
        return None
    for row in pq.read_table(path, schema=tables.VODS).to_pylist():
        if row["video_id"] == video_id:
            segments = tuple(MutedSegment(**s) for s in row["muted_segments"])
            game_ids = None if row["game_ids"] is None else tuple(row["game_ids"])
            return Vod(**{**row, "muted_segments": segments, "game_ids": game_ids})
    return None


def _snapshot_exists(data_dir: Path, video_id: str) -> bool:
    """A snapshot counts only with both its Parquet file and its manifest."""
    directory = clips_dir(data_dir)
    return any(
        manifest.with_suffix(".parquet").is_file()
        for manifest in directory.glob(f"{validate_video_id(video_id)}-*.json")
    )


def _take_snapshot(
    data_dir: Path,
    vod: Vod,
    helix: HelixClient,
    salt: SecretStr,
    clock: Callable[[], datetime],
) -> dict[str, object]:
    started = clock()
    found: VodClips = fetch_vod_clips(helix, vod, salt=salt, captured_at=started, clock=clock)
    ended = clock()
    snapshot_id = snapshot_id_for(vod.video_id, started)
    rows = [dataclasses.asdict(clip) for clip in found.clips]
    parquet = clips_dir(data_dir) / f"{snapshot_id}.parquet"
    _write_table(pa.Table.from_pylist(rows, schema=tables.CLIPS), parquet)
    summary = found.summary
    age = started - vod.created_at
    manifest: dict[str, object] = {
        "snapshot_id": snapshot_id,
        "video_id": vod.video_id,
        "broadcaster_id": vod.broadcaster_id,
        "file": parquet.name,
        "capture_started_at": _iso(started),
        "capture_ended_at": _iso(ended),
        "search_started_at": _iso(summary.search_range.started_at),
        "search_ended_at": _iso(summary.search_range.ended_at),
        "n_ranges_queried": len(summary.ranges_queried),
        "saturated_ranges": [
            [_iso(r.started_at), _iso(r.ended_at)] for r in summary.saturated_ranges
        ],
        "complete": summary.complete,
        "n_clips": summary.n_clips,
        "n_without_vod_offset": summary.n_without_vod_offset,
        "vod_created_at": _iso(vod.created_at),
        "vod_age_days": round(age.total_seconds() / 86400, 3),
        "mature": age >= MIN_VOD_AGE,
        "stage_version": STAGE_VERSION,
    }
    text = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    manifest_path = parquet.with_suffix(".json")
    temp = manifest_path.with_name(f".{manifest_path.name}.part")
    try:
        temp.write_text(text, encoding="utf-8", newline="\n")
        temp.replace(manifest_path)
    except BaseException:
        # A snapshot without its manifest isn't pinned: remove it, so a retry leaves one.
        temp.unlink(missing_ok=True)
        parquet.unlink(missing_ok=True)
        raise
    return manifest


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _write_chat(path: Path, replay: chat_module.ChatReplay) -> None:
    rows = [
        {**dataclasses.asdict(message), "emote_ids": list(message.emote_ids)}
        for message in replay.messages
    ]
    _write_table(pa.Table.from_pylist(rows, schema=tables.CHAT_MESSAGES), path)
