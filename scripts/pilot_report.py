"""Report on the pilot collection (#35) from what is on disk. Reads only; writes no data.

Run from the repository root (where ``.env`` sets ``AUTOCLIPPER_DATA_DIR``)::

    uv run python scripts/pilot_report.py                              # tables on stdout
    uv run python scripts/pilot_report.py --json report.json           # also every metric
    uv run python scripts/pilot_report.py --check <report.md>    # are the doc's tables up to date?
    uv run python scripts/pilot_report.py --update <report.md>   # refresh its tables

Every number comes from the collection itself, never from a stored summary:

- **Which VODs:** the active pilot is ``configs/pilot_vods.txt`` (or the ``--vods`` file; the
  provenance table names the list that was read and its SHA-256). Each listed VOD must be a
  primary of the saved selections (``pilot/selections/selection-*.json``, recomputed with
  ``select_pilot_vods.py``) of a channel in ``configs/channels.yaml``. VODs in the catalog but
  not in the list (such as a diagnostic VOD of a dropped channel) are reported as *historical*
  and kept out of the pilot totals.
- **Per VOD:** the catalog (step status, tools), ``processed/vods.parquet`` (Helix length and
  times), the clip snapshot and its manifest, the pseudonymized chat table, the WAV header and
  the raw audio's size, and the runner's records (``pilot/runs/<run_id>.jsonl`` and the child's
  log next to it) for run time, phase times, memory and disk.
- **Storage:** every file under the data directory is put in exactly one category; a file that
  fits none is reported, so the totals reconcile with the directory. Files under ``pilot/``
  that are none of selections, lists, run records or the run lock are local notes. The lock and
  notes aren't part of the dataset and are left out of the storage table (``--json`` counts them),
  so a complete copy of the dataset gives the same table.
- **Checks:** the integrity rules the pilot relies on (four ``ok`` steps per VOD, one complete
  and mature snapshot, schemas, pseudonymized authors, no unredacted mentions, the WAV format,
  no raw chat left, ...). Any failure makes the command exit 1.

``--check`` and ``--update`` work on the blocks of a Markdown file between
``<!-- BEGIN pilot-report:<name> -->`` and ``<!-- END pilot-report:<name> -->``. ``--update``
rewrites only those blocks; ``--check`` exits 1 when one differs from a fresh computation, so a
reader with the data directory can confirm that the report's numbers match the data.

Definitions used in the tables: a VOD-hour is the Helix duration; a clip *starts after the end*
when its VOD offset is at or past the Helix duration and *ends after the end* when offset plus
length passes it; a chat message is *after the end* when its offset is past the duration; a clip
is *made after the stream* when Twitch's ``created_at`` is later than the VOD's start plus its
duration; the *densest 20 minutes* is the largest number of a VOD's clips whose offsets fall
within any 1,200 s span.
"""

import hashlib
import io
import json
import math
import re
import statistics
import sys
import wave
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Final

import numpy as np
import polars as pl
import pyarrow.parquet as pq
import select_pilot_vods as sp
import typer
from run_pilot import read_vod_list

from autoclipper import tables
from autoclipper.cli import ExitCode
from autoclipper.config import ConfigError, get_settings
from autoclipper.ingest.catalog import read_catalog
from autoclipper.ingest.chat import redact_mentions
from autoclipper.ingest.media import SAMPLE_RATE_HZ
from autoclipper.ingest.vod import MEDIA_LENGTH_TOLERANCE_S, STEPS
from autoclipper.labels.moments import merge_clips_into_moments
from autoclipper.labels.windows import make_windows

MIB: Final = 1024**2
GIB: Final = 1024**3
DENSEST_WINDOW_S: Final = 1200.0
VIEW_THRESHOLDS: Final = (10, 100)
# Training negatives are windows at least this far from any moment (docs/data/labeling.md § 4).
NEG_BUFFER_S: Final = 60.0
# A second label preview counts only clips with at least this many views.
PREVIEW_MIN_VIEWS: Final = 10
NA: Final = "-"  # a value that is missing in a table
EN_DASH: Final = chr(0x2013)  # en dash, between the ends of a range
_HASH: Final = re.compile(r"[0-9a-f]{64}")
_LOG_LINE: Final = re.compile(r"^(\S+Z) (out|err) (.*)$")
_STEP_DONE: Final = re.compile(r"logger=autoclipper\.ingest\.vod ingest step=(\w+) .*status=(\w+)")
_AUDIO_DOWNLOADED: Final = re.compile(r"logger=autoclipper\.ingest\.media downloaded audio ")
_CHAT_PARSED: Final = re.compile(r"parsed chat replay .*n_malformed=(\d+)")
_CLIPS_FETCHED: Final = re.compile(r"fetched clips .*other_videos=(\d+) duplicates=(\d+)")
BEGIN: Final = "<!-- BEGIN pilot-report:{} -->"
END: Final = "<!-- END pilot-report:{} -->"
_BLOCK: Final = re.compile(
    r"(<!-- BEGIN pilot-report:([\w-]+) -->\n)(.*?)(<!-- END pilot-report:\2 -->)", re.DOTALL
)


# --- Per-VOD measurements ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ClipStats:
    """One VOD's clip snapshot, as the report uses it."""

    snapshot_id: str
    n_clips: int
    n_without_offset: int
    n_ranges_queried: int
    n_saturated_ranges: int
    complete: bool
    mature: bool
    vod_age_days: float
    views: tuple[int, ...]
    n_made_after_stream: int
    n_start_after_end: int
    n_end_after_end: int
    densest_window: int
    n_creators: int
    n_other_videos: int | None = None
    n_duplicates: int | None = None


@dataclass(frozen=True, slots=True)
class LabelPreview:
    """How one VOD's windows would split under the documented label defaults.

    A preview for planning #38, not the labels: 10 s windows (`make_windows`), clips merged into
    moments (`merge_clips_into_moments`), a window positive when it overlaps a moment, a training
    negative when it is at least `NEG_BUFFER_S` from every moment, otherwise in the buffer.
    """

    n_windows: int
    n_moments: int
    n_positive: int
    n_negative: int
    n_buffer: int


def label_preview(clips: pl.DataFrame, *, video_id: str, vod_duration_s: float) -> LabelPreview:
    """`LabelPreview` of one VOD's clip snapshot (`clips` in the ``clips`` schema)."""
    windows = make_windows(video_id, vod_duration_s)
    moments = merge_clips_into_moments(clips).moments
    starts = windows["start_s"].to_numpy()[:, None]
    ends = windows["end_s"].to_numpy()[:, None]
    m_start = moments["start_s"].to_numpy()[None, :]
    m_end = moments["end_s"].to_numpy()[None, :]
    if moments.is_empty():
        overlaps = np.zeros(windows.height, dtype=bool)
        distance = np.full(windows.height, np.inf)
    else:
        # Half-open intervals, as label_pos: [s, e) and [ms, me) overlap iff s < me and ms < e.
        # Otherwise the distance is the gap between them (0 when they only touch).
        overlap = (starts < m_end) & (m_start < ends)
        gap = np.maximum(np.maximum(m_start - ends, starts - m_end), 0.0)
        overlaps = overlap.any(axis=1)
        distance = np.where(overlap, 0.0, gap).min(axis=1)
    positive = int(overlaps.sum())
    negative = int((~overlaps & (distance >= NEG_BUFFER_S)).sum())
    return LabelPreview(
        n_windows=windows.height,
        n_moments=moments.height,
        n_positive=positive,
        n_negative=negative,
        n_buffer=windows.height - positive - negative,
    )


