"""Tests for streaming uncompressed ZIP archives of finished renders."""

import io
import os
import zipfile
from pathlib import Path

from autoclipper.export.archive import (
    CHUNK_BYTES,
    MISSING_NOTE_NAME,
    ArchiveMember,
    ArchivePlan,
    stream_archive,
)


def _plan(*members: ArchiveMember, notes: tuple[str, ...] = ()) -> ArchivePlan:
    return ArchivePlan("archive.zip", members, (), notes)


def test_large_files_stream_in_bounded_chunks_and_read_back_intact(tmp_path: Path) -> None:
    big = tmp_path / "exp_big.mp4"
    payload = os.urandom(CHUNK_BYTES * 5 + 123)
    big.write_bytes(payload)
    small = tmp_path / "exp_small.mp4"
    small.write_bytes(b"small clip")
    plan = _plan(
        ArchiveMember(big, "Big_clip.mp4", "Clip 1: gone"),
        ArchiveMember(small, "Small_clip.mp4", "Clip 2: gone"),
    )

    chunks = list(stream_archive(plan))

    # One file chunk plus the ZIP headers around it, never a whole member at once.
    assert max(len(chunk) for chunk in chunks) <= CHUNK_BYTES + 1024
    assert len(chunks) > 5
    with zipfile.ZipFile(io.BytesIO(b"".join(chunks))) as archive:
        assert archive.testzip() is None
        assert archive.namelist() == ["Big_clip.mp4", "Small_clip.mp4"]
        assert {info.compress_type for info in archive.infolist()} == {zipfile.ZIP_STORED}
        assert archive.read("Big_clip.mp4") == payload
        assert archive.read("Small_clip.mp4") == b"small clip"


def test_a_file_removed_after_planning_is_listed_in_the_missing_note(tmp_path: Path) -> None:
    kept = tmp_path / "exp_kept.mp4"
    kept.write_bytes(b"kept")
    plan = _plan(
        ArchiveMember(tmp_path / "exp_gone.mp4", "Gone.mp4", "Clip 1 (exp_gone): file missing"),
        ArchiveMember(kept, "Kept.mp4", "Clip 2 (exp_kept): file missing"),
        notes=("Clip 3 (exp_failed): render failed",),
    )

    data = b"".join(stream_archive(plan))

    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        assert archive.namelist() == ["Kept.mp4", MISSING_NOTE_NAME]
        note = archive.read(MISSING_NOTE_NAME).decode()
    assert "Clip 3 (exp_failed): render failed" in note
    assert "Clip 1 (exp_gone): file missing" in note
    assert "exp_kept" not in note
