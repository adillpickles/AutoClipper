"""Tests that the pytest configuration in pyproject.toml keeps live tests out of default runs."""

from pathlib import Path

import pytest

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"

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