@dataclass(frozen=True, slots=True)
class ChatStats:
    """One VOD's pseudonymized chat."""

    n_messages: int
    n_authors: int
    n_after_end: int
    n_bad_hashes: int
    n_unredacted_mentions: int
    offsets_sorted: bool
    min_offset_s: float | None
    n_malformed: int | None = None


@dataclass(frozen=True, slots=True)
class AudioStats:
    """One VOD's raw and normalized audio."""

    raw_bytes: int
    raw_files: int
    wav_bytes: int
    sample_rate_hz: int
    channels: int
    sample_width_bytes: int
    wav_duration_s: float
    wav_header_bytes: int


@dataclass(frozen=True, slots=True)
class RunStats:
    """The runner's records of one VOD: its attempts and the last attempt's measurements."""

    attempts: int
    run_ids: tuple[str, ...]
    total_s: float
    state_after: str | None
    peak_job_memory: int | None
    system_memory_free_min: int | None
    raw_chat_peak_bytes: int | None
    temp_files_left: int
    phases_s: dict[str, float | None] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class VodReport:
    """Everything the report knows about one VOD."""

    video_id: str
    login: str
    group: str  # "active" or "historical"
    selection_file: str | None
    created_at: str
    duration_s: float
    steps: dict[str, str]
    tools: dict[str, str]
    catalog_errors: int
    clips: ClipStats | None
    chat: ChatStats | None
    audio: AudioStats | None
    run: RunStats | None
    labels: LabelPreview | None = None
    retention_days: int | None = None  # Twitch's retention for the channel's type (an estimate)
    labels_min_views: LabelPreview | None = None  # only clips with >= PREVIEW_MIN_VIEWS views

    @property
    def hours(self) -> float:
        """Helix duration in hours."""
        return self.duration_s / 3600

    @property
    def estimated_expiry(self) -> str | None:
        """When Twitch would delete the VOD by its channel type's retention (UTC date)."""
        if self.retention_days is None:
            return None
        created = datetime.fromisoformat(self.created_at)
        return (created + timedelta(days=self.retention_days)).date().isoformat()


def densest_window(offsets: Iterable[float], width_s: float = DENSEST_WINDOW_S) -> int:
    """The most offsets within any span of `width_s` seconds (half-open, ``[t, t + width)``)."""
    ordered = sorted(offsets)
    best = start = 0
    for end, value in enumerate(ordered):
        while value - ordered[start] >= width_s:
            start += 1
        best = max(best, end - start + 1)
    return best


def clip_stats(
    clips: pl.DataFrame,
    manifest: Mapping[str, Any],
    *,
    vod_created_at: datetime,
    vod_duration_s: float,
    listing: tuple[int, int] | None = None,
) -> ClipStats:
    """Clip measurements of one snapshot (`clips` in the ``clips`` schema)."""
    stream_end = vod_created_at + timedelta(seconds=vod_duration_s)
    with_offset = clips.filter(pl.col("vod_offset_s").is_not_null())
    offsets = with_offset["vod_offset_s"].to_list()
    ends = (with_offset["vod_offset_s"] + with_offset["duration_s"]).to_list()
    return ClipStats(
        snapshot_id=str(manifest["snapshot_id"]),
        n_clips=clips.height,
        n_without_offset=clips.height - with_offset.height,
        n_ranges_queried=int(manifest["n_ranges_queried"]),
        n_saturated_ranges=len(manifest["saturated_ranges"]),
        complete=bool(manifest["complete"]),
        mature=bool(manifest["mature"]),
        vod_age_days=float(manifest["vod_age_days"]),
        views=tuple(int(v) for v in clips["view_count"].to_list()),
        n_made_after_stream=clips.filter(pl.col("created_at") > stream_end).height,
        n_start_after_end=sum(1 for o in offsets if o >= vod_duration_s),
        n_end_after_end=sum(1 for e in ends if e > vod_duration_s),
        densest_window=densest_window(offsets),
        n_creators=clips["creator_hash"].n_unique(),
        n_other_videos=listing[0] if listing else None,
        n_duplicates=listing[1] if listing else None,
    )


def chat_stats(chat: pl.DataFrame, *, vod_duration_s: float) -> ChatStats:
    """Chat measurements of one VOD (`chat` in the ``chat_messages`` schema)."""
    offsets = chat["offset_s"]
    texts = chat["text"].to_list()
    hashes = chat["user_hash"].to_list()
    return ChatStats(
        n_messages=chat.height,
        n_authors=chat["user_hash"].n_unique(),
        n_after_end=chat.filter(pl.col("offset_s") > vod_duration_s).height,
        n_bad_hashes=sum(1 for h in hashes if h is None or not _HASH.fullmatch(h)),
        n_unredacted_mentions=sum(1 for t in texts if t and redact_mentions(t) != t),
        offsets_sorted=bool(offsets.is_sorted()),
        min_offset_s=None if chat.is_empty() else float(offsets.min()),  # type: ignore[arg-type]
    )


def audio_stats(wav_path: Path, raw_paths: Sequence[Path]) -> AudioStats:
    """The WAV's format and length from its header, and the raw audio's size."""
    with wave.open(str(wav_path), "rb") as w:
        rate, channels, width, frames = (
            w.getframerate(),
            w.getnchannels(),
            w.getsampwidth(),
            w.getnframes(),
        )
    size = wav_path.stat().st_size
    return AudioStats(
        raw_bytes=sum(p.stat().st_size for p in raw_paths),
        raw_files=len(raw_paths),
        wav_bytes=size,
        sample_rate_hz=rate,
        channels=channels,
        sample_width_bytes=width,
        wav_duration_s=frames / rate,
        wav_header_bytes=size - frames * channels * width,
    )


def _timestamp(text: str) -> datetime:
    return datetime.fromisoformat(text)


@dataclass(frozen=True, slots=True)
class LogFacts:
    """What one attempt's child log says."""

    milestones: dict[str, datetime]
    n_malformed: int | None
    listing: tuple[int, int] | None


def read_log(lines: Iterable[str]) -> LogFacts:
    """The step completions (and the end of the audio download) logged by ``ingest``."""
    milestones: dict[str, datetime] = {}
    malformed = listing = None
    for line in lines:
        match = _LOG_LINE.match(line.rstrip("\n"))
        if not match:
            continue
        when, _, text = _timestamp(match[1]), match[2], match[3]
        if step := _STEP_DONE.search(text):
            milestones.setdefault(step[1], when)
        elif _AUDIO_DOWNLOADED.search(text):
            milestones.setdefault("audio_download", when)
        elif parsed := _CHAT_PARSED.search(text):
            malformed = int(parsed[1])
        elif fetched := _CLIPS_FETCHED.search(text):
            listing = (int(fetched[1]), int(fetched[2]))
    return LogFacts(milestones, malformed, listing)


PHASES: Final = (
    ("metadata", None, "vod_meta"),
    ("clips", "vod_meta", "clips"),
    ("chat", "clips", "chat"),
    ("audio_download", "chat", "audio_download"),
    ("normalize", "audio_download", "audio"),
)


