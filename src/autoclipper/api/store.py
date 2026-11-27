"""Durable SQLite index for analysis jobs and renders (docs/architecture/api-contract.md § 9.3).

SQLite (stdlib `sqlite3`, WAL mode) was chosen over a JSON-lines log because multiple worker
threads (analysis, render, fetch) and HTTP request threads update jobs and renders concurrently,
and transactional upserts avoid rewriting the whole store or truncating partial lines on a crash.
Databases confirmed to be corrupt or structurally incomplete are quarantined aside and recreated on
startup; individual corrupt rows are logged and skipped rather than crashing the server.
"""

import contextlib
import json
import logging
import math
import secrets
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Literal, cast

from pydantic import ValidationError

from autoclipper.analysis import AnalysisResult, AnalysisWarning
from autoclipper.schemas import (
    ApiError,
    CreateExportRequest,
    HighlightsSegment,
    Order,
    Progress,
    State,
)

log = logging.getLogger(__name__)

SCHEMA_VERSION: Final = 2
DEFAULT_DB_NAME: Final = "library.db"
DEFAULT_MOCK_DB_NAME: Final = "library-mock.db"
_VALID_STATES: Final = frozenset({"queued", "running", "done", "failed"})
_VALID_RUNNERS: Final = frozenset({"mock", "pipeline"})
_VALID_RENDER_KINDS: Final = frozenset({"export", "highlights"})
_VALID_ORDERS: Final = frozenset({"chronological", "rank"})

# SQLite extended/primary error codes that confirm file corruption or non-database content.
_SQLITE_CORRUPT: Final = 11
_SQLITE_NOTADB: Final = 26

_JOBS_COLUMNS: Final = frozenset(
    {
        "job_id",
        "seq",
        "runner",
        "video_id",
        "video_path",
        "state",
        "title",
        "broadcaster_id",
        "broadcaster_display_name",
        "duration_s",
        "thumbnail_url",
        "candidate_count",
        "result_path",
        "progress_json",
        "warnings_json",
        "error_json",
        "created_at",
        "started_at",
        "updated_at",
        "finished_at",
    }
)

_RENDERS_COLUMNS: Final = frozenset(
    {
        "render_id",
        "job_id",
        "kind",
        "video_id",
        "captions",
        "request_json",
        "segments_json",
        "sort_order",
        "state",
        "progress_json",
        "error_json",
        "file_path",
        "filename",
        "duration_s",
        "created_at",
        "started_at",
        "updated_at",
        "finished_at",
    }
)

_DDL: Final = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    seq INTEGER NOT NULL,
    runner TEXT NOT NULL,
    video_id TEXT NOT NULL,
    video_path TEXT,
    state TEXT NOT NULL,
    title TEXT,
    broadcaster_id TEXT,
    broadcaster_display_name TEXT,
    duration_s REAL,
    thumbnail_url TEXT,
    candidate_count INTEGER,
    result_path TEXT,
    progress_json TEXT,
    warnings_json TEXT NOT NULL,
    error_json TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    updated_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs (created_at DESC, seq DESC, job_id DESC);

