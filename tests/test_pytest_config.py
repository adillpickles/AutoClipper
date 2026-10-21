"""Tests that the pytest configuration keeps live tests out of runs that don't ask for them."""

from pathlib import Path

import pytest

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"
CONFTEST = Path(__file__).resolve().parent / "conftest.py"

OFFLINE_AND_LIVE_TESTS = """
import pytest


def test_offline():
    pass


@pytest.mark.live
def test_live():
    pass
"""


@pytest.fixture
def project(pytester: pytest.Pytester) -> pytest.Pytester:
    """A throwaway project with this repository's pytest config and one offline + one live test."""
    pytester.makepyprojecttoml(PYPROJECT.read_text(encoding="utf-8"))
    tests_dir = pytester.mkdir("tests")
    (tests_dir / "conftest.py").write_text(CONFTEST.read_text(encoding="utf-8"), encoding="utf-8")
    (tests_dir / "test_markers.py").write_text(OFFLINE_AND_LIVE_TESTS, encoding="utf-8")
    return pytester


def _passed_test_names(recorder: pytest.HookRecorder) -> list[str]:
    passed, _skipped, _failed = recorder.listoutcomes()
    return [report.nodeid.rpartition("::")[2] for report in passed]


def test_default_run_deselects_live_tests(project: pytest.Pytester) -> None:
    recorder = project.inline_run()

    recorder.assertoutcome(passed=1)
    assert _passed_test_names(recorder) == ["test_offline"]


def test_marker_expression_live_selects_only_live_tests(project: pytest.Pytester) -> None:
    recorder = project.inline_run("-m", "live")

    recorder.assertoutcome(passed=1)
    assert _passed_test_names(recorder) == ["test_live"]


@pytest.mark.parametrize("expression", ["not slow", "slow or not slow", "", "  "])
def test_marker_expression_without_live_still_deselects_live_tests(
    project: pytest.Pytester, expression: str
) -> None:
    recorder = project.inline_run("-m", expression)

    recorder.assertoutcome(passed=1)
    assert _passed_test_names(recorder) == ["test_offline"]


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("live or not live", ["test_offline", "test_live"]),
        ("not live", ["test_offline"]),
        ("(live)", ["test_live"]),
    ],
)
def test_marker_expression_naming_live_is_used_as_given(
    project: pytest.Pytester, expression: str, expected: list[str]
) -> None:
    recorder = project.inline_run("-m", expression)

    assert _passed_test_names(recorder) == expected


LIVE_AND_OFFLINE_SETTINGS_TESTS = """
import os

import pytest

from autoclipper.config import get_settings


@pytest.mark.live
def test_live_test_keeps_local_settings():
    assert os.environ["TWITCH_CLIENT_ID"] == "fake-client-id"
    assert get_settings().require_chat_hash_salt().get_secret_value() == "fake-salt"


def test_offline_test_is_isolated():
    assert "TWITCH_CLIENT_ID" not in os.environ
    assert get_settings().chat_hash_salt is None
"""


def test_live_tests_keep_the_developers_env_and_dotenv(
    project: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TWITCH_CLIENT_ID", "fake-client-id")
    (project.path / ".env").write_text("AUTOCLIPPER_CHAT_HASH_SALT=fake-salt\n", encoding="utf-8")
    (project.path / "tests" / "test_settings.py").write_text(
        LIVE_AND_OFFLINE_SETTINGS_TESTS, encoding="utf-8"
    )

    recorder = project.inline_run("-m", "live or not live", "tests/test_settings.py")

    recorder.assertoutcome(passed=2)


def test_unregistered_markers_are_errors(project: pytest.Pytester) -> None:
    project.makepyfile(
        test_typo="""
        import pytest


        @pytest.mark.lvie
        def test_typo():
            pass
        """
    )

    result = project.runpytest("test_typo.py")

    assert result.ret == pytest.ExitCode.INTERRUPTED
    result.stdout.fnmatch_lines(["*'lvie' not found in `markers` configuration option*"])
