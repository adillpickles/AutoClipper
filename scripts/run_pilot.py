"""Run ``autoclipper ingest`` for approved pilot VODs (#35), one at a time, with guards.

Run from the repository root (where ``.env`` is)::

    uv run python scripts/run_pilot.py --dry-run --ids <video_id>   # check and plan; runs nothing
    uv run python scripts/run_pilot.py --ids <video_id>             # exactly that VOD
    uv run python scripts/run_pilot.py                              # configs/pilot_vods.txt

``autoclipper ingest`` does all the work (metadata, clip snapshot, chat, audio, catalog; see
docs/data/collection-plan.md § Ingest). This script only decides *which* VODs to run, runs the
command for each in a child process, and watches it:

- **Approved VODs only.** Every VOD must be a primary or backup of the saved selections
  (``select_pilot_vods.py``), of a channel in ``configs/channels.yaml`` whose broadcaster ID
  matches the selections and ``processed/broadcasters.parquet``, and at least 7 days old now
  (the clip-snapshot maturity rule). Backups run only when named with ``--ids`` and
  ``--allow-backup``; nothing is ever substituted. The estimated expiry risk is reported, never
  enforced. VODs run oldest first. ``--dry-run`` makes no network request and starts nothing.
- **One process tree per VOD.** On Windows the child is created suspended, put in a Job Object
  that kills every process in it when the job's last handle closes, and only then resumed. If
  that setup fails, the child is killed before it has run. A timeout, a guard or Ctrl+C ends the
  whole tree (TwitchDownloaderCLI, FFmpeg) with the job; if this script itself dies, Windows
  closes the handle and ends the tree too. Elsewhere the child gets its own process group, which
  is killed (that doesn't cover this script being killed).
- **Guards.** Before a VOD starts, its expected disk use (estimated from its length and the
  steps still to do, with a safety factor, calibrated from VODs already on disk) must leave
  ``--min-free-gb``. While it runs, free disk, the job's peak committed memory (Windows only) and
  the system's available memory (Windows, and Linux through ``/proc/meminfo``; not measured on
  macOS) are checked every ``--poll-s``; a guard that can't measure is skipped. Crossing a limit
  ends the attempt and stops the batch. A Windows job memory cap backs up the memory check. Only
  the Windows paths have been run; the POSIX ones (process group, ``flock``, ``/proc/meminfo``)
  are untested until their tests run in Linux CI, and macOS is untested.
- **Retries.** Ingest skips finished steps itself, so a retry only redoes what failed. A retry
  happens (at most ``--retries`` times, after ``--retry-delay-min``) only for failures that can
  pass: network and Twitch-availability errors, damaged downloads, tool crashes and timeouts.
- **Outcome per VOD**, from the catalog: ``complete`` (every step ``ok``), ``partial`` (the rest
  ``unavailable``), ``unavailable`` (nothing available), ``failed``, ``incomplete`` (stopped),
  or ``already complete`` (nothing to do). ``unavailable`` never counts as success.
- **Records**, under ``<data_dir>/pilot/runs/``: ``<run_id>.jsonl`` (one line per attempt, plus a
  summary) and ``<run_id>/<video_id>-<attempt>.log`` (the child's output). Both are redacted
  first: URL query strings (Twitch playlist URLs carry access tokens), token-like fields and the
  configured credentials and salt are replaced with ``[redacted]``. Ingest never logs chat text.
- **Cleanup** after a stopped attempt removes only that VOD's raw chat file (it holds usernames);
  finished outputs, other VODs and the catalog are never touched. Partial downloads are kept,
  because the next run resumes them.
- **One runner per data directory**: an OS file lock, released by the OS if the runner dies. It
  doesn't stop someone running ``autoclipper ingest`` by hand at the same time.
- **No sleep while running** (Windows): ``SetThreadExecutionState`` asks Windows not to sleep on
  idle from the first VOD until the run ends. It doesn't block a sleep, restart or shutdown you
  ask for, and Windows drops the request if the process ends.
"""

import contextlib
import ctypes
import json
import os
import random
import re
import shutil
import statistics
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Final, Literal, Protocol, TextIO

import pyarrow.parquet as pq
import select_pilot_vods as sp
import typer

from autoclipper import tables
from autoclipper.cli import ExitCode
from autoclipper.config import ConfigError, MissingSettingError, Settings, get_settings
from autoclipper.ingest.catalog import CatalogEntry, catalog_path, read_catalog
from autoclipper.ingest.media import normalized_audio_path, raw_audio_dir
from autoclipper.ingest.vod import (
    MIN_VOD_AGE,
    STEPS,
    chat_path,
    clips_dir,
    raw_chat_path,
    vods_path,
)
from autoclipper.logging import configure_logging

if sys.platform == "win32":
    import msvcrt
    from ctypes import wintypes
else:
    import fcntl
    import signal

REPO_ROOT: Final = Path(__file__).resolve().parents[1]
GB: Final = 1024**3
MB: Final = 1024**2

# --- Outcomes ----------------------------------------------------------------------------------

type VodState = Literal["complete", "partial", "unavailable", "failed", "incomplete"]
type AttemptOutcome = Literal[
    "exited", "timeout", "interrupted", "stopped_disk", "stopped_memory", "launch_failed"
]
STOPS_BATCH: Final = frozenset({"interrupted", "stopped_disk", "stopped_memory", "launch_failed"})

# Catalog error types worth another attempt: the cause can pass (network, Twitch availability,
# a damaged download fetched again, a tool that crashed or hung). Anything else (bad settings or
# input, a refused format, media of the wrong length) would fail the same way again.
RETRYABLE_ERRORS: Final = frozenset(
    {
        "DownloadNetworkError",
        "DownloadIntegrityError",
        "DownloadToolError",
        "TwitchUnavailableError",
        "TwitchRateLimitError",
        "FFmpegTimeoutError",
        "ConnectError",
        "ConnectTimeout",
        "ReadTimeout",
        "WriteTimeout",
        "PoolTimeout",
        "ReadError",
        "WriteError",
        "RemoteProtocolError",
        "NetworkError",
        "TimeoutException",
    }
)
# ChatDownloadError covers both a failed tool run (can pass) and an unsupported file (can't).
_CHAT_TOOL_FAILED: Final = re.compile(r"chatdownload failed for video_id=\d+ with exit code")
_ERROR_TYPE: Final = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*): ")