def phase_seconds(
    started_at: datetime, milestones: Mapping[str, datetime]
) -> dict[str, float | None]:
    """Seconds per phase, each from the previous milestone (the first from the attempt's start).

    A phase is None when either of its milestones wasn't logged (e.g. a step skipped on a
    re-run). Times come from log lines, never from catalog timestamp gaps.
    """
    result: dict[str, float | None] = {}
    for name, start, end in PHASES:
        begin = started_at if start is None else milestones.get(start)
        finish = milestones.get(end)
        result[name] = None if begin is None or finish is None else (finish - begin).total_seconds()
    return result


def read_runs(runs_dir: Path) -> dict[str, list[dict[str, Any]]]:
    """Every attempt record of the runner, by video ID, in run order (``run_id`` added)."""
    attempts: dict[str, list[dict[str, Any]]] = {}
    for path in sorted(runs_dir.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("record") == "attempt":
                attempts.setdefault(row["video_id"], []).append({**row, "run_id": path.stem})
    return attempts


def read_run_settings(runs_dir: Path) -> dict[str, dict[str, Any]]:
    """The ``limits`` of every run's start record, by run ID."""
    settings: dict[str, dict[str, Any]] = {}
    for path in sorted(runs_dir.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip() and (row := json.loads(line)).get("record") == "start":
                settings[path.stem] = dict(row.get("limits") or {})
    return settings


def describe_settings(limits: Mapping[str, Any]) -> str:
    """The runner limits of a start record in words."""
    parts = [f"retries {limits.get('retries', '?')}"]
    if "timeout_base_s" in limits:
        parts.append(
            f"timeout {limits['timeout_base_s'] / 60:g} min + "
            f"{limits.get('timeout_per_hour_s', 0) / 60:g} min per VOD-hour"
        )
    if "min_free_bytes" in limits:
        parts.append(f"keep {limits['min_free_bytes'] / GIB:g} GiB disk free")
    if "max_job_memory" in limits:
        parts.append(f"tree memory cap {limits['max_job_memory'] / GIB:g} GiB")
    if "min_free_memory" in limits:
        parts.append(f"stop below {limits['min_free_memory'] / GIB:g} GiB free memory")
    if "disk_safety_factor" in limits:
        parts.append(f"disk estimate x{limits['disk_safety_factor']:g}")
    if "poll_s" in limits:
        parts.append(f"checked every {limits['poll_s']:g} s")
    return ", ".join(parts)


def run_stats(attempts: Sequence[dict[str, Any]], runs_dir: Path) -> tuple[RunStats, LogFacts]:
    """The runner's view of one VOD; phases and log facts come from its last attempt."""
    last = attempts[-1]
    log = runs_dir / last["run_id"] / f"{last['video_id']}-{last['attempt']}.log"
    facts = (
        read_log(log.read_text(encoding="utf-8").splitlines())
        if log.is_file()
        else LogFacts({}, None, None)
    )
    stats = RunStats(
        attempts=len(attempts),
        run_ids=tuple(dict.fromkeys(a["run_id"] for a in attempts)),
        total_s=sum(float(a.get("duration_s") or 0.0) for a in attempts),
        state_after=last.get("state_after"),
        peak_job_memory=last.get("peak_job_memory"),
        system_memory_free_min=last.get("system_memory_free_min"),
        raw_chat_peak_bytes=last.get("raw_chat_peak_bytes"),
        temp_files_left=len(last.get("temp_files_left") or []),
        phases_s=phase_seconds(_timestamp(last["started_at"]), facts.milestones),
    )
    return stats, facts


# --- Storage -----------------------------------------------------------------------------------

CATEGORIES: Final = (
    ("raw_audio", "Raw audio (compressed source)"),
    ("wav", "Normalized WAV (16 kHz mono)"),
    ("chat", "Pseudonymized chat tables"),
    ("clips", "Clip snapshots and manifests"),
    ("tables", "Shared tables (catalog, VODs, channels)"),
    ("raw_chat", "Raw chat replays (should be none)"),
    ("selections", "Selection records"),
    ("lists", "Earlier pilot lists"),
    ("runs", "Run records and logs"),
    ("lock", "Runner lock file"),
    ("notes", "Local notes (not part of the dataset)"),
    ("other", "Unclassified"),
)


OUTSIDE_DATASET: Final = frozenset({"lock", "notes"})
"""Categories counted in the storage check but not shown in the storage table."""


def classify(relative: Path) -> tuple[str, str | None]:
    """A data-dir file's storage category and the video ID it belongs to (if any)."""
    parts = relative.parts
    name = relative.name
    stem = name.split(".", 1)[0]
    if parts == ("catalog.parquet",) or parts in (
        ("processed", "vods.parquet"),
        ("processed", "broadcasters.parquet"),
    ):
        return "tables", None
    if len(parts) == 3 and parts[:2] == ("raw", "audio"):
        return "raw_audio", stem
    if len(parts) == 3 and parts[:2] == ("raw", "chat"):
        return "raw_chat", stem
    if len(parts) == 3 and parts[:2] == ("interim", "audio") and name.endswith(".wav"):
        return "wav", stem
    if len(parts) == 3 and parts[:2] == ("interim", "chat") and name.endswith(".parquet"):
        return "chat", stem
    if len(parts) == 3 and parts[:2] == ("processed", "clips"):
        return "clips", stem.split("-", 1)[0]
    if parts[:1] == ("pilot",):
        if parts[1:2] == ("selections",):
            return "selections", None
        if parts[1:2] == ("lists",):
            return "lists", None
        if parts[1:2] == ("runs",):
            return "runs", None
        if parts[1:] == ("run.lock",):
            return "lock", None
        return "notes", None
    return "other", None


@dataclass(frozen=True, slots=True)
class StorageRow:
    """Files and bytes of one category, split by VOD group."""

    category: str
    files: int
    bytes: int
    by_group: dict[str, int]  # "active" / "historical" / "unknown" / "shared" -> bytes


def storage(data_dir: Path, groups: Mapping[str, str]) -> tuple[list[StorageRow], list[str]]:
    """Every file under `data_dir` in one category; also the unclassified files' paths."""
    files: Counter[str] = Counter()
    sizes: Counter[str] = Counter()
    split: dict[str, Counter[str]] = {key: Counter() for key, _ in CATEGORIES}
    other: list[str] = []
    for path in sorted(p for p in data_dir.rglob("*") if p.is_file()):
        relative = path.relative_to(data_dir)
        category, video_id = classify(relative)
        size = path.stat().st_size
        files[category] += 1
        sizes[category] += size
        group = "shared" if video_id is None else groups.get(video_id, "unknown")
        split[category][group] += size
        if category == "other":
            other.append(relative.as_posix())
    rows = [
        StorageRow(key, files[key], sizes[key], dict(split[key]))
        for key, _ in CATEGORIES
        if files[key]
    ]
    return rows, other


def list_label(path: Path) -> str:
    """How the report names a VOD list: its path in the repository, else only its file name.

    A list outside the repository shows as ``<outside the repository>/<name>``, so a report never
    carries a local absolute path.
    """
    resolved = path.resolve()
    if resolved.is_relative_to(sp.REPO_ROOT):
        return resolved.relative_to(sp.REPO_ROOT).as_posix()
    return f"<outside the repository>/{resolved.name}"


def sha256(path: Path) -> str:
    """Hex SHA-256 of a file."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


# --- The report --------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Check:
    """One integrity rule and whether the collection meets it."""

    name: str
    ok: bool
    detail: str = ""


@dataclass(frozen=True, slots=True)
class Report:
    """The whole report."""

    channels: tuple[str, ...]
    list_label: str  # the VOD list that was read; see list_label()
    list_sha256: str
    selection_files: dict[str, str]  # file name -> SHA-256
    vods: tuple[VodReport, ...]
    storage: tuple[StorageRow, ...]
    unclassified: tuple[str, ...]
    checks: tuple[Check, ...]
    run_settings: dict[str, dict[str, Any]] = field(default_factory=dict)  # by run ID
    first_started_at: str | None = None
    last_ended_at: str | None = None

    def group(self, name: str) -> list[VodReport]:
        """The VODs of a group, in list order (active) or video-ID order (historical)."""
        return [v for v in self.vods if v.group == name]

    @property
    def ok(self) -> bool:
        """True when every check passes."""
        return all(c.ok for c in self.checks)


def _times(attempts: Mapping[str, Sequence[dict[str, Any]]], key: str) -> list[str]:
    return [a[key] for rows in attempts.values() for a in rows if a.get(key)]


def _manifest(clips_dir: Path, video_id: str) -> list[tuple[Path, dict[str, Any]]]:
    found = []
    for path in sorted(clips_dir.glob(f"{video_id}-*.json")):
        found.append((path, json.loads(path.read_text(encoding="utf-8"))))
    return found


def build_report(
    data_dir: Path,
    *,
    vod_list: Path,
    selection_paths: Sequence[Path],
    channels: Sequence[str],
) -> Report:
    """Measure the collection under `data_dir` (read only)."""
    listed = read_vod_list(vod_list)
    checks: list[Check] = []

    # Which selection each listed VOD is a primary of.
    origin: dict[str, tuple[str, sp.Assessment]] = {}
    selections = []
    for path in selection_paths:
        selection = sp.selection_from_json(json.loads(path.read_text(encoding="utf-8")))
        selections.append(selection)
        for a in selection.assessments:
            if a.decision in ("primary", "backup"):
                origin.setdefault(a.vod.video_id, (path.name, a))
    sp.approved_picks(selections)  # raises if the selections disagree
    wanted = [c.lower() for c in channels]
    not_primary = [i for i in listed if i not in origin or origin[i][1].decision != "primary"]
    outside = sorted({origin[i][1].login for i in listed if i in origin} - set(wanted))
    per_channel = Counter(origin[i][1].login for i in listed if i in origin)
    checks.append(
        Check(
            "list: unique IDs, all primaries of the selections, channels in channels.yaml",
            len(set(listed)) == len(listed) and not not_primary and not outside,
            f"{len(listed)} IDs; per channel "
            + ", ".join(f"{c} {per_channel.get(c, 0)}" for c in wanted)
            + (f"; not primaries: {', '.join(not_primary)}" if not_primary else "")
            + (f"; other channels: {', '.join(outside)}" if outside else ""),
        )
    )

    catalog = read_catalog(data_dir)
    keys = sorted({key for _, key in catalog}, key=lambda k: (len(k), k))
    vod_rows = {
        r["video_id"]: r
        for r in pq.read_table(
            data_dir / "processed" / "vods.parquet", schema=tables.VODS
        ).to_pylist()
    }
    channel_rows = pq.read_table(
        data_dir / "processed" / "broadcasters.parquet", schema=tables.BROADCASTERS
    ).to_pylist()
    broadcasters = {str(r["broadcaster_id"]): str(r["login"]) for r in channel_rows}
    retention = {
        str(r["broadcaster_id"]): sp.RETENTION_DAYS.get(str(r["broadcaster_type"]))
        for r in channel_rows
    }
    runs_dir = data_dir / "pilot" / "runs"
    attempts = read_runs(runs_dir)
    clips_dir = data_dir / "processed" / "clips"
    order = listed + [k for k in keys if k not in set(listed)]
    groups = {
        video_id: ("active" if video_id in set(listed) else "historical") for video_id in order
    }

    reports: list[VodReport] = []
    problems: dict[str, list[str]] = {}

    def problem(kind: str, video_id: str) -> None:
        problems.setdefault(kind, []).append(video_id)

    for video_id in order:
        meta = vod_rows.get(video_id)
        steps = {
            step: catalog[(step, video_id)].status for step in STEPS if (step, video_id) in catalog
        }
        tools = {
            step: catalog[(step, video_id)].tool for step in STEPS if (step, video_id) in catalog
        }
        errors = sum(
            1 for step in STEPS if (step, video_id) in catalog and catalog[(step, video_id)].error
        )
        if steps != dict.fromkeys(STEPS, "ok"):
            problem("steps", video_id)
        if meta is None:
            problem("metadata", video_id)
            continue
        if video_id in origin and origin[video_id][1].vod.broadcaster_id != meta["broadcaster_id"]:
            problem("metadata", video_id)
        duration = float(meta["duration_s"])
        created = meta["created_at"]
        login = broadcasters.get(str(meta["broadcaster_id"]), str(meta["broadcaster_id"]))
        run = facts = None
        if video_id in attempts:
            run, facts = run_stats(attempts[video_id], runs_dir)
            if run.state_after != "complete" or run.temp_files_left:
                problem("runs", video_id)
        else:
            problem("runs", video_id)

        clips = labels = labels_min_views = None
        manifests = _manifest(clips_dir, video_id)
        if len(manifests) != 1:
            problem("snapshot", video_id)
        else:
            manifest_path, manifest = manifests[0]
            table_path = clips_dir / str(manifest["file"])
            schema_ok = table_path.is_file() and pq.read_schema(table_path).equals(
                tables.CLIPS, check_metadata=False
            )
            if not schema_ok:
                problem("snapshot", video_id)
            else:
                frame = pl.read_parquet(table_path)
                clips = clip_stats(
                    frame,
                    manifest,
                    vod_created_at=created,
                    vod_duration_s=duration,
                    listing=facts.listing if facts else None,
                )
                labels = label_preview(frame, video_id=video_id, vod_duration_s=duration)
                labels_min_views = label_preview(
                    frame.filter(pl.col("view_count") >= PREVIEW_MIN_VIEWS),
                    video_id=video_id,
                    vod_duration_s=duration,
                )
                if (
                    manifest["snapshot_id"] != manifest_path.stem
                    or manifest["n_clips"] != frame.height
                    or manifest["n_without_vod_offset"] != clips.n_without_offset
                    or not clips.complete
                    or not clips.mature
                    or clips.n_saturated_ranges
                    or frame["clip_id"].n_unique() != frame.height
                    or set(frame["video_id"].to_list()) - {video_id}
                    or set(frame["broadcaster_id"].to_list()) - {str(meta["broadcaster_id"])}
                ):
                    problem("snapshot", video_id)

        chat = None
        chat_file = data_dir / "interim" / "chat" / f"{video_id}.parquet"
        if not chat_file.is_file() or not pq.read_schema(chat_file).equals(
            tables.CHAT_MESSAGES, check_metadata=False
        ):
            problem("chat", video_id)
        else:
            chat = chat_stats(pl.read_parquet(chat_file), vod_duration_s=duration)
            if facts is not None and facts.n_malformed is not None:
                chat = ChatStats(**{**asdict(chat), "n_malformed": facts.n_malformed})
            if (
                chat.n_bad_hashes
                or chat.n_unredacted_mentions
                or not chat.offsets_sorted
                or (chat.min_offset_s is not None and chat.min_offset_s < 0)
            ):
                problem("chat", video_id)

        audio = None
        wav = data_dir / "interim" / "audio" / f"{video_id}.wav"
        raw = sorted(p for p in (data_dir / "raw" / "audio").glob(f"{video_id}.*") if p.is_file())
        if not wav.is_file():
            problem("audio", video_id)
        else:
            audio = audio_stats(wav, raw)
            if (
                (audio.sample_rate_hz, audio.channels, audio.sample_width_bytes)
                != (SAMPLE_RATE_HZ, 1, 2)
                or abs(audio.wav_duration_s - duration) > MEDIA_LENGTH_TOLERANCE_S
                or audio.raw_files != 1
            ):
                problem("audio", video_id)

        reports.append(
            VodReport(
                video_id=video_id,
                login=login,
                group=groups[video_id],
                selection_file=origin[video_id][0] if video_id in origin else None,
                created_at=created.isoformat().replace("+00:00", "Z"),
                duration_s=duration,
                steps=steps,
                tools=tools,
                catalog_errors=errors,
                clips=clips,
                chat=chat,
                audio=audio,
                run=run,
                labels=labels,
                labels_min_views=labels_min_views,
                retention_days=retention.get(str(meta["broadcaster_id"])),
            )
        )

    historical = [k for k in order if groups[k] == "historical"]
    for name, kind in (
        ("catalog: every VOD has vod_meta, clips, chat and audio ok", "steps"),
        (
            "metadata: every VOD has a row in processed/vods.parquet, of its selection's channel",
            "metadata",
        ),
        (
            "clips: one snapshot per VOD, schema, complete, mature, no saturated range, "
            "counts match the manifest, unique clip IDs of this VOD and channel",
            "snapshot",
        ),
        (
            "chat: schema, 64-hex author pseudonyms, no unredacted @mentions, "
            "sorted non-negative offsets",
            "chat",
        ),
        (
            f"audio: one raw file, WAV {SAMPLE_RATE_HZ} Hz mono 16-bit within "
            f"{MEDIA_LENGTH_TOLERANCE_S:g} s of the Helix length",
            "audio",
        ),
        ("runs: last attempt complete, no temporary files left", "runs"),
    ):
        bad = problems.get(kind, [])
        everyone = f"all {len(listed)} active and {len(historical)} historical VODs"
        checks.append(Check(name, not bad, ", ".join(bad) if bad else everyone))

    rows, other = storage(data_dir, groups)
    raw_chat = [r for r in rows if r.category == "raw_chat"]
    checks.append(
        Check(
            "privacy: no raw chat replay left on disk",
            not raw_chat,
            f"{raw_chat[0].files} files" if raw_chat else "raw/chat/ empty",
        )
    )
    unknown = sum(r.by_group.get("unknown", 0) for r in rows)
    checks.append(
        Check(
            "storage: every file in a known category and of a cataloged VOD",
            not other and not unknown,
            (", ".join(other) if other else "none unclassified")
            + (f"; {unknown} bytes of VODs not in the catalog" if unknown else ""),
        )
    )
    return Report(
        channels=tuple(wanted),
        list_label=list_label(vod_list),
        list_sha256=sha256(vod_list),
        selection_files={p.name: sha256(p) for p in selection_paths},
        vods=tuple(reports),
        storage=tuple(rows),
        unclassified=tuple(other),
        checks=tuple(checks),
        run_settings=read_run_settings(runs_dir),
        first_started_at=min(_times(attempts, "started_at"), default=None),
        last_ended_at=max(_times(attempts, "ended_at"), default=None),
    )


# --- Aggregates --------------------------------------------------------------------------------


def _median(values: Sequence[float]) -> float:
    return float(statistics.median(values)) if values else float("nan")


def nearest_rank(values: Sequence[float], q: float) -> float:
    """The `q` quantile by the nearest-rank method (a value that occurs in `values`)."""
    ordered = sorted(values)
    if not ordered:
        return float("nan")
    rank = math.ceil(q * len(ordered) - 1e-9)  # the tolerance absorbs float error in q * n
    return float(ordered[min(len(ordered), max(rank, 1)) - 1])


def memory_fit(vods: Sequence[VodReport]) -> tuple[float, float] | None:
    """Least-squares peak tree memory (MiB) = intercept + slope * chat messages."""
    points = [
        (v.chat.n_messages, v.run.peak_job_memory / MIB)
        for v in vods
        if v.chat and v.run and v.run.peak_job_memory
    ]
    if len(points) < 2:
        return None
    fit = statistics.linear_regression([p[0] for p in points], [p[1] for p in points])
    return fit.intercept, fit.slope


@dataclass(frozen=True, slots=True)
class Totals:
    """Sums over a set of VODs."""

    n_vods: int
    hours: float
    clips: int
    without_offset: int
    saturated_ranges: int
    one_view: int
    messages: int
    authors: int
    chat_ok: int
    runtime_s: float
    raw_audio_bytes: int
    wav_bytes: int
    chat_bytes: int
    clip_bytes: int


def totals(vods: Sequence[VodReport], data_dir: Path) -> Totals:
    """Sums over `vods`; unique authors are counted across them (pseudonyms only)."""
    authors: set[str] = set()
    for v in vods:
        path = data_dir / "interim" / "chat" / f"{v.video_id}.parquet"
        if path.is_file():
            authors.update(pl.read_parquet(path, columns=["user_hash"])["user_hash"].to_list())
    clip_bytes = 0
    for v in vods:
        for p in (data_dir / "processed" / "clips").glob(f"{v.video_id}-*"):
            clip_bytes += p.stat().st_size
    chat_files = [data_dir / "interim" / "chat" / f"{v.video_id}.parquet" for v in vods]
    return Totals(
        n_vods=len(vods),
        hours=sum(v.hours for v in vods),
        clips=sum(v.clips.n_clips for v in vods if v.clips),
        without_offset=sum(v.clips.n_without_offset for v in vods if v.clips),
        saturated_ranges=sum(v.clips.n_saturated_ranges for v in vods if v.clips),
        one_view=sum(sum(1 for x in v.clips.views if x == 1) for v in vods if v.clips),
        messages=sum(v.chat.n_messages for v in vods if v.chat),
        authors=len(authors),
        chat_ok=sum(1 for v in vods if v.steps.get("chat") == "ok"),
        runtime_s=sum(v.run.total_s for v in vods if v.run),
        raw_audio_bytes=sum(v.audio.raw_bytes for v in vods if v.audio),
        wav_bytes=sum(v.audio.wav_bytes for v in vods if v.audio),
        chat_bytes=sum(p.stat().st_size for p in chat_files if p.is_file()),
        clip_bytes=clip_bytes,
    )


# --- Markdown ----------------------------------------------------------------------------------


def _n(value: float, digits: int = 0) -> str:
    return f"{value:,.{digits}f}"


def _pct(part: float, whole: float, digits: int = 0) -> str:
    return NA if not whole else f"{100 * part / whole:.{digits}f} %"


def _gb(size: float) -> str:
    return f"{size / 1e9:,.2f} GB"


def _mb(size: float) -> str:
    return f"{size / 1e6:,.1f} MB"


def _size(size: float) -> str:
    """Decimal units: kB below 1 MB, MB below 1 GB, then GB."""
    if size < 1e6:
        return f"{size / 1e3:,.1f} kB"
    return _mb(size) if size < 1e9 else _gb(size)


def _range(values: Sequence[float], digits: int = 0, unit: str = "") -> str:
    if not values:
        return NA
    return f"{_n(min(values), digits)}{EN_DASH}{_n(max(values), digits)}{unit}"


def _table(header: Sequence[str], rows: Iterable[Sequence[str]], align: str = "") -> str:
    align = align or "l" + "r" * (len(header) - 1)
    marks = {"l": "---", "r": "--:", "c": ":-:"}
    lines = [
        "| " + " | ".join(header) + " |",
        "|" + "|".join(marks[a] for a in align) + "|",
    ]
    lines += ["| " + " | ".join(row) + " |" for row in rows]
    return "\n".join(lines) + "\n"


def render_blocks(report: Report, data_dir: Path) -> dict[str, str]:
    """The report's generated Markdown blocks, by name."""
    active = report.group("active")
    historical = report.group("historical")
    t = totals(active, data_dir)
    blocks: dict[str, str] = {}

    phase_sum: defaultdict[str, float] = defaultdict(float)
    for v in active:
        for name, seconds in (v.run.phases_s if v.run else {}).items():
            phase_sum[name] += seconds or 0.0
    disk = t.raw_audio_bytes + t.wav_bytes + t.chat_bytes + t.clip_bytes
    blocks["issue-metrics"] = _table(
        ["Metric (issue #35)", "Value"],
        [
            [
                "Download time (ingest wall time, all steps)",
                f"{_n(t.runtime_s)} s ({t.runtime_s / 3600:.2f} h) for {t.hours:.2f} VOD-hours: "
                f"{t.runtime_s / 60 / t.hours:.2f} min per VOD-hour",
            ],
            [
                "Audio download alone",
                f"{_n(phase_sum['audio_download'])} s "
                f"({_pct(phase_sum['audio_download'], t.runtime_s)} of the wall time)",
            ],
            [
                "Disk use (active pilot outputs)",
                f"{_gb(disk)}: raw audio {_gb(t.raw_audio_bytes)}, WAV {_gb(t.wav_bytes)}, "
                f"chat {_mb(t.chat_bytes)}, clips {_mb(t.clip_bytes)}",
            ],
            [
                "Disk use per VOD-hour",
                f"{_mb(disk / t.hours)} (raw audio only: {_mb(t.raw_audio_bytes / t.hours)})",
            ],
            ["Chat success rate", f"{t.chat_ok} / {t.n_vods} VODs ({_pct(t.chat_ok, t.n_vods)})"],
            [
                "Clips per VOD-hour",
                f"{t.clips / t.hours:.1f} overall ({_n(t.clips)} clips); per VOD "
                + _range([v.clips.n_clips / v.hours for v in active if v.clips], 1),
            ],
            [
                "Clips without a VOD offset",
                f"{_n(t.without_offset)} of {_n(t.clips)} ({_pct(t.without_offset, t.clips, 1)})",
            ],
            [
                "Clip-search ranges that hit the clip limit",
                f"{t.saturated_ranges} of "
                f"{_n(sum(v.clips.n_ranges_queried for v in active if v.clips))} ranges queried",
            ],
        ],
        "ll",
    )

    rows = []
    for login in report.channels:
        vs = [v for v in active if v.login == login]
        if not vs:
            continue
        ct = totals(vs, data_dir)
        rows.append(
            [
                login,
                str(ct.n_vods),
                f"{ct.hours:.2f}",
                _n(ct.clips),
                f"{ct.clips / ct.hours:.1f}",
                _pct(ct.one_view, ct.clips),
                _n(ct.messages),
                _n(ct.messages / ct.hours),
                _n(ct.authors),
                _n(ct.runtime_s),
            ]
        )
    rows.append(
        [
            "**all**",
            str(t.n_vods),
            f"{t.hours:.2f}",
            _n(t.clips),
            f"{t.clips / t.hours:.1f}",
            _pct(t.one_view, t.clips),
            _n(t.messages),
            _n(t.messages / t.hours),
            _n(t.authors),
            _n(t.runtime_s),
        ]
    )
    blocks["channels"] = _table(
        [
            "Channel",
            "VODs",
            "Hours",
            "Clips",
            "Clips/h",
            "1-view clips",
            "Chat messages",
            "Messages/h",
            "Unique chatters",
            "Ingest s",
        ],
        rows,
    )

    vod_rows = []
    for n, v in enumerate(active, start=1):
        c, ch, a, r = v.clips, v.chat, v.audio, v.run
        dl = r.phases_s.get("audio_download") if r else None
        vod_rows.append(
            [
                str(n),
                v.login,
                f"`{v.video_id}`",
                v.created_at[:10],
                f"{v.hours:.2f}",
                f"{c.vod_age_days:.1f}" if c else NA,
                _n(c.n_clips) if c else NA,
                f"{c.n_clips / v.hours:.1f}" if c else NA,
                _n(ch.n_messages) if ch else NA,
                _n(r.total_s) if r else NA,
                _n(dl) if dl is not None else NA,
                f"{a.raw_bytes / MIB / dl:.2f}" if a and dl else NA,
                _n(r.peak_job_memory / MIB) if r and r.peak_job_memory else NA,
                v.estimated_expiry or NA,
            ]
        )
    blocks["vods"] = _table(
        [
            "#",
            "Channel",
            "VOD",
            "Recorded (UTC)",
            "Hours",
            "Age at capture (d)",
            "Clips",
            "Clips/h",
            "Chat messages",
            "Ingest s",
            "Audio download s",
            "MiB/s",
            "Peak MiB",
            "Est. expiry",
        ],
        vod_rows,
        "lllrrrrrrrrrrr",
    )

    views = [x for v in active if v.clips for x in v.clips.views]
    shares = [
        v.clips.densest_window / v.clips.n_clips for v in active if v.clips and v.clips.n_clips
    ]
    one_view_shares = [
        sum(1 for x in v.clips.views if x == 1) / v.clips.n_clips
        for v in active
        if v.clips and v.clips.n_clips
    ]
    start_after = [v for v in active if v.clips and v.clips.n_start_after_end]
    end_after = [v for v in active if v.clips and v.clips.n_end_after_end]
    made_after = sum(v.clips.n_made_after_stream for v in active if v.clips)
    blocks["clips"] = _table(
        ["Clip measure (active pilot)", "Value"],
        [
            ["Clips", _n(len(views))],
            [
                "Views: median / p90 / p99 / max (nearest rank)",
                " / ".join(_n(nearest_rank(views, q)) for q in (0.5, 0.9, 0.99))
                + f" / {_n(max(views)) if views else NA}",
            ],
            ["Clips with 1 view", f"{_n(t.one_view)} ({_pct(t.one_view, len(views))})"],
            [
                "1-view share per VOD",
                _range([100 * s for s in one_view_shares], unit=" %"),
            ],
            *[
                [f"Clips with ≥ {k} views", _n(sum(1 for x in views if x >= k))]
                for k in VIEW_THRESHOLDS
            ],
            [
                "Made after the stream ended",
                f"{_n(made_after)} ({_pct(made_after, len(views))})",
            ],
            [
                "Share of a VOD's clips in its densest 20 minutes",
                f"{_range([100 * s for s in shares], unit=' %')} "
                f"(median {100 * _median(shares):.0f} %)",
            ],
            [
                "Clips starting at or after the reported VOD end",
                f"{sum(v.clips.n_start_after_end for v in start_after if v.clips)} "
                f"in {len(start_after)} VODs",
            ],
            [
                "Clips ending after the reported VOD end",
                f"{sum(v.clips.n_end_after_end for v in end_after if v.clips)} "
                f"in {len(end_after)} VODs",
            ],
            [
                "Clip snapshots complete / mature",
                f"{sum(1 for v in active if v.clips and v.clips.complete)} / "
                f"{sum(1 for v in active if v.clips and v.clips.mature)} of {t.n_vods}",
            ],
            [
                "VOD age at capture",
                _range([v.clips.vod_age_days for v in active if v.clips], 1, " days"),
            ],
        ],
        "ll",
    )

    after = [v for v in active if v.chat and v.chat.n_after_end]
    per_vod_authors = sum(v.chat.n_authors for v in active if v.chat)
    malformed = [v.chat.n_malformed for v in active if v.chat and v.chat.n_malformed is not None]
    blocks["chat"] = _table(
        ["Chat measure (active pilot)", "Value"],
        [
            ["Messages", _n(t.messages)],
            ["Messages per VOD", _range([v.chat.n_messages for v in active if v.chat])],
            [
                "Messages per VOD-hour",
                f"{_n(t.messages / t.hours)} overall; per VOD "
                + _range([v.chat.n_messages / v.hours for v in active if v.chat]),
            ],
            [
                "Unique chatter pseudonyms",
                f"{_n(t.authors)} across the pilot; {_n(per_vod_authors)} summed per VOD",
            ],
            [
                "Messages after the reported VOD end",
                f"{_n(sum(v.chat.n_after_end for v in after if v.chat))} in {len(after)} VODs",
            ],
            [
                "Malformed comments skipped by the parser",
                f"{sum(malformed)} (logged for {len(malformed)} VODs)",
            ],
            [
                "Pseudonyms that aren't 64-hex / unredacted @mentions",
                f"{sum(v.chat.n_bad_hashes for v in active if v.chat)} / "
                f"{sum(v.chat.n_unredacted_mentions for v in active if v.chat)}",
            ],
            [
                "Chat tool",
                ", ".join(sorted({v.tools.get("chat", NA) for v in active})),
            ],
        ],
        "ll",
    )

    audio = [v for v in active if v.audio]
    diffs = [v.audio.wav_duration_s - v.duration_s for v in audio if v.audio]
    bitrates = [v.audio.raw_bytes * 8 / v.duration_s / 1000 for v in audio if v.audio]
    blocks["audio"] = _table(
        ["Audio measure (active pilot)", "Value"],
        [
            [
                "WAV format",
                ", ".join(
                    sorted(
                        {
                            f"{a.sample_rate_hz} Hz, {a.channels} ch, "
                            f"{8 * a.sample_width_bytes}-bit"
                            for v in audio
                            if (a := v.audio)
                        }
                    )
                ),
            ],
            ["WAV length minus Helix length", _range(diffs, 2, " s")],
            ["Raw audio container bitrate", _range(bitrates, unit=" kb/s")],
            [
                "Raw audio per VOD-hour",
                f"{_mb(t.raw_audio_bytes / t.hours)} (WAV {_mb(t.wav_bytes / t.hours)})",
            ],
            [
                "Audio tool",
                ", ".join(sorted({v.tools.get("audio", NA) for v in active})),
            ],
        ],
        "ll",
    )

    runs = [v.run for v in active if v.run]
    peaks = [r.peak_job_memory / MIB for r in runs if r.peak_job_memory]
    free = [r.system_memory_free_min / GIB for r in runs if r.system_memory_free_min]
    fit = memory_fit(active)
    perf_rows = [
        [
            "Attempts",
            f"{sum(r.attempts for r in runs)} for {len(runs)} VODs "
            f"({sum(1 for r in runs if r.attempts == 1)} on the first attempt)",
        ],
        [
            "Ingest wall time per VOD",
            f"{_range([r.total_s for r in runs], unit=' s')} "
            f"(median {_n(_median([r.total_s for r in runs]))} s)",
        ],
        [
            "Ingest minutes per VOD-hour",
            _range([v.run.total_s / 60 / v.hours for v in active if v.run], 2),
        ],
    ]
    for name, label in (
        ("metadata", "Phase: metadata and token"),
        ("clips", "Phase: clip snapshot"),
        ("chat", "Phase: chat download and parse"),
        ("audio_download", "Phase: audio download"),
        ("normalize", "Phase: WAV normalization"),
    ):
        perf_rows.append(
            [label, f"{_n(phase_sum[name])} s ({_pct(phase_sum[name], t.runtime_s, 1)})"]
        )
    rates = [
        v.audio.raw_bytes / MIB / dl
        for v in active
        if v.audio and v.run and (dl := v.run.phases_s.get("audio_download"))
    ]
    perf_rows += [
        ["Audio download speed", f"{_range(rates, 2, ' MiB/s')} (median {_median(rates):.2f})"],
        ["Peak process-tree memory", _range(peaks, unit=" MiB")],
        ["Lowest free system memory during a run", _range(free, 2, " GiB")],
        [
            "Peak memory model (least squares)",
            f"{fit[0]:,.0f} MiB + {fit[1] * 1024:.2f} KiB per chat message" if fit else NA,
        ],
    ]
    blocks["performance"] = _table(["Run measure (active pilot)", "Value"], perf_rows, "ll")

    labels = dict(CATEGORIES)
    # The lock file and local notes aren't part of the dataset (a shared copy may lack them),
    # so the table leaves them out; `report.storage` and --json still count them.
    dataset = [r for r in report.storage if r.category not in OUTSIDE_DATASET]
    storage_rows = []
    for row in dataset:
        per_vod = set(row.by_group) != {"shared"}
        storage_rows.append(
            [
                labels[row.category],
                str(row.files),
                _size(row.bytes),
                _size(row.by_group.get("active", 0)) if per_vod else NA,
                _size(row.by_group.get("historical", 0)) if per_vod else NA,
            ]
        )
    storage_rows.append(
        [
            "**Total (dataset)**",
            str(sum(r.files for r in dataset)),
            _size(sum(r.bytes for r in dataset)),
            _size(sum(r.by_group.get("active", 0) for r in dataset)),
            _size(sum(r.by_group.get("historical", 0) for r in dataset)),
        ]
    )
    blocks["storage"] = _table(
        ["Category", "Files", "Size", "Active VODs", "Historical VODs"], storage_rows
    )

    hist_rows = []
    for v in historical:
        hist_rows.append(
            [
                v.login,
                f"`{v.video_id}`",
                f"{v.hours:.2f}",
                ", ".join(f"{k} {s}" for k, s in v.steps.items()),
                _n(v.clips.n_clips) if v.clips else NA,
                _n(v.chat.n_messages) if v.chat else NA,
                _mb(v.audio.raw_bytes + v.audio.wav_bytes) if v.audio else NA,
            ]
        )
    blocks["historical"] = _table(
        ["Channel", "VOD", "Hours", "Catalog", "Clips", "Chat messages", "Audio"],
        hist_rows,
        "llrlrrr",
    )

    label_rows = []
    for login in [*report.channels, None]:
        vs = [v for v in active if v.labels and (login is None or v.login == login)]
        if not vs:
            continue
        w = sum(v.labels.n_windows for v in vs if v.labels)
        pos = sum(v.labels.n_positive for v in vs if v.labels)
        neg = sum(v.labels.n_negative for v in vs if v.labels)
        neg_shares = [
            100 * v.labels.n_negative / v.labels.n_windows
            for v in vs
            if v.labels and v.labels.n_windows
        ]
        strict = [v.labels_min_views for v in vs if v.labels_min_views]
        strict_pos = sum(x.n_positive for x in strict)
        strict_neg = sum(x.n_negative for x in strict)
        label_rows.append(
            [
                login or "**all**",
                _n(w),
                _n(sum(v.labels.n_moments for v in vs if v.labels)),
                _pct(pos, w, 1),
                _pct(neg, w, 1),
                _pct(w - pos - neg, w, 1),
                _range(neg_shares, unit=" %"),
                f"{_pct(strict_pos, w, 1)} / {_pct(strict_neg, w, 1)}",
            ]
        )
    blocks["labels-preview"] = _table(
        [
            "Channel",
            "10 s windows",
            "Moments",
            "Positive",
            f"Negative (≥ {NEG_BUFFER_S:g} s away)",
            "Buffer",
            "Negative share per VOD",
            f"Positive / negative, clips ≥ {PREVIEW_MIN_VIEWS} views only",
        ],
        label_rows,
    )

    blocks["checks"] = _table(
        ["Check", "Result", "Detail"],
        [[c.name, "pass" if c.ok else "**FAIL**", c.detail] for c in report.checks],
        "lll",
    )

    prov_rows = [[f"`{report.list_label}`", f"`{report.list_sha256}`"]]
    prov_rows.append(
        [
            "Collection window (UTC, all runs)",
            f"{(report.first_started_at or NA)[:19]} to {(report.last_ended_at or NA)[:19]}",
        ]
    )
    settings = Counter(describe_settings(x) for x in report.run_settings.values())
    prov_rows += [
        [f"Runner settings ({n} of {len(report.run_settings)} runs)", text]
        for text, n in sorted(settings.items())
    ]
    prov_rows += [
        [f"`pilot/selections/{name}`", f"`{digest}`"]
        for name, digest in report.selection_files.items()
    ]
    tools = Counter(tool for v in active for tool in v.tools.values())
    prov_rows += [
        [f"Tool `{tool}`", f"{n} catalog rows of active VODs"] for tool, n in sorted(tools.items())
    ]
    by_selection = Counter(v.selection_file for v in active)
    prov_rows += [
        [f"VODs picked in `{name}`", str(n)] for name, n in sorted(by_selection.items(), key=str)
    ]
    blocks["provenance"] = _table(["Input", "SHA-256 / count"], prov_rows, "ll")
    return blocks


def render_document(blocks: Mapping[str, str]) -> str:
    """All blocks, each under a heading and between its markers (the stdout format)."""
    parts = []
    for name, body in blocks.items():
        parts.append(f"## {name}\n\n{BEGIN.format(name)}\n{body}{END.format(name)}\n")
    return "\n".join(parts)


def doc_blocks(text: str) -> dict[str, str]:
    """The generated blocks of a Markdown file, by name."""
    return {m[2]: m[3] for m in _BLOCK.finditer(text)}


def update_document(text: str, blocks: Mapping[str, str]) -> tuple[str, list[str]]:
    """`text` with each known block replaced; also the names of blocks it lacks."""
    found = set()

    def replace(match: re.Match[str]) -> str:
        name = match[2]
        if name not in blocks:
            return match[0]
        found.add(name)
        return f"{match[1]}{blocks[name]}{match[4]}"

    return _BLOCK.sub(replace, text), [n for n in blocks if n not in found]


def check_document(text: str, blocks: Mapping[str, str]) -> list[str]:
    """Problems with a document's generated blocks; empty when all match."""
    found = doc_blocks(text)
    problems = [f"block {name!r} is missing" for name in blocks if name not in found]
    problems += [f"block {name!r} is unknown" for name in found if name not in blocks]
    problems += [
        f"block {name!r} differs from the data"
        for name in blocks
        if name in found and found[name] != blocks[name]
    ]
    return problems


def report_json(report: Report, blocks: Mapping[str, str]) -> dict[str, Any]:
    """Every measurement, for ``--json``."""
    return {
        "channels": list(report.channels),
        "list": report.list_label,
        "list_sha256": report.list_sha256,
        "selection_files": report.selection_files,
        "vods": [{**asdict(v), "hours": v.hours} for v in report.vods],
        "storage": [asdict(r) for r in report.storage],
        "unclassified": list(report.unclassified),
        "checks": [asdict(c) for c in report.checks],
        "ok": report.ok,
        "blocks": dict(blocks),
    }


# --- Command line ------------------------------------------------------------------------------

app = typer.Typer(add_completion=False, help="Report on the pilot collection (read only).")


def _utf8_output() -> None:
    """Write stdout and stderr as UTF-8, like the report files.

    The tables contain non-ASCII text ("≥", the en dash). A redirected stdout on Windows uses
    the legacy code page, and printing would fail with UnicodeEncodeError.
    """
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8")


def _fail(message: str) -> typer.Exit:
    typer.echo(f"Error: {message}", err=True)
    return typer.Exit(ExitCode.USER_ERROR)


@app.command()
def main(
    data_dir: Annotated[
        Path | None, typer.Option(help="Data directory. Default: AUTOCLIPPER_DATA_DIR.")
    ] = None,
    vods: Annotated[Path, typer.Option(help="The active pilot list.")] = sp.DEFAULT_LIST,
    selection: Annotated[
        list[Path] | None,
        typer.Option(help="A saved selection; repeat for several. Default: all of them."),
    ] = None,
    channels: Annotated[Path, typer.Option(help="The channels file.")] = sp.DEFAULT_CHANNELS,
    json_out: Annotated[
        Path | None, typer.Option("--json", help="Also write every measurement to this file.")
    ] = None,
    check: Annotated[
        Path | None, typer.Option(help="Exit 1 if this document's generated blocks are stale.")
    ] = None,
    update: Annotated[
        Path | None, typer.Option(help="Rewrite this document's generated blocks.")
    ] = None,
) -> None:
    """Measure the pilot collection and print (or check) the report's tables."""
    _utf8_output()
    if data_dir is None:
        try:
            data_dir = get_settings().data_dir
        except ConfigError as exc:
            raise _fail(str(exc)) from None
    if not (data_dir / "catalog.parquet").is_file():
        raise _fail(f"no catalog in {data_dir}")
    paths = selection or sorted(sp.selections_dir(data_dir).glob("selection-*.json"))
    if not paths:
        raise _fail("no saved selections")
    try:
        report = build_report(
            data_dir,
            vod_list=vods,
            selection_paths=paths,
            channels=sp.load_channel_logins(channels),
        )
    except (OSError, ValueError) as exc:
        raise _fail(str(exc)) from None
    blocks = render_blocks(report, data_dir)
    if json_out is not None:
        json_out.write_text(
            json.dumps(report_json(report, blocks), indent=2, default=str) + "\n",
            encoding="utf-8",
            newline="\n",
        )
    status = ExitCode.SUCCESS if report.ok else ExitCode.USER_ERROR
    if update is not None:
        text, missing = update_document(update.read_text(encoding="utf-8"), blocks)
        update.write_text(text, encoding="utf-8", newline="\n")
        if missing:
            typer.echo(f"not in {update}: {', '.join(missing)}", err=True)
    if check is not None:
        problems = check_document(check.read_text(encoding="utf-8"), blocks)
        for p in problems:
            typer.echo(f"{check}: {p}", err=True)
        if problems:
            status = ExitCode.USER_ERROR
        else:
            typer.echo(f"{check}: every generated block matches the data")
    if check is None and update is None:
        typer.echo(render_document(blocks), nl=False)
    for c in report.checks:
        if not c.ok:
            typer.echo(f"check failed: {c.name}: {c.detail}", err=True)
    raise typer.Exit(status)


if __name__ == "__main__":
    app()
