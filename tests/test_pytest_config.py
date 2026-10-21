"""Tests for the pytest configuration in pyproject.toml and tests/conftest.py.

Live tests stay out of default runs, and FFmpeg tests skip locally but fail in CI when FFmpeg is
missing.
"""

import os
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


FFMPEG_TESTS = """
import pytest


def test_offline():
    pass


@pytest.mark.ffmpeg
def test_marked():
    pass


def test_uses_media_fixture(make_test_media):
    pass
"""


@pytest.fixture
def ffmpeg_project(pytester: pytest.Pytester) -> pytest.Pytester:
    """A throwaway project with this repository's pytest config and conftest, and FFmpeg tests."""
    pytester.makepyprojecttoml(PYPROJECT.read_text(encoding="utf-8"))
    pytester.makeconftest(CONFTEST.read_text(encoding="utf-8"))
    tests_dir = pytester.mkdir("tests")
    (tests_dir / "test_ffmpeg_marker.py").write_text(FFMPEG_TESTS, encoding="utf-8")
    return pytester


def _put_on_path(monkeypatch: pytest.MonkeyPatch, directory: Path, programs: list[str]) -> None:
    """Make PATH contain only `directory`, holding stand-ins for `programs` (never run)."""
    directory.mkdir()
    for program in programs:
        if os.name == "nt":
            (directory / f"{program}.bat").write_text("@exit /b 0\n", encoding="utf-8")
        else:
            script = directory / program
            script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            script.chmod(0o755)
    monkeypatch.setenv("PATH", os.fspath(directory))


def test_ffmpeg_tests_skip_locally_when_ffmpeg_is_missing(
    ffmpeg_project: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("CI", raising=False)
    _put_on_path(monkeypatch, tmp_path / "bin", [])

    result = ffmpeg_project.runpytest()

    result.assert_outcomes(passed=1, skipped=2)
    reason = "ffmpeg and ffprobe not found on PATH; install FFmpeg (docs/development/setup.md)"
    result.stdout.fnmatch_lines([f"*SKIPPED*{reason}*"])


def test_ffmpeg_tests_skip_locally_when_only_ffprobe_is_missing(
    ffmpeg_project: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("CI", raising=False)
    _put_on_path(monkeypatch, tmp_path / "bin", ["ffmpeg"])

    result = ffmpeg_project.runpytest()

    result.assert_outcomes(passed=1, skipped=2)
    result.stdout.fnmatch_lines(["*SKIPPED*ffprobe not found on PATH*"])


@pytest.mark.parametrize("ci", ["true", "TRUE", "1"])
def test_ffmpeg_tests_error_in_ci_when_ffmpeg_is_missing(
    ffmpeg_project: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, ci: str
) -> None:
    monkeypatch.setenv("CI", ci)
    _put_on_path(monkeypatch, tmp_path / "bin", [])

    result = ffmpeg_project.runpytest()

    result.assert_outcomes(passed=1, errors=2)
    result.stdout.fnmatch_lines(["*In CI (CI=true), FFmpeg tests must run, not skip.*"])


@pytest.mark.parametrize("ci", [None, "true"])
def test_ffmpeg_tests_run_when_ffmpeg_is_on_path(
    ffmpeg_project: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, ci: str | None
) -> None:
    # The stand-in binaries are never started: the inner tests request make_test_media but
    # don't call it.
    if ci is None:
        monkeypatch.delenv("CI", raising=False)
    else:
        monkeypatch.setenv("CI", ci)
    _put_on_path(monkeypatch, tmp_path / "bin", ["ffmpeg", "ffprobe"])

    result = ffmpeg_project.runpytest()

    result.assert_outcomes(passed=3)


def test_ffmpeg_tests_can_be_deselected_by_marker(
    ffmpeg_project: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Deselected tests are not checked, so this passes even in CI without FFmpeg.
    monkeypatch.setenv("CI", "true")
    _put_on_path(monkeypatch, tmp_path / "bin", [])

    result = ffmpeg_project.runpytest("-m", "not live and not ffmpeg")

    result.assert_outcomes(passed=1, deselected=2)