def vod_state(entries: dict[str, CatalogEntry | None]) -> VodState:
    """A VOD's state from its four catalog rows (None: the step has no row)."""
    statuses = [None if e is None else e.status for e in entries.values()]
    if all(s == "ok" for s in statuses):
        return "complete"
    if any(s == "failed" for s in statuses):
        return "failed"
    if any(s is None for s in statuses):
        return "incomplete"
    if all(s == "unavailable" for s in statuses):
        return "unavailable"
    return "partial"


def error_type(error: str | None) -> str:
    """The exception type a catalog error starts with (``Type: message``), or ``other``."""
    match = _ERROR_TYPE.match(error or "")
    return match.group(1) if match else "other"


def is_retryable(error: str | None) -> bool:
    """Whether a failed step's catalog error is worth another attempt."""
    kind = error_type(error)
    if kind == "ChatDownloadError":
        return bool(_CHAT_TOOL_FAILED.search(error or ""))
    return kind in RETRYABLE_ERRORS


# --- Redaction ---------------------------------------------------------------------------------

_URL_QUERY: Final = re.compile(r"(https?://[^\s?#\"'<>]+)\?[^\s\"'<>]*")
_SECRET_FIELD: Final = re.compile(
    r"(?i)\b(access_token|refresh_token|client_secret|client_id|token|sig|signature|password"
    r"|authorization|salt)(\"?\s*[=:]\s*\"?)([^\s&\"',;}]+)"
)
_BEARER: Final = re.compile(r"(?i)\b(bearer|oauth)\s+[A-Za-z0-9._~+/=-]{8,}")
MAX_LINE_CHARS: Final = 2000


class Redactor:
    """Removes credentials, tokens and URL query strings from text before it is stored."""

    def __init__(self, secrets: Sequence[str] = ()) -> None:
        """Remember the exact secret values to remove (short values are ignored)."""
        self._secrets = sorted({s for s in secrets if len(s) >= 8}, key=len, reverse=True)

    def __call__(self, text: str) -> str:
        """`text` with every secret replaced by ``[redacted]``, cut to `MAX_LINE_CHARS`."""
        for secret in self._secrets:
            text = text.replace(secret, "[redacted]")
        text = _URL_QUERY.sub(r"\1?[redacted]", text)
        # Before the field rule: in "Authorization: Bearer <token>" it would take "Bearer" as
        # the value and leave the token.
        text = _BEARER.sub(r"\1 [redacted]", text)
        text = _SECRET_FIELD.sub(r"\1\2[redacted]", text)
        return text if len(text) <= MAX_LINE_CHARS else text[:MAX_LINE_CHARS] + "...[cut]"


def redactor_for(settings: Settings) -> Redactor:
    """A `Redactor` for the configured Twitch credentials and chat hash salt."""
    secrets = [
        value.get_secret_value()
        for value in (settings.twitch_client_id, settings.twitch_client_secret)
        if value is not None
    ]
    if settings.chat_hash_salt is not None:
        secrets.append(settings.chat_hash_salt.get_secret_value())
    return Redactor(secrets)


# --- The plan ----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PlannedVod:
    """An approved VOD, as the saved selections describe it."""

    video_id: str
    login: str
    broadcaster_id: str
    designation: Literal["primary", "backup"]
    created_at: datetime
    duration_s: float
    age_days: float
    expiry_risk: sp.ExpiryRisk

    @property
    def hours(self) -> float:
        """Duration in hours."""
        return self.duration_s / 3600


class PlanError(ValueError):
    """The requested VODs can't be run as asked."""


def read_vod_list(path: Path) -> list[str]:
    """The video IDs of a pilot list (one VOD link or ID per line; blank lines skipped).

    Raises:
        PlanError: A line isn't a VOD link or ID.
    """
    ids = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if line.strip():
            video_id = sp.parse_vod(line)
            if video_id is None:
                raise PlanError(f"{path}:{n} isn't a Twitch VOD link or ID")
            ids.append(video_id)
    return ids


def plan_vods(
    selections: Sequence[sp.Selection],
    requested: Sequence[str],
    *,
    channels: Sequence[str],
    broadcasters: dict[str, str] | None,
    allow_backup: bool,
    now: datetime,
    expiry_margin_days: float = 3.0,
) -> list[PlannedVod]:
    """Check the requested VODs against the approved selections; oldest first.

    Args:
        selections: The saved selections (their picks are the approved VODs).
        requested: Video IDs or links to run.
        channels: The logins in ``configs/channels.yaml``.
        broadcasters: login -> broadcaster ID from ``broadcasters.parquet``, if it exists.
        allow_backup: Whether backups may be run (only when named explicitly).
        now: The current time, for the maturity rule.
        expiry_margin_days: As in the selection: days left below which expiry risk is high.

    Raises:
        PlanError: A VOD isn't approved, is a backup without `allow_backup`, belongs to another
            broadcaster than its channel's, or is under 7 days old.
    """
    if not requested:
        raise PlanError("no VODs requested")
    try:
        picks = sp.approved_picks(selections)
        chosen = sp.resolve_approved(picks, requested, channels)
    except ValueError as exc:
        raise PlanError(str(exc)) from None
    channel_ids = {b.login.lower(): b.broadcaster_id for s in selections for b in s.broadcasters}
    plan = []
    for a in chosen:
        vod = a.vod
        if a.decision == "backup" and not allow_backup:
            raise PlanError(f"{vod.video_id} is a backup; run it only with --allow-backup")
        expected = channel_ids[a.login]
        if vod.broadcaster_id != expected:
            raise PlanError(f"{vod.video_id} belongs to {vod.broadcaster_id}, not {a.login}")
        if broadcasters is not None and broadcasters.get(a.login) != expected:
            raise PlanError(f"broadcasters.parquet disagrees with the selections about {a.login}")
        age = now - vod.created_at
        if age < MIN_VOD_AGE:
            raise PlanError(f"{vod.video_id} is under {MIN_VOD_AGE.days} days old")
        risk = sp.expiry_risk(age, a.retention_days, expiry_margin_days)
        designation: Literal["primary", "backup"] = (
            "backup" if a.decision == "backup" else "primary"
        )
        plan.append(
            PlannedVod(
                vod.video_id,
                a.login,
                vod.broadcaster_id,
                designation,
                vod.created_at,
                vod.duration_s,
                age / timedelta(days=1),
                risk,
            )
        )
    return plan


# --- Disk and memory ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Rates:
    """Expected bytes per VOD-hour of each output (before the safety factor)."""

    raw_audio: float = 100 * MB  # Twitch audio-only AAC is about 72 MB/h
    wav: float = 120 * MB  # 16 kHz mono 16-bit: 115.2 MB/h
    raw_chat: float = 300 * MB  # the raw replay, held until parsed; busy chats are large
    chat: float = 30 * MB  # pseudonymized Parquet
    fixed: float = 50 * MB  # metadata, clip snapshot, temporary files


