"""Tests for validating the output folder and copying finished renders into it."""

import tempfile
from collections.abc import Callable
from pathlib import Path

import pytest

from autoclipper.export.output_folder import (
    OutputFolderError,
    copy_into_folder,
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
