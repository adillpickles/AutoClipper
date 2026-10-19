"""Smoke tests for the `autoclipper` command-line interface."""

import shutil
import subprocess
import sysconfig
from importlib.metadata import version

import pytest
from typer.testing import CliRunner

from autoclipper.cli import ExitCode, app

runner = CliRunner()


def test_help_shows_usage_of_a_command_group() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == ExitCode.SUCCESS
    # "COMMAND [ARGS]..." means the CLI is a group that subcommands can be added to.
    assert "Usage: autoclipper [OPTIONS] COMMAND [ARGS]..." in result.output


def test_version_prints_the_installed_package_version() -> None:
    result = runner.invoke(app, ["--version"])

    assert result.exit_code == ExitCode.SUCCESS
    assert result.output.strip() == f"autoclipper {version('autoclipper')}"


def test_no_arguments_shows_help_and_exits_with_user_error() -> None:
    result = runner.invoke(app, [])

    assert result.exit_code == ExitCode.USER_ERROR
    assert "Usage: autoclipper" in result.output


@pytest.mark.parametrize(
    ("args", "message"),
    [
        pytest.param(["--no-such-option"], "No such option", id="unknown-option"),
        pytest.param(["no-such-command"], "No such command", id="unknown-command"),
    ],
)
def test_usage_errors_exit_with_user_error(args: list[str], message: str) -> None:
    result = runner.invoke(app, args)

    # Typer's default for usage errors is 2, which conventions §7 reserves for external failures.
    assert result.exit_code == ExitCode.USER_ERROR
    assert message in result.output


def test_installed_console_script_shows_help() -> None:
    # Runs the script `uv sync` installed, which exercises the [project.scripts] entry point.
    script = shutil.which("autoclipper", path=sysconfig.get_path("scripts"))
    assert script is not None, "the autoclipper console script is not installed; run `uv sync`"

    completed = subprocess.run(
        [script, "--help"], capture_output=True, text=True, check=False, timeout=60
    )

    assert completed.returncode == ExitCode.SUCCESS, completed.stderr
    assert "Usage: autoclipper" in completed.stdout