def estimate_bytes(
    hours: float, entries: dict[str, CatalogEntry | None], rates: Rates, factor: float
) -> int:
    """Disk a VOD's remaining steps are expected to need, times the safety factor."""
    done = {step for step, e in entries.items() if e is not None and e.status != "failed"}
    per_hour = 0.0
    if "audio" not in done:
        per_hour += rates.raw_audio + rates.wav
    if "chat" not in done:
        per_hour += rates.raw_chat + rates.chat
    return int((per_hour * hours + rates.fixed) * factor)


def _raw_audio_files(data_dir: Path, video_id: str) -> list[Path]:
    """Finished raw audio downloads (staging files excluded)."""
    return [
        p
        for p in raw_audio_dir(data_dir).glob(f"{video_id}.*")
        if p.is_file() and ".download" not in p.name
    ]


def output_sizes(data_dir: Path, video_id: str) -> dict[str, int]:
    """Bytes on disk of a VOD's outputs, by kind."""

    def size(paths: Sequence[Path]) -> int:
        return sum(p.stat().st_size for p in paths if p.is_file())

    return {
        "raw_audio": size(_raw_audio_files(data_dir, video_id)),
        "wav": size([normalized_audio_path(data_dir, video_id)]),
        "chat": size([chat_path(data_dir, video_id)]),
        "clips": size(sorted(clips_dir(data_dir).glob(f"{video_id}-*"))),
    }


def measured_rates(
    data_dir: Path, hours_by_id: dict[str, float], raw_chat_per_hour: Sequence[float]
) -> Rates:
    """Rates from VODs already on disk (median per VOD-hour); defaults where nothing is measured.

    Args:
        data_dir: The data directory.
        hours_by_id: Durations of the approved VODs.
        raw_chat_per_hour: Peak raw chat file sizes per VOD-hour seen in this run.
    """
    default = Rates()
    samples: dict[str, list[float]] = {"raw_audio": [], "wav": [], "chat": []}
    for video_id, hours in hours_by_id.items():
        if hours <= 0:
            continue
        sizes = output_sizes(data_dir, video_id)
        for kind in samples:
            if sizes[kind] > 0:
                samples[kind].append(sizes[kind] / hours)
    return Rates(
        raw_audio=statistics.median(samples["raw_audio"])
        if samples["raw_audio"]
        else default.raw_audio,
        wav=statistics.median(samples["wav"]) if samples["wav"] else default.wav,
        raw_chat=max(raw_chat_per_hour) if raw_chat_per_hour else default.raw_chat,
        chat=statistics.median(samples["chat"]) if samples["chat"] else default.chat,
        fixed=default.fixed,
    )


def disk_free(path: Path) -> int:
    """Free bytes on the volume holding `path` (or its nearest existing parent)."""
    while not path.exists() and path.parent != path:
        path = path.parent
    return shutil.disk_usage(path).free


def system_memory_free() -> int | None:
    """Bytes of physical memory available to new allocations; None where not measurable."""
    if sys.platform == "win32":
        status = _MEMORYSTATUSEX()
        status.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
        if not _kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return None
        return int(status.ullAvailPhys)
    meminfo = Path("/proc/meminfo")
    if meminfo.is_file():
        for line in meminfo.read_text(encoding="ascii").splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) * 1024
    return None


# --- Windows API -------------------------------------------------------------------------------

