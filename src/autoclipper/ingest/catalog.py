"""The ingest catalog: what was collected for each VOD, when, with which tool, and how it went.

``<data_dir>/catalog.parquet`` holds the ``catalog`` table (docs/architecture/data-model.md): one
row per ``(object_type, object_key)``, e.g. ``("chat", "<video_id>")``. Recording a step replaces
that object's row, so the file always shows each object's latest state, and a re-run of
``autoclipper ingest`` reads it to skip finished steps. Writes go to a temporary file that
replaces the catalog only once it is complete, so an interrupted write never leaves a broken
catalog. The catalog assumes one ingest process at a time per data directory.
"""

import contextlib
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final, Literal

import pyarrow as pa
import pyarrow.parquet as pq

from autoclipper import tables

type Status = Literal["ok", "failed", "unavailable"]

CATALOG_FILE: Final = "catalog.parquet"


def catalog_path(data_dir: Path) -> Path:
    """``<data_dir>/catalog.parquet``."""
    return data_dir / CATALOG_FILE


@dataclass(frozen=True, slots=True)
class CatalogEntry:
    """One row of the catalog.

    Attributes:
        object_type: ``vod_meta``, ``clips``, ``chat`` or ``audio``.
        object_key: Usually the ``video_id``.
        status: ``ok``, ``failed``, or ``unavailable`` (e.g. the VOD was deleted or is
            subscriber-only).
        tool: What produced it, e.g. ``helix`` or ``TwitchDownloaderCLI@1.56.5``.
        stage_version: The ingest stage's version.
        created_at: When the step finished (UTC).
        error: Why it failed or is unavailable; None when ok.
    """

    object_type: str
    object_key: str
    status: Status
    tool: str
    stage_version: str
    created_at: datetime
    error: str | None = None


def read_catalog(data_dir: Path) -> dict[tuple[str, str], CatalogEntry]:
    """The catalog's rows by ``(object_type, object_key)``; empty if there is no catalog yet."""
    path = catalog_path(data_dir)
    if not path.is_file():
        return {}
    rows = pq.read_table(path, schema=tables.CATALOG).to_pylist()
    entries = [CatalogEntry(**row) for row in rows]
    return {(e.object_type, e.object_key): e for e in entries}


def record(data_dir: Path, entry: CatalogEntry) -> None:
    """Add `entry`, replacing the row of the same object."""
    entries = read_catalog(data_dir)
    entries[(entry.object_type, entry.object_key)] = entry
    rows = [
        {
            "object_type": e.object_type,
            "object_key": e.object_key,
            "status": e.status,
            "tool": e.tool,
            "stage_version": e.stage_version,
            "created_at": e.created_at,
            "error": e.error,
        }
        for _, e in sorted(entries.items())
    ]
    table = pa.Table.from_pylist(rows, schema=tables.CATALOG)
    write_table_atomically(table, catalog_path(data_dir))


def write_table_atomically(table: pa.Table, path: Path) -> None:
    """Write `table` as Parquet to a temporary file next to `path`, then move it into place."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".part", dir=path.parent)
    os.close(handle)
    temp = Path(temp_name)
    try:
        pq.write_table(table, temp)
        temp.replace(path)
    finally:
        with contextlib.suppress(OSError):
            temp.unlink(missing_ok=True)
