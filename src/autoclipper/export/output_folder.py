"""A user-chosen folder where finished exports and highlights videos are also saved.

`validate_output_folder` checks a folder before it's saved as a setting; `copy_into_folder`
copies one finished MP4 into it under its download name. Errors carry a stable ``reason`` and a
message without any path, so the API can show them as they are (API contract § 1); callers log
the path themselves.

A copy is written to an ``.autoclipper-*.part`` file and renamed into place when complete. A
copy stopped by `stop` removes its partial file; one cut off by a crash or power loss leaves it
behind, so `remove_stale_partials` deletes old ones (each copy runs it first).
"""

import contextlib
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Final, Literal

from autoclipper.export.filenames import collision_key, deduplicate_filename

log = logging.getLogger(__name__)

type FolderProblem = Literal["not_absolute", "not_a_directory", "not_creatable", "not_writable"]

_MESSAGES: Final[dict[FolderProblem, str]] = {
    "not_absolute": "Enter a full folder path, starting from a drive or the root folder.",
    "not_a_directory": "That path is a file, not a folder.",
    "not_creatable": "That folder doesn't exist and couldn't be created.",
    "not_writable": "AutoClipper can't write to that folder. Pick one you have access to.",
}
_PARTIAL_PREFIX: Final = ".autoclipper-"
_PARTIAL_SUFFIX: Final = ".part"
_COPY_BUFFER_BYTES: Final = 1024 * 1024
# A partial file this old can't belong to a copy in progress (even a multi-GB copy to a slow
# drive takes minutes), so it was left by a crash and is safe to delete.
STALE_PARTIAL_AGE_S: Final = 60 * 60


class CopyCancelled(Exception):
    """The copy was stopped (the server is shutting down); its partial file was removed."""


class OutputFolderError(Exception):
    """A folder can't be used; `message` is safe to show (it names no path)."""

    def __init__(self, reason: FolderProblem) -> None:
        self.reason: FolderProblem = reason
        self.message = _MESSAGES[reason]
        super().__init__(self.message)


def validate_output_folder(raw: str) -> Path:
    """Return `raw` as an absolute folder that exists (created if needed) and is writable."""
    path = Path(raw.strip()).expanduser()
    if not path.is_absolute():
        raise OutputFolderError("not_absolute")
    if path.exists() and not path.is_dir():
        raise OutputFolderError("not_a_directory")
    try:
        path.mkdir(parents=True, exist_ok=True)
    except (FileExistsError, NotADirectoryError) as exc:  # a file somewhere along the way
        raise OutputFolderError("not_a_directory") from exc
    except OSError as exc:
        raise OutputFolderError("not_creatable") from exc
    _check_writable(path)
    return path


def _check_writable(folder: Path) -> None:
    # Actually write: permission bits and ACLs (Windows) don't reliably say.
    try:
        with tempfile.NamedTemporaryFile(dir=folder, prefix=_PARTIAL_PREFIX, suffix=".tmp"):
            pass
    except OSError as exc:
        raise OutputFolderError("not_writable") from exc


def copy_into_folder(
    source: Path, folder: Path, filename: str, *, stop: threading.Event | None = None
) -> Path:
    """Copy `source` into `folder` as `filename`, adding ``_2``, ``_3``, ... on a name clash.

    The copy goes to a partial file first and is renamed into place, so the folder never shows a
    half-written MP4 under its final name and an existing file is never replaced. `stop` is
    checked between chunks; once set, the partial file is removed and `CopyCancelled` raised.
    Raises `OutputFolderError` if the folder is gone or unusable, `FileNotFoundError` if
    `source` is gone, `OSError` for other failures.
    """
    if not folder.is_dir():
        raise OutputFolderError("not_creatable")
    remove_stale_partials(folder)
    with source.open("rb") as src:
        try:
            partial = tempfile.NamedTemporaryFile(  # noqa: SIM115 - closed before the rename
                dir=folder, prefix=_PARTIAL_PREFIX, suffix=_PARTIAL_SUFFIX, delete=False
            )
        except OSError as exc:
            raise OutputFolderError("not_writable") from exc
        partial_path = Path(partial.name)
        try:
            with partial:
                while chunk := src.read(_COPY_BUFFER_BYTES):
                    if stop is not None and stop.is_set():
                        raise CopyCancelled
                    partial.write(chunk)
            return _rename_unique(partial_path, folder, filename)
        except BaseException:
            with contextlib.suppress(OSError):
                partial_path.unlink(missing_ok=True)
            raise


def remove_stale_partials(folder: Path, *, older_than_s: float = STALE_PARTIAL_AGE_S) -> int:
    """Delete partial copies left in `folder` by an interrupted run; return how many."""
    cutoff = time.time() - older_than_s
    removed = 0
    try:
        entries = list(os.scandir(folder))
    except OSError:
        return 0
    for entry in entries:
        name = entry.name
        if not (name.startswith(_PARTIAL_PREFIX) and name.endswith(_PARTIAL_SUFFIX)):
            continue
        try:
            if entry.is_file(follow_symlinks=False) and entry.stat().st_mtime < cutoff:
                Path(entry.path).unlink()
                removed += 1
        except OSError as exc:
            log.warning("couldn't remove stale partial copy %s: %s", entry.path, exc)
    if removed:
        log.info("removed %d stale partial copies from %s", removed, folder)
    return removed


def _rename_unique(partial: Path, folder: Path, filename: str) -> Path:
    used = {collision_key(entry.name) for entry in os.scandir(folder)}
    while True:
        target = folder / deduplicate_filename(filename, used)
        try:
            # A hard link fails if the name is taken, on every platform, unlike a rename on
            # POSIX; fall back to a checked rename where links aren't supported (FAT, exFAT).
            os.link(partial, target)
        except FileExistsError:
            continue  # taken since the listing; `used` now holds it, so try the next suffix
        except OSError:
            if target.exists():
                continue
            partial.rename(target)
            return target
        partial.unlink()
        return target
