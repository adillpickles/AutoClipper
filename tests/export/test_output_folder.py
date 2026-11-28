"""Tests for validating the output folder and copying finished renders into it."""

import errno
import os
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from autoclipper.export.output_folder import (
    STALE_PARTIAL_AGE_S,
    CopyCancelled,
    OutputFolderError,
    copy_into_folder,
    remove_stale_partials,
    validate_output_folder,
)


def test_a_missing_folder_is_created_and_returned_absolute(tmp_path: Path) -> None:
    folder = tmp_path / "clips" / "nested"

    assert validate_output_folder(f"  {folder}  ") == folder
    assert folder.is_dir()
    assert list(folder.iterdir()) == []  # the write check cleans up after itself


@pytest.mark.parametrize(
    ("make", "reason"),
    [
        (lambda tmp: "relative/folder", "not_absolute"),
        (lambda tmp: str(tmp / "file.mp4"), "not_a_directory"),
        (lambda tmp: str(tmp / "file.mp4" / "below"), "not_a_directory"),
    ],
    ids=["relative", "a-file", "below-a-file"],
)
def test_unusable_folders_are_rejected_without_naming_the_path(
    tmp_path: Path, make: Callable[[Path], str], reason: str
) -> None:
    (tmp_path / "file.mp4").write_bytes(b"x")
    raw = make(tmp_path)

    with pytest.raises(OutputFolderError) as caught:
        validate_output_folder(raw)

    assert caught.value.reason == reason
    assert str(tmp_path) not in caught.value.message
    assert "folder" in caught.value.message


def test_a_folder_that_cant_be_written_is_not_writable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Permission bits can't make a folder read-only on Windows, so the write itself fails here.
    def refuse(*args: object, **kwargs: object) -> object:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(tempfile, "NamedTemporaryFile", refuse)

    with pytest.raises(OutputFolderError) as caught:
        validate_output_folder(str(tmp_path))

    assert caught.value.reason == "not_writable"


def test_a_copy_lands_under_its_name_and_a_clash_gets_the_next_suffix(tmp_path: Path) -> None:
    source = tmp_path / "exp_1.mp4"
    source.write_bytes(b"rendered clip" * 100_000)
    folder = tmp_path / "out"
    folder.mkdir()
    (folder / "CLIP.mp4").write_bytes(b"someone else's file")  # differs only in case
    (folder / "clip_2.mp4").write_bytes(b"another")

    first = copy_into_folder(source, folder, "clip.mp4")
    second = copy_into_folder(source, folder, "clip.mp4")

    assert (first.name, second.name) == ("clip_3.mp4", "clip_4.mp4")
    assert first.read_bytes() == source.read_bytes()
    assert (folder / "CLIP.mp4").read_bytes() == b"someone else's file"
    assert sorted(p.name for p in folder.iterdir()) == [
        "CLIP.mp4",
        "clip_2.mp4",
        "clip_3.mp4",
        "clip_4.mp4",
    ]


def test_a_failed_copy_leaves_nothing_behind(tmp_path: Path) -> None:
    folder = tmp_path / "out"
    folder.mkdir()

    with pytest.raises(FileNotFoundError):
        copy_into_folder(tmp_path / "gone.mp4", folder, "clip.mp4")
    with pytest.raises(OutputFolderError) as caught:
        copy_into_folder(tmp_path / "gone.mp4", tmp_path / "unplugged", "clip.mp4")

    assert list(folder.iterdir()) == []
    assert caught.value.reason == "not_creatable"


def _partials(folder: Path) -> list[str]:
    return sorted(p.name for p in folder.iterdir() if p.name.endswith(".part"))


def test_a_stopped_copy_removes_its_partial_file(tmp_path: Path) -> None:
    source = tmp_path / "hl_1.mp4"
    source.write_bytes(b"\0" * (3 * 1024 * 1024))
    folder = tmp_path / "out"
    folder.mkdir()
    stop = threading.Event()
    stop.set()  # the server is shutting down

    with pytest.raises(CopyCancelled):
        copy_into_folder(source, folder, "reel.mp4", stop=stop)

    assert list(folder.iterdir()) == []


def test_partial_copies_left_by_a_crash_are_removed_but_recent_ones_kept(tmp_path: Path) -> None:
    source = tmp_path / "exp_1.mp4"
    source.write_bytes(b"clip")
    folder = tmp_path / "out"
    folder.mkdir()
    stale = folder / ".autoclipper-abc123.part"
    stale.write_bytes(b"\0" * 1000)
    old = time.time() - STALE_PARTIAL_AGE_S - 60
    os.utime(stale, (old, old))
    in_progress = folder / ".autoclipper-def456.part"  # another copy that's still running
    in_progress.write_bytes(b"\0")
    unrelated = folder / ".autoclipper-notes.txt"
    unrelated.write_bytes(b"keep")
    os.utime(unrelated, (old, old))

    copied = copy_into_folder(source, folder, "clip.mp4")

    assert not stale.exists()
    assert in_progress.exists()
    assert unrelated.exists()
    assert copied.exists()
    assert remove_stale_partials(folder) == 0


def test_without_hard_links_the_copy_falls_back_to_a_checked_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # FAT and exFAT drives have no hard links.
    def no_links(src: object, dst: object) -> None:
        raise OSError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr(os, "link", no_links)
    source = tmp_path / "exp_1.mp4"
    source.write_bytes(b"rendered clip")
    folder = tmp_path / "out"
    folder.mkdir()
    (folder / "clip.mp4").write_bytes(b"someone else's file")

    copied = copy_into_folder(source, folder, "clip.mp4")

    assert copied.name == "clip_2.mp4"
    assert copied.read_bytes() == b"rendered clip"
    assert (folder / "clip.mp4").read_bytes() == b"someone else's file"
    assert _partials(folder) == []
