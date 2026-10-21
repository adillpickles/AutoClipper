"""Tests for autoclipper.logging. conftest.py undoes `configure_logging` after each test."""

import ast
import io
import logging
import re
from pathlib import Path

import pytest

from autoclipper.config import ConfigError, get_settings
from autoclipper.logging import KeyValueFormatter, configure_logging, reset_logging

PACKAGE_DIR = Path(__file__).resolve().parents[1] / "src" / "autoclipper"

LINE = re.compile(
    r"ts=\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z level=(?P<level>[A-Z]+) "
    r"logger=(?P<logger>\S+) (?P<message>.*)"
)

log = logging.getLogger("autoclipper.tests.logging")


def our_handlers() -> list[logging.Handler]:
    return [h for h in logging.getLogger().handlers if isinstance(h.formatter, KeyValueFormatter)]


def emitted_lines(stderr: str) -> list[re.Match[str]]:
    lines = stderr.splitlines()
    matches = [LINE.fullmatch(line) for line in lines]
    assert all(matches), f"lines not in key=value format: {lines}"
    return [match for match in matches if match]


def test_record_is_one_key_value_line_on_stderr(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO")

    log.info("fetched clips video_id=%s n=%d", "v123", 5)

    captured = capsys.readouterr()
    assert captured.out == ""
    [line] = emitted_lines(captured.err)
    assert line["level"] == "INFO"
    assert line["logger"] == "autoclipper.tests.logging"
    assert line["message"] == "fetched clips video_id=v123 n=5"


def test_timestamp_is_utc_iso_8601_with_milliseconds() -> None:
    record = logging.LogRecord("autoclipper.x", logging.WARNING, __file__, 1, "msg", None, None)
    record.created = 1_760_000_000.5  # 2025-10-09T08:53:20.5 UTC

    formatted = KeyValueFormatter().format(record)

    assert formatted == "ts=2025-10-09T08:53:20.500Z level=WARNING logger=autoclipper.x msg"


def test_tracebacks_follow_the_line(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO")

    try:
        raise RuntimeError("boom")
    except RuntimeError:
        log.exception("stage failed stage=test")

    first, *rest = capsys.readouterr().err.splitlines()
    assert LINE.fullmatch(first)
    assert rest[0] == "Traceback (most recent call last):"
    assert rest[-1] == "RuntimeError: boom"


def test_level_comes_from_autoclipper_log_level(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("AUTOCLIPPER_LOG_LEVEL", "debug")

    configure_logging()
    log.debug("debug detail")

    [line] = emitted_lines(capsys.readouterr().err)
    assert line["level"] == "DEBUG"


def test_default_level_is_info(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging()

    log.debug("hidden")
    log.info("shown")

    assert [line["message"] for line in emitted_lines(capsys.readouterr().err)] == ["shown"]


def test_explicit_level_overrides_the_setting(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("AUTOCLIPPER_LOG_LEVEL", "DEBUG")

    configure_logging("ERROR")
    log.warning("hidden")
    log.error("shown")

    assert [line["message"] for line in emitted_lines(capsys.readouterr().err)] == ["shown"]


def test_invalid_log_level_setting_raises_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTOCLIPPER_LOG_LEVEL", "LOUD")

    with pytest.raises(ConfigError, match="AUTOCLIPPER_LOG_LEVEL"):
        configure_logging()

    assert our_handlers() == []


def test_configuring_twice_installs_one_handler_and_updates_the_level(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging("WARNING")
    configure_logging("DEBUG")

    log.debug("once")

    assert len(our_handlers()) == 1
    assert [line["message"] for line in emitted_lines(capsys.readouterr().err)] == ["once"]


def test_other_libraries_stay_at_warning(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("DEBUG")
    other = logging.getLogger("some_dependency")

    other.info("chatter")
    other.warning("disk almost full")

    [line] = emitted_lines(capsys.readouterr().err)
    assert (line["logger"], line["message"]) == ("some_dependency", "disk almost full")


def test_pytest_log_capture_still_works(caplog: pytest.LogCaptureFixture) -> None:
    configure_logging("INFO")

    log.info("captured n=%d", 1)

    assert caplog.record_tuples == [("autoclipper.tests.logging", logging.INFO, "captured n=1")]


def test_handler_writes_to_the_current_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    configure_logging("INFO")
    # Replace stderr after configuring, as pytest's capture and Typer's test runner do.
    replacement = io.StringIO()
    monkeypatch.setattr("sys.stderr", replacement)

    log.info("after swap")

    [line] = emitted_lines(replacement.getvalue())
    assert line["message"] == "after swap"


def test_reset_logging_removes_the_handler_and_level() -> None:
    configure_logging("DEBUG")

    reset_logging()

    assert our_handlers() == []
    assert logging.getLogger("autoclipper").level == logging.NOTSET


def test_secrets_never_reach_the_log_output(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    secrets = ("fake-client-id-4f1d", "fake-client-secret-9b2e", "fake-salt-c7a0")
    monkeypatch.setenv("TWITCH_CLIENT_ID", secrets[0])
    monkeypatch.setenv("TWITCH_CLIENT_SECRET", secrets[1])
    monkeypatch.setenv("AUTOCLIPPER_CHAT_HASH_SALT", secrets[2])
    configure_logging("DEBUG")
    settings = get_settings()

    log.debug("loaded settings=%s", settings)
    log.debug("credentials=%r", settings.require_twitch_credentials())
    log.debug("salt=%s", settings.require_chat_hash_salt())

    err = capsys.readouterr().err
    assert len(emitted_lines(err)) == 3
    assert "**********" in err
    for secret in secrets:
        assert secret not in err


# --- Conventions §6 ---------------------------------------------------------------------------

_LOGGING_SETUP_CALLS = {"basicConfig", "dictConfig", "fileConfig", "addHandler"}


def test_only_the_logging_module_configures_logging() -> None:
    offenders: dict[str, list[str]] = {}
    for path in sorted(PACKAGE_DIR.rglob("*.py")):
        if path == PACKAGE_DIR / "logging.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        calls = [
            f"line {node.lineno}: {node.func.attr}()"
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in _LOGGING_SETUP_CALLS
        ]
        if calls:
            offenders[str(path.relative_to(PACKAGE_DIR))] = calls

    # docs/development/conventions.md §6: library modules only call logging.getLogger(__name__).
    assert offenders == {}
