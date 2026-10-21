"""Tests that the CLI's root callback sets up logging and reports invalid settings.

The root callback only runs before a subcommand, so these tests register a throwaway ``probe``
subcommand on the real app for the duration of a test.
"""

import logging
import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from autoclipper.cli import ExitCode, app

runner = CliRunner()

LINE = re.compile(
    r"ts=\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z level=(?P<level>[A-Z]+) "
    r"logger=autoclipper\.probe (?P<message>.*)"
)


@pytest.fixture
def probe() -> Iterator[None]:
    def probe_command() -> None:
        log = logging.getLogger("autoclipper.probe")
        log.info("probe ran n=%d", 1)
        log.debug("probe detail")

    app.command("probe")(probe_command)
    command_info = app.registered_commands[-1]
    yield
    app.registered_commands.remove(command_info)


def log_messages(stderr: str) -> list[tuple[str, str]]:
    matches = [LINE.fullmatch(line) for line in stderr.splitlines()]
    assert all(matches), f"unexpected stderr: {stderr!r}"
    return [(match["level"], match["message"]) for match in matches if match]


@pytest.mark.usefixtures("probe")
def test_subcommand_logs_key_value_lines_at_the_default_level() -> None:
    result = runner.invoke(app, ["probe"])

    assert result.exit_code == ExitCode.SUCCESS, result.output
    assert result.stdout == ""
    assert log_messages(result.stderr) == [("INFO", "probe ran n=1")]


@pytest.mark.usefixtures("probe")
def test_subcommand_logs_at_autoclipper_log_level(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTOCLIPPER_LOG_LEVEL", "DEBUG")

    result = runner.invoke(app, ["probe"])

    assert result.exit_code == ExitCode.SUCCESS, result.output
    assert log_messages(result.stderr) == [("INFO", "probe ran n=1"), ("DEBUG", "probe detail")]


@pytest.mark.usefixtures("probe")
def test_invalid_settings_exit_with_user_error_before_the_command_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTOCLIPPER_LOG_LEVEL", "LOUD")

    result = runner.invoke(app, ["probe"])

    assert result.exit_code == ExitCode.USER_ERROR
    assert result.stderr.startswith("Error: invalid settings")
    assert "AUTOCLIPPER_LOG_LEVEL" in result.stderr
    assert "Traceback" not in result.output
    assert "probe ran" not in result.output


@pytest.mark.usefixtures("probe")
def test_settings_error_does_not_print_values_from_env_file(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("TWITCH_SECRET=fake-secret-value-77aa\n", encoding="utf-8")

    result = runner.invoke(app, ["probe"])

    assert result.exit_code == ExitCode.USER_ERROR
    assert "TWITCH_SECRET: not an AutoClipper setting" in result.stderr
    assert "fake-secret-value-77aa" not in result.output


@pytest.mark.usefixtures("probe")
def test_unparseable_env_file_stops_the_command(tmp_path: Path) -> None:
    # Regression test: the bad line used to be dropped with a warning and the command ran (exit 0)
    # with the default data directory.
    (tmp_path / ".env").write_text('AUTOCLIPPER_DATA_DIR="D:/expected-data\n', encoding="utf-8")

    result = runner.invoke(app, ["probe"])

    assert result.exit_code == ExitCode.USER_ERROR
    assert "can't be parsed (line 1)" in result.stderr
    assert "expected-data" not in result.output
    assert "probe ran" not in result.output


@pytest.mark.parametrize("args", [["--help"], ["--version"]])
def test_help_and_version_work_with_invalid_settings(
    monkeypatch: pytest.MonkeyPatch, args: list[str]
) -> None:
    monkeypatch.setenv("AUTOCLIPPER_LOG_LEVEL", "LOUD")

    result = runner.invoke(app, args)

    assert result.exit_code == ExitCode.SUCCESS
    assert "AUTOCLIPPER_LOG_LEVEL" not in result.output
