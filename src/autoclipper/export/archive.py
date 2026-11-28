"""Stream a job's finished MP4 files as one uncompressed ZIP archive.

MP4s are already compressed, so members are stored (``ZIP_STORED``) and copied in fixed-size
chunks. The archive is written to a non-seekable sink, which makes ``zipfile`` emit data
descriptors after each member instead of seeking back; nothing is staged in memory beyond one
chunk or on disk. Each member records its size up front, so a reel over 4 GiB gets ZIP64 records.
"""

import io
import logging
from collections.abc import Buffer, Generator, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Final
from zipfile import ZIP_STORED, ZipFile, ZipInfo

from autoclipper.schemas import ArchiveOmission

log = logging.getLogger(__name__)

CHUNK_BYTES: Final = 256 * 1024
MISSING_NOTE_NAME: Final = "missing.txt"


@dataclass(frozen=True, slots=True)
class ArchiveMember:
    """A finished file, its download name, and the note to write if it vanishes mid-stream."""

    path: Path
    filename: str
    missing_note: str


@dataclass(frozen=True, slots=True)
class ArchivePlan:
    """What a job's ZIP will contain, snapshotted when the request arrives."""

    filename: str
    members: tuple[ArchiveMember, ...]
    omitted: tuple[ArchiveOmission, ...]
    notes: tuple[str, ...]


class _ChunkSink(io.RawIOBase):
    """A non-seekable destination that hands bytes back after each ZIP write."""

    def __init__(self) -> None:
        super().__init__()
        self._chunks: list[bytes] = []

    def writable(self) -> bool:
        return True

    def write(self, b: Buffer, /) -> int:
        data = bytes(b)
        if data:
            self._chunks.append(data)
        return len(data)

    def drain(self) -> Iterator[bytes]:
        chunks, self._chunks = self._chunks, []
        yield from chunks


def stream_archive(plan: ArchivePlan) -> Generator[bytes]:
    """Yield the ZIP bytes for `plan`, adding ``missing.txt`` when any render is left out.

    Closing the generator early (a client disconnect) closes the member file it has open.
    """
    notes = list(plan.notes)
    sink = _ChunkSink()
    with ZipFile(sink, "w", compression=ZIP_STORED) as archive:
        for member in plan.members:
            try:
                # Size and mtime from the file; zipfile switches to ZIP64 only when it's needed.
                info = ZipInfo.from_file(member.path, member.filename, strict_timestamps=False)
                source = member.path.open("rb")
            except OSError as exc:
                # Usually the file was removed after the plan was built; missing.txt says so.
                log.warning(
                    "archive member could not be opened, listing it in %s file=%s: %s",
                    MISSING_NOTE_NAME,
                    member.path.name,
                    exc,
                )
                notes.append(member.missing_note)
                continue
            info.compress_type = ZIP_STORED
            with source, archive.open(info, "w") as destination:
                while chunk := source.read(CHUNK_BYTES):
                    destination.write(chunk)
                    yield from sink.drain()
            yield from sink.drain()
        if notes:
            text = "\n".join(["Renders left out of this archive:", "", *notes, ""])
            archive.writestr(MISSING_NOTE_NAME, text)
    yield from sink.drain()