if sys.platform == "win32":
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _CREATE_SUSPENDED: Final = 0x00000004
    _CREATE_NEW_PROCESS_GROUP: Final = 0x00000200
    _JOB_BASIC_ACCOUNTING: Final = 1
    _JOB_EXTENDED_LIMITS: Final = 9
    _LIMIT_JOB_MEMORY: Final = 0x00000200
    _LIMIT_KILL_ON_JOB_CLOSE: Final = 0x00002000
    _PROCESS_TERMINATE: Final = 0x0001
    _PROCESS_SET_QUOTA: Final = 0x0100
    _THREAD_SUSPEND_RESUME: Final = 0x0002
    _TH32CS_SNAPTHREAD: Final = 0x00000004
    _INVALID_HANDLE: Final = ctypes.c_void_p(-1).value

    class _IoCounters(ctypes.Structure):
        _fields_ = [
            (name, ctypes.c_ulonglong)
            for name in ("ReadOps", "WriteOps", "OtherOps", "ReadBytes", "WriteBytes", "Other")
        ]

    class _BasicLimits(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _ExtendedLimits(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BasicLimits),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    class _Accounting(ctypes.Structure):
        _fields_ = [
            ("TotalUserTime", ctypes.c_int64),
            ("TotalKernelTime", ctypes.c_int64),
            ("ThisPeriodTotalUserTime", ctypes.c_int64),
            ("ThisPeriodTotalKernelTime", ctypes.c_int64),
            ("TotalPageFaultCount", wintypes.DWORD),
            ("TotalProcesses", wintypes.DWORD),
            ("ActiveProcesses", wintypes.DWORD),
            ("TotalTerminatedProcesses", wintypes.DWORD),
        ]

    class _ThreadEntry(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ThreadID", wintypes.DWORD),
            ("th32OwnerProcessID", wintypes.DWORD),
            ("tpBasePri", ctypes.c_long),
            ("tpDeltaPri", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
        ]

    class _MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", wintypes.DWORD),
            ("dwMemoryLoad", wintypes.DWORD),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    def _fn(name: str, restype: object, *argtypes: object) -> Callable[..., object]:
        function = getattr(_kernel32, name)
        function.restype = restype
        function.argtypes = argtypes
        return function

    _HANDLE = wintypes.HANDLE
    _CreateJobObjectW = _fn("CreateJobObjectW", _HANDLE, ctypes.c_void_p, wintypes.LPCWSTR)
    _SetInformationJobObject = _fn(
        "SetInformationJobObject",
        wintypes.BOOL,
        _HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    )
    _QueryInformationJobObject = _fn(
        "QueryInformationJobObject",
        wintypes.BOOL,
        _HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    )
    _AssignProcessToJobObject = _fn("AssignProcessToJobObject", wintypes.BOOL, _HANDLE, _HANDLE)
    _TerminateJobObject = _fn("TerminateJobObject", wintypes.BOOL, _HANDLE, wintypes.UINT)
    _OpenProcess = _fn("OpenProcess", _HANDLE, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    _OpenThread = _fn("OpenThread", _HANDLE, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    _ResumeThread = _fn("ResumeThread", wintypes.DWORD, _HANDLE)
    _CreateToolhelp32Snapshot = _fn(
        "CreateToolhelp32Snapshot", _HANDLE, wintypes.DWORD, wintypes.DWORD
    )
    _Thread32First = _fn("Thread32First", wintypes.BOOL, _HANDLE, ctypes.POINTER(_ThreadEntry))
    _Thread32Next = _fn("Thread32Next", wintypes.BOOL, _HANDLE, ctypes.POINTER(_ThreadEntry))
    _CloseHandle = _fn("CloseHandle", wintypes.BOOL, _HANDLE)
    _SetThreadExecutionState = _fn("SetThreadExecutionState", wintypes.DWORD, wintypes.DWORD)
    _kernel32.GlobalMemoryStatusEx.argtypes = (ctypes.POINTER(_MEMORYSTATUSEX),)
    _kernel32.GlobalMemoryStatusEx.restype = wintypes.BOOL

    def _check(ok: object, what: str) -> None:
        if not ok:
            raise ctypes.WinError(ctypes.get_last_error(), what)

    def _close_handle(handle: object) -> None:
        _CloseHandle(handle)

    def _create_job(memory_cap: int | None) -> object:
        job = _CreateJobObjectW(None, None)
        _check(job, "CreateJobObjectW")
        limits = _ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = _LIMIT_KILL_ON_JOB_CLOSE
        if memory_cap is not None:
            limits.BasicLimitInformation.LimitFlags |= _LIMIT_JOB_MEMORY
            limits.JobMemoryLimit = memory_cap
        ok = _SetInformationJobObject(
            job, _JOB_EXTENDED_LIMITS, ctypes.byref(limits), ctypes.sizeof(limits)
        )
        if not ok:
            error = ctypes.get_last_error()
            _close_handle(job)
            raise ctypes.WinError(error, "SetInformationJobObject")
        return job

    def _assign_process(job: object, pid: int) -> None:
        process = _OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid)
        _check(process, "OpenProcess")
        try:
            _check(_AssignProcessToJobObject(job, process), "AssignProcessToJobObject")
        finally:
            _close_handle(process)

    def _resume_process(pid: int) -> None:
        """Resume the threads of a process created suspended (it has one)."""
        snapshot = _CreateToolhelp32Snapshot(_TH32CS_SNAPTHREAD, 0)
        if snapshot in (None, _INVALID_HANDLE):
            raise ctypes.WinError(ctypes.get_last_error(), "CreateToolhelp32Snapshot")
        resumed = 0
        try:
            entry = _ThreadEntry()
            entry.dwSize = ctypes.sizeof(_ThreadEntry)
            more = _Thread32First(snapshot, ctypes.byref(entry))
            while more:
                if entry.th32OwnerProcessID == pid:
                    thread = _OpenThread(_THREAD_SUSPEND_RESUME, False, entry.th32ThreadID)
                    _check(thread, "OpenThread")
                    try:
                        previous = _ResumeThread(thread)
                        if previous == 0xFFFFFFFF:
                            raise ctypes.WinError(ctypes.get_last_error(), "ResumeThread")
                        resumed += previous != 0
                    finally:
                        _close_handle(thread)
                more = _Thread32Next(snapshot, ctypes.byref(entry))
        finally:
            _close_handle(snapshot)
        if not resumed:
            raise OSError(f"no suspended thread of process {pid} to resume")

    def _job_info(job: object, kind: int, info: ctypes.Structure) -> None:
        ok = _QueryInformationJobObject(job, kind, ctypes.byref(info), ctypes.sizeof(info), None)
        _check(ok, "QueryInformationJobObject")


ES_CONTINUOUS: Final = 0x80000000
ES_SYSTEM_REQUIRED: Final = 0x00000001


def _set_execution_state(flags: int) -> None:
    if sys.platform == "win32":
        _SetThreadExecutionState(flags)


@contextlib.contextmanager
def keep_awake() -> Iterator[None]:
    """Ask Windows not to sleep on idle until the block ends (nothing elsewhere)."""
    if sys.platform != "win32":
        yield
        return
    _set_execution_state(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
    try:
        yield
    finally:
        _set_execution_state(ES_CONTINUOUS)


# --- The process tree --------------------------------------------------------------------------


class JobSetupError(OSError):
    """The child couldn't be placed under control; it was killed before it ran."""


class Tree(Protocol):
    """A running ingest child and its descendants."""

    def poll(self) -> int | None:
        """The child's exit code, or None while it runs."""
        ...

    def terminate(self) -> None:
        """End every process of the tree and wait for the child."""
        ...

    def peak_memory(self) -> int | None:
        """Peak committed memory of the whole tree so far, or None if not measurable."""
        ...

    def close(self) -> None:
        """End any process left in the tree and release its handles (idempotent)."""
        ...


type Sink = Callable[[str, str], None]
"""Receives each output line of the child: (stream name, line)."""


class ProcessTree:
    """A child process and all its descendants, ended together.

    Windows: a Job Object with kill-on-close; the child is created suspended and resumed only
    once it's in the job. Elsewhere: a new session (process group), killed with SIGKILL.
    """

    def __init__(self, process: subprocess.Popen[str], job: object | None) -> None:
        """Use `start`."""
        self._process = process
        self._job = job
        self._readers: list[threading.Thread] = []

    @classmethod
    def start(
        cls,
        command: Sequence[str],
        *,
        cwd: Path,
        env: dict[str, str],
        sink: Sink,
        memory_cap: int | None = None,
    ) -> "ProcessTree":
        """Start `command` as a controlled tree; its stdout and stderr go to `sink` by line.

        Raises:
            JobSetupError: The job couldn't be created or the child put in it (the child was
                killed before running).
            OSError: The command couldn't be started.
        """
        if sys.platform == "win32":
            try:
                job = _create_job(memory_cap)
            except OSError as exc:
                raise JobSetupError(f"can't create a job object: {exc}") from exc
            try:
                # Suspended: nothing of the child runs until it is in the job. Its own process
                # group: Ctrl+C reaches this script only, which then ends the tree itself.
                process = subprocess.Popen(
                    list(command),
                    cwd=cwd,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    creationflags=_CREATE_SUSPENDED | _CREATE_NEW_PROCESS_GROUP,
                )
            except BaseException:
                _close_handle(job)
                raise
            try:
                _assign_process(job, process.pid)
                _resume_process(process.pid)
            except BaseException as exc:
                process.kill()  # suspended: it never ran
                process.wait()
                for stream in (process.stdout, process.stderr):
                    if stream is not None:
                        stream.close()
                _close_handle(job)
                raise JobSetupError(f"can't put the child in its job: {exc}") from exc
            tree = cls(process, job)
        else:
            process = subprocess.Popen(
                list(command),
                cwd=cwd,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                start_new_session=True,
            )
            tree = cls(process, None)
        for name, stream in (("out", process.stdout), ("err", process.stderr)):
            reader = threading.Thread(target=_pump, args=(stream, name, sink), daemon=True)
            reader.start()
            tree._readers.append(reader)
        return tree

    @property
    def pid(self) -> int:
        """The child's process ID."""
        return self._process.pid

    def poll(self) -> int | None:
        """The child's exit code, or None while it runs."""
        return self._process.poll()

    def wait(self, timeout: float | None = None) -> int:
        """Wait for the child to exit and return its exit code."""
        return self._process.wait(timeout)

    def active_processes(self) -> int | None:
        """Processes still in the job (Windows), or None."""
        if sys.platform == "win32" and self._job is not None:
            info = _Accounting()
            _job_info(self._job, _JOB_BASIC_ACCOUNTING, info)
            return int(info.ActiveProcesses)
        return None

    def peak_memory(self) -> int | None:
        """Peak committed memory of all processes in the job (Windows), or None."""
        if sys.platform == "win32" and self._job is not None:
            info = _ExtendedLimits()
            _job_info(self._job, _JOB_EXTENDED_LIMITS, info)
            return int(info.PeakJobMemoryUsed)
        return None

    def _kill_all(self) -> None:
        if sys.platform == "win32":
            if self._job is not None:
                _TerminateJobObject(self._job, 1)
        else:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(self._process.pid, signal.SIGKILL)

    def terminate(self) -> None:
        """End every process of the tree and wait for the child."""
        self._kill_all()
        if self._process.poll() is None:
            self._process.kill()
        self._process.wait(timeout=60)

    def close(self) -> None:
        """End any process left in the tree, release the job and wait for the output."""
        if self._process.poll() is None:
            self.terminate()
        self._kill_all()  # descendants that outlived the child
        if sys.platform == "win32" and self._job is not None:
            _close_handle(self._job)
            self._job = None
        for reader in self._readers:
            reader.join(timeout=10)
        self._readers.clear()
        for stream in (self._process.stdout, self._process.stderr):
            if stream is not None:
                stream.close()


def _pump(stream: TextIO | None, name: str, sink: Sink) -> None:
    if stream is None:
        return
    with contextlib.suppress(ValueError, OSError):  # closed during shutdown
        for line in stream:
            sink(name, line.rstrip("\r\n"))


# --- One attempt -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Limits:
    """The guards and timing of a run."""

    min_free_bytes: int = 25 * GB
    disk_safety_factor: float = 1.5
    max_job_memory: int = 8 * GB
    min_free_memory: int = 1 * GB
    timeout_base_s: float = 60 * 60
    timeout_per_hour_s: float = 30 * 60
    retries: int = 2
    retry_delays_s: tuple[float, ...] = (10 * 60, 30 * 60)
    delay_s: tuple[float, float] = (30.0, 120.0)
    poll_s: float = 5.0

    def timeout_s(self, hours: float) -> float:
        """The time limit for one attempt of a VOD this long."""
        return self.timeout_base_s + self.timeout_per_hour_s * hours

    def memory_cap(self) -> int:
        """The Windows job's hard memory limit: a backstop above the checked limit."""
        return int(self.max_job_memory * 1.25)


@dataclass
class Deps:
    """What a run calls; tests pass fakes."""

    launch: Callable[[Sequence[str], Sink, int | None], Tree]
    clock: Callable[[], datetime] = lambda: datetime.now(tz=UTC)
    monotonic: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    disk_free: Callable[[Path], int] = disk_free
    memory_free: Callable[[], int | None] = system_memory_free
    rng: random.Random = field(default_factory=random.Random)


@dataclass
class Attempt:
    """One run of ``autoclipper ingest`` for one VOD: a line of the run's JSONL."""

    video_id: str
    login: str
    designation: str
    attempt: int
    started_at: str
    ended_at: str = ""
    duration_s: float = 0.0
    outcome: AttemptOutcome = "exited"
    exit_code: int | None = None
    timeout_s: float = 0.0
    state_before: VodState = "incomplete"
    state_after: VodState = "incomplete"
    steps: dict[str, str | None] = field(default_factory=dict)
    steps_changed: list[str] = field(default_factory=list)
    errors: list[dict[str, str]] = field(default_factory=list)
    retryable: bool = False
    estimated_bytes: int = 0
    disk_free_before: int = 0
    disk_free_after: int = 0
    disk_free_min: int = 0
    peak_job_memory: int | None = None
    system_memory_free_min: int | None = None
    raw_chat_peak_bytes: int = 0
    raw_chat_removed: bool = False
    temp_files_left: list[str] = field(default_factory=list)
    output_bytes: dict[str, int] = field(default_factory=dict)
    log_file: str = ""
    detail: str = ""


def step_entries(data_dir: Path, video_id: str) -> dict[str, CatalogEntry | None]:
    """The VOD's catalog row for each ingest step (None where there is none)."""
    catalog = read_catalog(data_dir)
    return {step: catalog.get((step, video_id)) for step in STEPS}


def _temp_files(data_dir: Path, video_id: str) -> list[str]:
    """Temporary files of this VOD left by an interrupted run (partial downloads resume)."""
    patterns = [
        (raw_audio_dir(data_dir), f"{video_id}.download.*"),
        (clips_dir(data_dir), f".{video_id}-*.part"),
        (chat_path(data_dir, video_id).parent, f".{video_id}.parquet.*.part"),
        (normalized_audio_path(data_dir, video_id).parent, f"*{video_id}*.part"),
    ]
    found = {p.name for directory, pattern in patterns for p in directory.glob(pattern)}
    return sorted(found)


def run_attempt(
    vod: PlannedVod,
    number: int,
    *,
    data_dir: Path,
    command: Sequence[str],
    log_path: Path,
    limits: Limits,
    rates: Rates,
    redact: Redactor,
    deps: Deps,
) -> Attempt:
    """Run ingest once for `vod` under the guards, and record what happened."""
    before = step_entries(data_dir, vod.video_id)
    timeout = limits.timeout_s(vod.hours)
    record = Attempt(
        vod.video_id,
        vod.login,
        vod.designation,
        number,
        started_at=_iso(deps.clock()),
        timeout_s=timeout,
        state_before=vod_state(before),
        estimated_bytes=estimate_bytes(vod.hours, before, rates, limits.disk_safety_factor),
        log_file=str(log_path),
    )
    record.disk_free_before = record.disk_free_min = deps.disk_free(data_dir)
    raw_chat = raw_chat_path(data_dir, vod.video_id)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    lock = threading.Lock()
    started = deps.monotonic()
    with log_path.open("a", encoding="utf-8", newline="\n") as log:

        def sink(stream: str, line: str) -> None:
            with lock:
                log.write(f"{_iso(deps.clock())} {stream} {redact(line)}\n")
                log.flush()

        tree: Tree | None = None
        try:
            tree = deps.launch(command, sink, limits.memory_cap())
            while True:
                code = tree.poll()
                if code is not None:
                    record.outcome, record.exit_code = "exited", code
                    break
                if deps.monotonic() - started > timeout:
                    record.outcome, record.detail = "timeout", f"over {timeout:.0f} s"
                    break
                free = deps.disk_free(data_dir)
                record.disk_free_min = min(record.disk_free_min, free)
                if free < limits.min_free_bytes:
                    record.outcome = "stopped_disk"
                    record.detail = f"free disk {free / GB:.1f} GB < {limits.min_free_bytes / GB:g}"
                    break
                peak = tree.peak_memory()
                if peak is not None:
                    record.peak_job_memory = peak
                    if peak > limits.max_job_memory:
                        record.outcome = "stopped_memory"
                        record.detail = f"job memory {peak / GB:.1f} GB > limit"
                        break
                available = deps.memory_free()
                if available is not None:
                    low = record.system_memory_free_min
                    record.system_memory_free_min = (
                        available if low is None else min(low, available)
                    )
                    if available < limits.min_free_memory:
                        record.outcome = "stopped_memory"
                        record.detail = f"system memory free {available / GB:.1f} GB < limit"
                        break
                with contextlib.suppress(OSError):
                    record.raw_chat_peak_bytes = max(
                        record.raw_chat_peak_bytes, raw_chat.stat().st_size
                    )
                deps.sleep(limits.poll_s)
        except OSError as exc:  # JobSetupError, or the command couldn't start
            record.outcome = "launch_failed"
            record.detail = redact(f"{type(exc).__name__}: {exc}")
        except KeyboardInterrupt:
            record.outcome, record.detail = "interrupted", "Ctrl+C"
        finally:
            if tree is not None:
                if record.outcome != "exited":
                    tree.terminate()
                peak = tree.peak_memory()
                if peak is not None:
                    record.peak_job_memory = peak
                tree.close()
    # Only this VOD's raw chat file: it holds usernames (ingest removes it itself unless killed).
    if raw_chat.exists():
        raw_chat.unlink()
        record.raw_chat_removed = True
    after = step_entries(data_dir, vod.video_id)
    record.ended_at = _iso(deps.clock())
    record.duration_s = round(deps.monotonic() - started, 3)
    record.state_after = vod_state(after)
    record.steps = {step: None if e is None else e.status for step, e in after.items()}
    record.steps_changed = [step for step in STEPS if after[step] != before[step]]
    record.errors = [
        {"step": step, "type": error_type(e.error), "message": redact(e.error or "")[:300]}
        for step, e in after.items()
        if e is not None and e.status == "failed"
    ]
    record.retryable = record.outcome == "timeout" or (
        record.outcome == "exited"
        and record.exit_code == ExitCode.EXTERNAL_FAILURE
        and any(is_retryable(e.error) for e in after.values() if e and e.status == "failed")
    )
    record.disk_free_after = deps.disk_free(data_dir)
    record.temp_files_left = _temp_files(data_dir, vod.video_id)
    record.output_bytes = output_sizes(data_dir, vod.video_id)
    return record


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


# --- The batch ---------------------------------------------------------------------------------


@dataclass
class VodResult:
    """How a VOD ended in this run."""

    video_id: str
    login: str
    designation: str
    result: str  # a VodState, "already complete", "refused_disk" or "not run"
    attempts: int = 0


class RunLockedError(RuntimeError):
    """Another runner holds the data directory's lock."""


@contextlib.contextmanager
def run_lock(path: Path) -> Iterator[None]:
    """Hold an exclusive OS lock on `path`; the OS releases it if the process dies.

    Raises:
        RunLockedError: Another process (or another open of `path`) holds it.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        try:
            if sys.platform == "win32":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise RunLockedError(f"another pilot run holds {path}") from None
        try:
            yield
        finally:
            if sys.platform == "win32":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class RunLog:
    """Appends the run's JSON lines to ``<data_dir>/pilot/runs/<run_id>.jsonl``."""

    def __init__(self, path: Path) -> None:
        """Write to `path` (created if needed)."""
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, kind: str, data: dict[str, object]) -> None:
        """Append one record."""
        line = json.dumps({"record": kind, **data}, sort_keys=True)
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(line + "\n")


def run_batch(
    plan: Sequence[PlannedVod],
    *,
    data_dir: Path,
    run_dir: Path,
    command_for: Callable[[str], list[str]],
    limits: Limits,
    hours_by_id: dict[str, float],
    redact: Redactor,
    log: RunLog,
    deps: Deps,
    say: Callable[[str], None] = lambda _: None,
) -> list[VodResult]:
    """Ingest the planned VODs in order, one at a time; stop the batch when a guard trips."""
    results = [VodResult(v.video_id, v.login, v.designation, "not run") for v in plan]
    raw_chat_rates: list[float] = []
    for index, vod in enumerate(plan):
        result = results[index]
        worked = False
        stop = False
        try:
            while True:
                entries = step_entries(data_dir, vod.video_id)
                if vod_state(entries) == "complete":
                    if result.attempts == 0:
                        result.result = "already complete"
                        log.write("skipped", {"video_id": vod.video_id, "reason": result.result})
                        say(f"{vod.video_id}: already complete")
                    break
                rates = measured_rates(data_dir, hours_by_id, raw_chat_rates)
                need = estimate_bytes(vod.hours, entries, rates, limits.disk_safety_factor)
                free = deps.disk_free(data_dir)
                if free - need < limits.min_free_bytes:
                    result.result, stop = "refused_disk", True
                    log.write(
                        "refused",
                        {
                            "video_id": vod.video_id,
                            "reason": "disk",
                            "disk_free": free,
                            "estimated_bytes": need,
                            "min_free_bytes": limits.min_free_bytes,
                        },
                    )
                    say(
                        f"{vod.video_id}: not started, {free / GB:.1f} GB free < "
                        f"{need / GB:.1f} GB needed + {limits.min_free_bytes / GB:g} GB margin"
                    )
                    break
                result.attempts += 1
                say(f"{vod.video_id} ({vod.login}): attempt {result.attempts}")
                attempt = run_attempt(
                    vod,
                    result.attempts,
                    data_dir=data_dir,
                    command=command_for(vod.video_id),
                    log_path=run_dir / f"{vod.video_id}-{result.attempts}.log",
                    limits=limits,
                    rates=rates,
                    redact=redact,
                    deps=deps,
                )
                log.write("attempt", asdict(attempt))
                worked = worked or bool(attempt.steps_changed) or attempt.outcome != "exited"
                if attempt.raw_chat_peak_bytes and vod.hours > 0:
                    raw_chat_rates.append(attempt.raw_chat_peak_bytes / vod.hours)
                result.result = attempt.state_after
                say(
                    f"{vod.video_id}: {attempt.outcome} -> {attempt.state_after}"
                    + (f" ({attempt.detail})" if attempt.detail else "")
                )
                if attempt.outcome in STOPS_BATCH:
                    stop = True
                    break
                if attempt.state_after in ("complete", "partial", "unavailable"):
                    break
                if not attempt.retryable or result.attempts > limits.retries:
                    break
                wait = limits.retry_delays_s[min(result.attempts, len(limits.retry_delays_s)) - 1]
                say(f"{vod.video_id}: retrying in {wait:.0f} s")
                deps.sleep(wait)
            if not stop and worked and index + 1 < len(plan):
                low, high = limits.delay_s
                pause = deps.rng.uniform(low, high)
                say(f"waiting {pause:.0f} s before the next VOD")
                deps.sleep(pause)
        except KeyboardInterrupt:
            log.write("interrupted", {"video_id": vod.video_id, "between_attempts": True})
            say("interrupted")
            stop = True
        if stop:
            break
    return results


# --- Checks ------------------------------------------------------------------------------------


def ingest_command(video_id: str) -> list[str]:
    """``autoclipper ingest <video_id>`` with this environment's console script."""
    name = "autoclipper.exe" if sys.platform == "win32" else "autoclipper"
    return [str(Path(sys.executable).with_name(name)), "ingest", video_id]


def prerequisites(settings: Settings) -> list[tuple[str, bool, str]]:
    """Local checks only (no network): (what, ok, detail)."""
    from autoclipper.ffmpeg import ffmpeg_version
    from autoclipper.ingest.chat import ChatDownloadError, find_twitchdownloader

    checks: list[tuple[str, bool, str]] = []
    try:
        settings.require_twitch_credentials()
        checks.append(("Twitch credentials", True, "configured (values not shown)"))
    except MissingSettingError as exc:
        checks.append(("Twitch credentials", False, str(exc)))
    try:
        settings.require_chat_hash_salt()
        checks.append(("chat hash salt", True, "configured (value not shown)"))
    except MissingSettingError as exc:
        checks.append(("chat hash salt", False, str(exc)))
    for tool in ("ffmpeg", "ffprobe"):
        found = shutil.which(tool)
        checks.append((tool, found is not None, found or "not on PATH"))
    try:
        checks.append(("ffmpeg version", True, ffmpeg_version()))
    except Exception as exc:  # any failure means it can't be used
        checks.append(("ffmpeg version", False, f"{type(exc).__name__}: {exc}"))
    try:
        checks.append(("TwitchDownloaderCLI", True, str(find_twitchdownloader())))
    except ChatDownloadError as exc:
        checks.append(("TwitchDownloaderCLI", False, str(exc)))
    try:
        from yt_dlp import version as ytdlp_version

        checks.append(("yt-dlp", True, ytdlp_version.__version__))
    except ImportError:
        checks.append(("yt-dlp", False, "not installed (uv sync)"))
    command = Path(ingest_command("0")[0])
    checks.append(("ingest command", command.is_file(), str(command)))
    inside = settings.data_dir.resolve().is_relative_to(REPO_ROOT)
    checks.append(
        (
            "data directory",
            not inside,
            f"{settings.data_dir} (outside the repo)"
            if not inside
            else f"{settings.data_dir} is inside the repository",
        )
    )
    checks.append(
        (
            "process control",
            True,
            "Windows Job Object"
            if sys.platform == "win32"
            else "process group (no cleanup if this script is killed)",
        )
    )
    return checks


# --- Command line ------------------------------------------------------------------------------

app = typer.Typer(add_completion=False, help="Ingest the approved pilot VODs one at a time.")


def _fail(message: str) -> typer.Exit:
    typer.echo(f"Error: {message}", err=True)
    return typer.Exit(ExitCode.USER_ERROR)


def _selection_files(paths: list[Path] | None, data_dir: Path) -> list[Path]:
    if paths:
        return paths
    found = sorted(sp.selections_dir(data_dir).glob("selection-*.json"))
    if not found:
        raise _fail("no saved selections; run select_pilot_vods.py fetch first")
    return found


def _broadcasters(data_dir: Path) -> dict[str, str] | None:
    path = sp.broadcasters_path(data_dir)
    if not path.is_file():
        return None
    rows = pq.read_table(path, schema=tables.BROADCASTERS).to_pylist()
    return {str(r["login"]).lower(): str(r["broadcaster_id"]) for r in rows}


def default_launch(
    cwd: Path, env: dict[str, str]
) -> Callable[[Sequence[str], Sink, int | None], Tree]:
    """The real launcher: a `ProcessTree` in `cwd` with `env`."""

    def launch(command: Sequence[str], sink: Sink, memory_cap: int | None) -> Tree:
        return ProcessTree.start(command, cwd=cwd, env=env, sink=sink, memory_cap=memory_cap)

    return launch


@app.command()
def main(
    ids: Annotated[
        list[str] | None, typer.Option("--ids", help="Run only these VODs (IDs or links).")
    ] = None,
    vods: Annotated[
        Path, typer.Option(help="The pilot list, used when --ids isn't given.")
    ] = REPO_ROOT / "configs" / "pilot_vods.txt",
    selection: Annotated[
        list[Path] | None,
        typer.Option(help="A saved selection; repeat for several. Default: all of them."),
    ] = None,
    channels: Annotated[Path, typer.Option(help="The channels file.")] = sp.DEFAULT_CHANNELS,
    allow_backup: Annotated[
        bool, typer.Option(help="Allow approved backups named with --ids.")
    ] = False,
    dry_run: Annotated[
        bool, typer.Option(help="Check and print the plan; no network, nothing started.")
    ] = False,
    min_free_gb: Annotated[float, typer.Option(help="Free disk to keep.")] = 25.0,
    disk_safety_factor: Annotated[float, typer.Option(help="Multiplier on disk estimates.")] = 1.5,
    max_job_memory_gb: Annotated[float, typer.Option(help="Stop above this tree memory.")] = 8.0,
    min_free_memory_gb: Annotated[
        float, typer.Option(help="Stop below this free system memory.")
    ] = 1.0,
    timeout_base_min: Annotated[float, typer.Option(help="Timeout per attempt, fixed part.")] = 60,
    timeout_per_hour_min: Annotated[
        float, typer.Option(help="Timeout per attempt, per VOD-hour.")
    ] = 30,
    retries: Annotated[int, typer.Option(min=0, max=5, help="Retries per VOD.")] = 2,
    retry_delay_min: Annotated[
        list[float] | None, typer.Option(help="Wait before each retry (repeat).")
    ] = None,
    delay_min_s: Annotated[float, typer.Option(help="Shortest pause between VODs.")] = 30,
    delay_max_s: Annotated[float, typer.Option(help="Longest pause between VODs.")] = 120,
    poll_s: Annotated[float, typer.Option(min=0.5, help="Seconds between checks.")] = 5,
    seed: Annotated[int | None, typer.Option(help="Seed for the pauses.")] = None,
) -> None:
    """Ingest approved pilot VODs one at a time (see the module notes)."""
    try:
        settings = get_settings()
        configure_logging(settings.log_level)
    except ConfigError as exc:
        raise _fail(str(exc)) from None
    data_dir = settings.data_dir
    limits = Limits(
        min_free_bytes=int(min_free_gb * GB),
        disk_safety_factor=disk_safety_factor,
        max_job_memory=int(max_job_memory_gb * GB),
        min_free_memory=int(min_free_memory_gb * GB),
        timeout_base_s=timeout_base_min * 60,
        timeout_per_hour_s=timeout_per_hour_min * 60,
        retries=retries,
        retry_delays_s=tuple(m * 60 for m in (retry_delay_min or [10, 30])),
        delay_s=(delay_min_s, delay_max_s),
        poll_s=poll_s,
    )
    if not 0 <= delay_min_s <= delay_max_s or disk_safety_factor < 1:
        raise _fail("need 0 <= --delay-min-s <= --delay-max-s and --disk-safety-factor >= 1")
    files = _selection_files(selection, data_dir)
    try:
        selections = [sp.selection_from_json(json.loads(p.read_text("utf-8"))) for p in files]
        requested = list(ids) if ids else read_vod_list(vods)
        now = datetime.now(tz=UTC)
        plan = plan_vods(
            selections,
            requested,
            channels=sp.load_channel_logins(channels),
            broadcasters=_broadcasters(data_dir),
            allow_backup=allow_backup,
            now=now,
        )
    except (OSError, ValueError, KeyError) as exc:
        raise _fail(str(exc)) from None
    hours_by_id = {
        a.vod.video_id: a.vod.duration_s / 3600 for a in sp.approved_picks(selections).values()
    }
    checks = prerequisites(settings)
    rates = measured_rates(data_dir, hours_by_id, [])

    typer.echo(f"Selections: {', '.join(p.name for p in files)}")
    typer.echo(f"{len(plan)} VOD(s) in this order (data dir {data_dir}):")
    free = disk_free(data_dir)
    for n, v in enumerate(plan, start=1):
        need = estimate_bytes(
            v.hours, step_entries(data_dir, v.video_id), rates, disk_safety_factor
        )
        typer.echo(
            f"  {n}. {v.video_id} {v.login} ({v.broadcaster_id}) {v.designation}, recorded "
            f"{_iso(v.created_at)}, {v.hours:.2f} h, {v.age_days:.1f} d old (>= 7: yes), expiry "
            f"risk {v.expiry_risk}; state {vod_state(step_entries(data_dir, v.video_id))}; "
            f"disk estimate {need / GB:.2f} GB; timeout {limits.timeout_s(v.hours) / 60:.0f} min"
        )
    typer.echo(f"Free disk now {free / GB:.1f} GB; margin kept {min_free_gb:g} GB.")
    typer.echo("Checks:")
    for what, ok, detail in checks:
        typer.echo(f"  [{'ok' if ok else 'MISSING'}] {what}: {detail}")
    if dry_run:
        outputs = (
            catalog_path(data_dir),
            vods_path(data_dir),
            clips_dir(data_dir),
            chat_path(data_dir, plan[0].video_id).parent,
            normalized_audio_path(data_dir, plan[0].video_id).parent,
            raw_audio_dir(data_dir),
        )
        typer.echo("Outputs: " + ", ".join(str(p) for p in outputs))
        runs = data_dir / "pilot" / "runs"
        typer.echo(f"Run records: {runs / '<run_id>.jsonl'} and {runs / '<run_id>'}")
        typer.echo(f"Dry run: nothing started; {len(plan)} VOD(s) would be processed.")
        if not all(ok for _, ok, _ in checks):
            raise typer.Exit(ExitCode.USER_ERROR)
        return
    if not all(ok for _, ok, _ in checks):
        raise _fail("prerequisites missing (see Checks)")

    run_id = f"{datetime.now(tz=UTC):%Y%m%dT%H%M%SZ}"
    runs = data_dir / "pilot" / "runs"
    log = RunLog(runs / f"{run_id}.jsonl")
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
    deps = Deps(launch=default_launch(Path.cwd(), env), rng=random.Random(seed))
    try:
        with run_lock(data_dir / "pilot" / "run.lock"), keep_awake():
            log.write(
                "start",
                {
                    "run_id": run_id,
                    "videos": [v.video_id for v in plan],
                    "limits": asdict(limits),
                    "selections": [str(p) for p in files],
                },
            )
            results = run_batch(
                plan,
                data_dir=data_dir,
                run_dir=runs / run_id,
                command_for=ingest_command,
                limits=limits,
                hours_by_id=hours_by_id,
                redact=redactor_for(settings),
                log=log,
                deps=deps,
                say=typer.echo,
            )
            log.write("summary", {"results": [asdict(r) for r in results]})
    except RunLockedError as exc:
        raise _fail(str(exc)) from None
    typer.echo("Results:")
    for r in results:
        typer.echo(f"  {r.video_id} {r.login} {r.designation}: {r.result} ({r.attempts} attempts)")
    typer.echo(f"Run record: {log.path}")
    if not all(r.result in ("complete", "already complete") for r in results):
        raise typer.Exit(ExitCode.EXTERNAL_FAILURE)


if __name__ == "__main__":
    app()