CREATE TABLE IF NOT EXISTS renders (
    render_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    video_id TEXT NOT NULL,
    captions INTEGER NOT NULL,
    request_json TEXT,
    segments_json TEXT NOT NULL,
    sort_order TEXT NOT NULL,
    state TEXT NOT NULL,
    progress_json TEXT,
    error_json TEXT,
    file_path TEXT,
    filename TEXT,
    duration_s REAL,
    created_at TEXT NOT NULL,
    started_at TEXT,
    updated_at TEXT NOT NULL,
    finished_at TEXT,
    FOREIGN KEY (job_id) REFERENCES jobs (job_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_renders_job ON renders (job_id, created_at DESC, render_id DESC);
"""


class _CorruptStoreError(Exception):
    """The SQLite database file is confirmed corrupt or has an incomplete/unsupported schema."""


def _is_corrupt_sqlite_error(exc: sqlite3.DatabaseError) -> bool:
    """Return True only when `exc` confirms corruption rather than a transient I/O error."""
    if isinstance(exc, sqlite3.OperationalError):
        msg = str(exc).lower()
        return "not a database" in msg or "malformed" in msg
    code = getattr(exc, "sqlite_errorcode", None)
    if code is not None and (code & 0xFF) in (_SQLITE_CORRUPT, _SQLITE_NOTADB):
        return True
    msg = str(exc).lower()
    return "not a database" in msg or "malformed" in msg or "corrupt" in msg


@dataclass(frozen=True, slots=True)
class StoredJobRecord:
    """One persisted analysis job row."""

    job_id: str
    seq: int
    video_id: str
    video_path: str | None
    state: State
    title: str | None
    broadcaster_id: str | None
    broadcaster_display_name: str | None
    duration_s: float | None
    thumbnail_url: str | None
    candidate_count: int | None
    result_path: Path | None
    progress: Progress | None
    warnings: tuple[AnalysisWarning, ...]
    error: ApiError | None
    created_at: datetime
    started_at: datetime | None
    updated_at: datetime
    finished_at: datetime | None
    runner: Literal["mock", "pipeline"] = "mock"


@dataclass(frozen=True, slots=True)
class StoredRenderRecord:
    """One persisted export or highlights render row."""

    render_id: str
    job_id: str
    kind: Literal["export", "highlights"]
    video_id: str
    captions: bool
    request: CreateExportRequest | None
    segments: tuple[HighlightsSegment, ...]
    order: Order
    state: State
    progress: Progress | None
    error: ApiError | None
    file: Path | None
    duration_s: float | None
    created_at: datetime
    started_at: datetime | None
    updated_at: datetime
    finished_at: datetime | None
    filename: str | None = None


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_dt(raw: object) -> datetime:
    if not isinstance(raw, str):
        raise ValueError("timestamp must be a string")
    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return parsed.astimezone(UTC)


def _parse_opt_dt(raw: object) -> datetime | None:
    return None if raw is None else _parse_dt(raw)


def _parse_opt_str(raw: object, name: str) -> str | None:
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ValueError(f"{name} must be a string or null")
    return raw


def _parse_opt_finite_float(raw: object, name: str, *, allow_zero: bool) -> float | None:
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        raise ValueError(f"{name} must be a number or null")
    value = float(raw)
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    if allow_zero:
        if value < 0:
            raise ValueError(f"{name} must be non-negative")
    elif value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def _parse_opt_non_negative_int(raw: object, name: str) -> int | None:
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ValueError(f"{name} must be an integer or null")
    if raw < 0:
        raise ValueError(f"{name} must be non-negative")
    return raw


def _encode_path(path: Path | None) -> str | None:
    if path is None:
        return None
    return str(path.resolve())


def _parse_opt_path(raw: object, base_dir: Path) -> Path | None:
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw:
        raise ValueError("path must be a non-empty string or null")
    path = Path(raw)
    return path if path.is_absolute() else (base_dir / path).resolve()


def write_result_json(path: Path, result: AnalysisResult) -> None:
    """Atomically write an `AnalysisResult` JSON file to `path`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(f"{path.stem}.{secrets.token_hex(4)}.part")
    try:
        partial.write_text(result.model_dump_json(indent=2) + "\n", encoding="utf-8", newline="\n")
        partial.replace(path)
    finally:
        partial.unlink(missing_ok=True)


def read_result_json(path: Path) -> AnalysisResult | None:
    """Load and validate an `AnalysisResult` JSON file, or return None if missing or corrupt."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        log.warning("couldn't read analysis result path=%s: %s", path, exc)
        return None
    try:
        return AnalysisResult.model_validate_json(text)
    except (ValidationError, ValueError) as exc:
        log.warning("corrupt analysis result JSON path=%s: %s", path, exc)
        return None


class JobStore:
    """Thread-safe SQLite store for jobs and renders under the artifacts directory."""

    def __init__(self, path: Path) -> None:
        self.path = path.resolve()
        self._lock = threading.Lock()
        self._conn = self._open_or_recover()

    def _open_or_recover(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            return self._open_and_verify()
        except _CorruptStoreError as exc:
            log.warning(
                "job store is corrupt or incomplete path=%s error=%s; quarantining and starting "
                "fresh",
                self.path,
                exc,
            )
            self._quarantine_corrupt_files()
            return self._open_and_verify()
        except sqlite3.DatabaseError as exc:
            if not _is_corrupt_sqlite_error(exc):
                raise
            log.warning(
                "job store is corrupt path=%s error=%s; quarantining and starting fresh",
                self.path,
                exc,
            )
            self._quarantine_corrupt_files()
            return self._open_and_verify()

    def _open_and_verify(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            check_row = conn.execute("PRAGMA quick_check").fetchone()
            if check_row is None or check_row[0] != "ok":
                raise _CorruptStoreError(
                    f"integrity check failed: {None if check_row is None else check_row[0]}"
                )
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA foreign_keys=ON")
            version_row = conn.execute("PRAGMA user_version").fetchone()
            user_version = int(version_row[0]) if version_row is not None else 0
            if user_version == 0:
                conn.executescript(_DDL)
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                conn.commit()
            elif user_version == 1:
                existing_tables = {
                    str(row[0])
                    for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
                }
                if "renders" in existing_tables:
                    cols = {str(row["name"]) for row in conn.execute("PRAGMA table_info(renders)")}
                    if "filename" not in cols:
                        conn.execute("ALTER TABLE renders ADD COLUMN filename TEXT")
                    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                    conn.commit()
            elif user_version != SCHEMA_VERSION:
                raise _CorruptStoreError(f"unsupported job store schema version {user_version}")
            self._verify_tables(conn)
            return conn
        except Exception:
            with contextlib.suppress(Exception):
                conn.close()
            raise

    @staticmethod
    def _verify_tables(conn: sqlite3.Connection) -> None:
        for table, expected in (("jobs", _JOBS_COLUMNS), ("renders", _RENDERS_COLUMNS)):
            cols = {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})")}
            if not expected.issubset(cols):
                missing = sorted(expected - cols)
                raise _CorruptStoreError(f"table {table} is missing columns: {', '.join(missing)}")

    def _quarantine_corrupt_files(self) -> None:
        stamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
        suffix = f"corrupt-{stamp}-{secrets.token_hex(4)}"
        if self.path.exists():
            # Never unlink the primary DB if quarantine fails: surface the error instead.
            target = self.path.with_name(f"{self.path.name}.{suffix}")
            self.path.replace(target)
        for sidecar in (Path(f"{self.path}-wal"), Path(f"{self.path}-shm")):
            if not sidecar.exists():
                continue
            target = sidecar.with_name(f"{sidecar.name}.{suffix}")
            with contextlib.suppress(OSError):
                sidecar.replace(target)

    def close(self) -> None:
        """Close the SQLite connection and release file handles."""
        with self._lock:
            with contextlib.suppress(sqlite3.DatabaseError):
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self._conn.close()

    def load_all(self) -> tuple[list[StoredJobRecord], list[StoredRenderRecord]]:
        """Load all valid job and render rows, skipping any individually corrupt rows."""
        base_dir = self.path.parent
        with self._lock:
            job_rows = self._conn.execute(
                "SELECT * FROM jobs ORDER BY seq ASC, created_at ASC, job_id ASC"
            ).fetchall()
            render_rows = self._conn.execute(
                "SELECT * FROM renders ORDER BY created_at ASC, render_id ASC"
            ).fetchall()

        jobs: list[StoredJobRecord] = []
        valid_job_ids: set[str] = set()
        for row in job_rows:
            try:
                record = _row_to_job(row, base_dir)
            except Exception as exc:
                log.warning("skipping corrupt job row job_id=%r: %s", row["job_id"], exc)
                continue
            jobs.append(record)
            valid_job_ids.add(record.job_id)

        renders: list[StoredRenderRecord] = []
        for row in render_rows:
            try:
                render = _row_to_render(row, base_dir)
            except Exception as exc:
                log.warning("skipping corrupt render row render_id=%r: %s", row["render_id"], exc)
                continue
            if render.job_id not in valid_job_ids:
                log.warning(
                    "skipping orphan render row render_id=%s job_id=%s",
                    render.render_id,
                    render.job_id,
                )
                continue
            renders.append(render)

        return jobs, renders

    def upsert_job(self, record: StoredJobRecord) -> None:
        """Insert or update one job record atomically."""
        warnings_json = json.dumps([w.model_dump(mode="json") for w in record.warnings])
        progress_json = record.progress.model_dump_json() if record.progress is not None else None
        error_json = record.error.model_dump_json() if record.error is not None else None
        params = (
            record.job_id,
            record.seq,
            record.runner,
            record.video_id,
            record.video_path,
            record.state,
            record.title,
            record.broadcaster_id,
            record.broadcaster_display_name,
            record.duration_s,
            record.thumbnail_url,
            record.candidate_count,
            _encode_path(record.result_path),
            progress_json,
            warnings_json,
            error_json,
            _iso(record.created_at),
            _iso(record.started_at),
            _iso(record.updated_at),
            _iso(record.finished_at),
        )
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO jobs (
                    job_id, seq, runner, video_id, video_path, state, title, broadcaster_id,
                    broadcaster_display_name, duration_s, thumbnail_url, candidate_count,
                    result_path, progress_json, warnings_json, error_json,
                    created_at, started_at, updated_at, finished_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    seq = excluded.seq,
                    runner = excluded.runner,
                    video_id = excluded.video_id,
                    video_path = excluded.video_path,
                    state = excluded.state,
                    title = excluded.title,
                    broadcaster_id = excluded.broadcaster_id,
                    broadcaster_display_name = excluded.broadcaster_display_name,
                    duration_s = excluded.duration_s,
                    thumbnail_url = excluded.thumbnail_url,
                    candidate_count = excluded.candidate_count,
                    result_path = excluded.result_path,
                    progress_json = excluded.progress_json,
                    warnings_json = excluded.warnings_json,
                    error_json = excluded.error_json,
                    created_at = excluded.created_at,
                    started_at = excluded.started_at,
                    updated_at = excluded.updated_at,
                    finished_at = excluded.finished_at
                """,
                params,
            )

    def upsert_render(self, record: StoredRenderRecord) -> None:
        """Insert or update one export or highlights render record atomically."""
        request_json = record.request.model_dump_json() if record.request is not None else None
        segments_json = json.dumps([s.model_dump(mode="json") for s in record.segments])
        progress_json = record.progress.model_dump_json() if record.progress is not None else None
        error_json = record.error.model_dump_json() if record.error is not None else None
        params = (
            record.render_id,
            record.job_id,
            record.kind,
            record.video_id,
            1 if record.captions else 0,
            request_json,
            segments_json,
            record.order,
            record.state,
            progress_json,
            error_json,
            _encode_path(record.file),
            record.filename,
            record.duration_s,
            _iso(record.created_at),
            _iso(record.started_at),
            _iso(record.updated_at),
            _iso(record.finished_at),
        )
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO renders (
                    render_id, job_id, kind, video_id, captions, request_json, segments_json,
                    sort_order, state, progress_json, error_json, file_path, filename, duration_s,
                    created_at, started_at, updated_at, finished_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(render_id) DO UPDATE SET
                    job_id = excluded.job_id,
                    kind = excluded.kind,
                    video_id = excluded.video_id,
                    captions = excluded.captions,
                    request_json = excluded.request_json,
                    segments_json = excluded.segments_json,
                    sort_order = excluded.sort_order,
                    state = excluded.state,
                    progress_json = excluded.progress_json,
                    error_json = excluded.error_json,
                    file_path = excluded.file_path,
                    filename = excluded.filename,
                    duration_s = excluded.duration_s,
                    created_at = excluded.created_at,
                    started_at = excluded.started_at,
                    updated_at = excluded.updated_at,
                    finished_at = excluded.finished_at
                """,
                params,
            )

    def delete_job(self, job_id: str) -> bool:
        """Delete a job and (via foreign key cascade) its render rows."""
        with self._lock, self._conn:
            cursor = self._conn.execute("DELETE FROM jobs WHERE job_id = ?", (job_id,))
            return cursor.rowcount > 0


def _row_to_job(row: sqlite3.Row, base_dir: Path) -> StoredJobRecord:
    job_id = row["job_id"]
    seq = row["seq"]
    runner = row["runner"]
    video_id = row["video_id"]
    state = row["state"]
    if not isinstance(job_id, str) or not job_id:
        raise ValueError("invalid job_id")
    if isinstance(seq, bool) or not isinstance(seq, int):
        raise ValueError("invalid seq")
    if runner not in _VALID_RUNNERS:
        raise ValueError(f"invalid runner {runner!r}")
    if not isinstance(video_id, str) or not video_id:
        raise ValueError("invalid video_id")
    if state not in _VALID_STATES:
        raise ValueError(f"invalid state {state!r}")
    raw_warnings = json.loads(row["warnings_json"])
    if not isinstance(raw_warnings, list):
        raise ValueError("warnings_json must be a JSON array")
    warnings = tuple(AnalysisWarning.model_validate(item) for item in raw_warnings)
    progress = (
        Progress.model_validate_json(row["progress_json"])
        if row["progress_json"] is not None
        else None
    )
    error = (
        ApiError.model_validate_json(row["error_json"]) if row["error_json"] is not None else None
    )
    return StoredJobRecord(
        job_id=job_id,
        seq=seq,
        runner=cast(Literal["mock", "pipeline"], runner),
        video_id=video_id,
        video_path=_parse_opt_str(row["video_path"], "video_path"),
        state=cast(State, state),
        title=_parse_opt_str(row["title"], "title"),
        broadcaster_id=_parse_opt_str(row["broadcaster_id"], "broadcaster_id"),
        broadcaster_display_name=_parse_opt_str(
            row["broadcaster_display_name"], "broadcaster_display_name"
        ),
        duration_s=_parse_opt_finite_float(row["duration_s"], "duration_s", allow_zero=False),
        thumbnail_url=_parse_opt_str(row["thumbnail_url"], "thumbnail_url"),
        candidate_count=_parse_opt_non_negative_int(row["candidate_count"], "candidate_count"),
        result_path=_parse_opt_path(row["result_path"], base_dir),
        progress=progress,
        warnings=warnings,
        error=error,
        created_at=_parse_dt(row["created_at"]),
        started_at=_parse_opt_dt(row["started_at"]),
        updated_at=_parse_dt(row["updated_at"]),
        finished_at=_parse_opt_dt(row["finished_at"]),
    )


def _row_to_render(row: sqlite3.Row, base_dir: Path) -> StoredRenderRecord:
    render_id = row["render_id"]
    job_id = row["job_id"]
    kind = row["kind"]
    video_id = row["video_id"]
    state = row["state"]
    order = row["sort_order"]
    if not isinstance(render_id, str) or not render_id:
        raise ValueError("invalid render_id")
    if not isinstance(job_id, str) or not job_id:
        raise ValueError("invalid job_id")
    if kind not in _VALID_RENDER_KINDS:
        raise ValueError(f"invalid render kind {kind!r}")
    if not isinstance(video_id, str) or not video_id:
        raise ValueError("invalid video_id")
    if state not in _VALID_STATES:
        raise ValueError(f"invalid render state {state!r}")
    if order not in _VALID_ORDERS:
        raise ValueError(f"invalid order {order!r}")
    request = (
        CreateExportRequest.model_validate_json(row["request_json"])
        if row["request_json"] is not None
        else None
    )
    if kind == "export" and request is None:
        raise ValueError("export render is missing request_json")
    raw_segments = json.loads(row["segments_json"])
    if not isinstance(raw_segments, list):
        raise ValueError("segments_json must be a JSON array")
    segments = tuple(HighlightsSegment.model_validate(item) for item in raw_segments)
    progress = (
        Progress.model_validate_json(row["progress_json"])
        if row["progress_json"] is not None
        else None
    )
    error = (
        ApiError.model_validate_json(row["error_json"]) if row["error_json"] is not None else None
    )
    return StoredRenderRecord(
        render_id=render_id,
        job_id=job_id,
        kind=cast(Literal["export", "highlights"], kind),
        video_id=video_id,
        captions=bool(row["captions"]),
        request=request,
        segments=segments,
        order=cast(Order, order),
        state=cast(State, state),
        progress=progress,
        error=error,
        file=_parse_opt_path(row["file_path"], base_dir),
        duration_s=_parse_opt_finite_float(row["duration_s"], "duration_s", allow_zero=True),
        created_at=_parse_dt(row["created_at"]),
        started_at=_parse_opt_dt(row["started_at"]),
        updated_at=_parse_dt(row["updated_at"]),
        finished_at=_parse_opt_dt(row["finished_at"]),
        filename=_parse_opt_str(row["filename"], "filename"),
    )
