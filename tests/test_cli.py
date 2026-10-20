"""Smoke tests for the `autoclipper` command-line interface."""

import os
import re
import shutil
import subprocess
import sysconfig
from importlib.metadata import version

import pytest
import typer
from typer._click.exceptions import ClickException, UsageError
from typer.testing import CliRunner

from autoclipper.cli import ExitCode, _RootGroup, app

runner = CliRunner()

# Typer renders help and errors with Rich, which emits ANSI styling whenever it decides the output
# is a terminal. On GitHub Actions it always does (Typer forces it when GITHUB_ACTIONS, FORCE_COLOR
# or PY_COLORS is set), so assertions on the text must not depend on whether styling is present.
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _plain(text: str) -> str:
    """Return `text` without ANSI escape sequences."""
    return _ANSI_ESCAPE.sub("", text)


def _script() -> str:
    # The script `uv sync` installed, which exercises the [project.scripts] entry point.
    script = shutil.which("autoclipper", path=sysconfig.get_path("scripts"))
    assert script is not None, "the autoclipper console script is not installed; run `uv sync`"
    return script


def test_help_shows_usage_of_a_command_group() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == ExitCode.SUCCESS
    # "COMMAND [ARGS]..." means the CLI is a group that subcommands can be added to.
    assert "Usage: autoclipper [OPTIONS] COMMAND [ARGS]..." in _plain(result.output)


def test_version_prints_the_installed_package_version() -> None:
    result = runner.invoke(app, ["--version"])

    assert result.exit_code == ExitCode.SUCCESS
    assert result.output.strip() == f"autoclipper {version('autoclipper')}"


def test_no_arguments_shows_help_and_exits_with_user_error() -> None:
    result = runner.invoke(app, [])

    assert result.exit_code == ExitCode.USER_ERROR
    assert "Usage: autoclipper" in _plain(result.output)


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
    assert message in _plain(result.output)


def test_installed_console_script_shows_help(monkeypatch: pytest.MonkeyPatch) -> None:
    script = _script()
    # Pin the child's stdout encoding so decoding doesn't depend on the shell's settings (on
    # Windows the locale default is a legacy code page, but PYTHONIOENCODING may be set).
    monkeypatch.setenv("PYTHONIOENCODING", "utf-8")

    completed = subprocess.run(
        [script, "--help"], capture_output=True, encoding="utf-8", check=False, timeout=60
    )

    assert completed.returncode == ExitCode.SUCCESS, completed.stderr
    assert "Usage: autoclipper" in _plain(completed.stdout)


def test_help_is_readable_when_styling_is_forced_as_on_github_actions() -> None:
    # Regression test: Typer decides on styling when it's imported, so this needs a fresh process
    # with the variables GitHub Actions sets; patching the environment in-process has no effect.
    # (On Linux Rich then emits ANSI codes; on Windows it may style through the console API.)
    env = {k: v for k, v in os.environ.items() if k not in {"NO_COLOR", "TERM"}}
    env |= {"GITHUB_ACTIONS": "true", "FORCE_COLOR": "1", "PYTHONIOENCODING": "utf-8"}

    completed = subprocess.run(
        [_script(), "--help"],
        capture_output=True,
        encoding="utf-8",
        env=env,
        check=False,
        timeout=60,
    )

    assert completed.returncode == ExitCode.SUCCESS, completed.stderr
    output = _plain(completed.stdout)
    assert "Usage: autoclipper" in output
    assert "[OPTIONS] COMMAND [ARGS]..." in output


class _ExternalToolError(ClickException):
    """Stands in for a library error that a command reports as an external failure."""

    exit_code = ExitCode.EXTERNAL_FAILURE


def _app_with_commands() -> typer.Typer:
    test_app = typer.Typer(cls=_RootGroup)

    @test_app.callback()
    def root() -> None:
        """Root."""

    @test_app.command()
    def count(n: int) -> None:
        """Takes an integer."""

    @test_app.command()
    def fetch() -> None:
        """Fails like an external tool would."""
        raise _ExternalToolError("the tool failed")

    return test_app


@pytest.mark.parametrize(
    "args",
    [
        pytest.param(["count"], id="missing-argument"),
        pytest.param(["count", "abc"], id="invalid-value"),
        pytest.param(["count", "1", "2"], id="extra-argument"),
        pytest.param(["count", "--bad", "1"], id="unknown-subcommand-option"),
    ],
)
def test_subcommand_usage_errors_exit_with_user_error(args: list[str]) -> None:
    result = runner.invoke(_app_with_commands(), args)

    assert result.exit_code == ExitCode.USER_ERROR, _plain(result.output)


def test_errors_raised_by_a_command_keep_their_exit_code() -> None:
    # Regression test: only usage errors are remapped; an external failure must stay 2.
    result = runner.invoke(_app_with_commands(), ["fetch"])

    assert result.exit_code == ExitCode.EXTERNAL_FAILURE, _plain(result.output)
    assert "the tool failed" in _plain(result.output)


def test_usage_error_is_still_where_the_cli_imports_it_from() -> None:
    # cli.py imports UsageError from Typer's private vendored Click; fail loudly if it moves.
    assert issubclass(UsageError, typer.TyperException)
