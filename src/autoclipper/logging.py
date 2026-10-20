"""Logging setup: one key=value line per record, with a UTC timestamp, on stderr.

Library modules never configure logging. They create a module logger and log key=value messages
(docs/development/conventions.md §6)::

    log = logging.getLogger(__name__)
    log.info("fetched clips video_id=%s n=%d", video_id, n_clips)

Entry points call `configure_logging` once at startup (the CLI's root callback does it for every
command). A record then comes out as::

    ts=2026-10-19T16:29:01.123Z level=INFO logger=autoclipper.ingest fetched clips video_id=1 n=5

``AUTOCLIPPER_LOG_LEVEL`` sets the level of AutoClipper's own loggers (the ``autoclipper``
logger and its children). Other libraries keep Python's default threshold of WARNING, so their
debug and info chatter stays out of the output.
"""

import logging
import sys
from datetime import UTC, datetime
from typing import TextIO, override

from autoclipper.config import LogLevel, get_settings

_PACKAGE_LOGGER = "autoclipper"


class KeyValueFormatter(logging.Formatter):
    """Formats a record as ``ts=<UTC ISO-8601> level=<LEVEL> logger=<name> <message>``.

    The timestamp has millisecond precision and a ``Z`` suffix. The message follows unchanged,
    because messages already carry their own key=value pairs. Tracebacks, if any, follow on the
    next lines as usual.
    """

    def __init__(self) -> None:
        super().__init__(fmt="ts=%(asctime)s level=%(levelname)s logger=%(name)s %(message)s")

    @override
    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        created_at = datetime.fromtimestamp(record.created, tz=UTC)
        return created_at.isoformat(timespec="milliseconds").replace("+00:00", "Z")


class _StderrHandler(logging.StreamHandler[TextIO]):
    """Writes to whatever `sys.stderr` is when a record is emitted.

    A handler that kept the stream it was created with would keep writing to a replaced stream,
    e.g. one that pytest's output capture or Typer's test runner has already closed.
    """

    @override
    def emit(self, record: logging.LogRecord) -> None:
        # `Handler.handle` holds the handler's lock while calling `emit`.
        self.stream = sys.stderr
        super().emit(record)


def configure_logging(level: LogLevel | None = None) -> None:
    """Send log records to stderr as key=value lines and set AutoClipper's log level.

    Safe to call more than once: the root logger only ever gets one AutoClipper handler, and a
    later call just changes the level. Handlers that others installed (such as pytest's log
    capture) are left alone.

    Args:
        level: Minimum level for the ``autoclipper`` loggers. None reads
            ``AUTOCLIPPER_LOG_LEVEL`` through `get_settings`.

    Raises:
        ConfigError: ``level`` is None and the settings are invalid.
    """
    if level is None:
        level = get_settings().log_level
    root = logging.getLogger()
    if not any(isinstance(handler, _StderrHandler) for handler in root.handlers):
        handler = _StderrHandler()
        handler.setFormatter(KeyValueFormatter())
        root.addHandler(handler)
    logging.getLogger(_PACKAGE_LOGGER).setLevel(level)


def reset_logging() -> None:
    """Undo `configure_logging`: remove its handler and unset AutoClipper's log level.

    Tests call this after each test (tests/conftest.py) so logging setup doesn't leak between
    them.
    """
    root = logging.getLogger()
    for handler in [h for h in root.handlers if isinstance(h, _StderrHandler)]:
        root.removeHandler(handler)
        handler.close()
    logging.getLogger(_PACKAGE_LOGGER).setLevel(logging.NOTSET)
