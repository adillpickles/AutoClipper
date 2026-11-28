"""Tests for the pytest configuration in pyproject.toml and tests/conftest.py.

Live tests stay out of default runs, and FFmpeg tests skip locally but fail in CI when FFmpeg is
missing.

Each test runs a throwaway pytest session in a subprocess (`_run_pytest`), never in this process.
An in-process session shares the garbage collector, warning filters and unraisable-exception hook
with the outer suite, so garbage an earlier test left behind (an unclosed file or socket in a
reference cycle) could be collected during the inner run and fail it under
``filterwarnings = ["error"]``. That made these tests fail only in full-suite runs (#225).
"""

import gc
import os
import warnings
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"
CONFTEST = Path(__file__).resolve().parent / "conftest.py"

# A child session takes about a second; the margin covers a loaded machine or CI runner.
SUBPROCESS_TIMEOUT_S = 180.0

# Variables that would change how the child pytest runs. Tests that need CI set it themselves.
_CHILD_ENV_OVERRIDES = ("PYTEST_ADDOPTS", "PYTEST_PLUGINS", "PYTEST_DISABLE_PLUGIN_AUTOLOAD", "CI")

JUNIT_XML = "inner-report.xml"


@pytest.fixture(autouse=True)
def _clean_child_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the outer run's pytest options and CI flag out of the child sessions."""
    for name in _CHILD_ENV_OVERRIDES:
        monkeypatch.delenv(name, raising=False)


def _run_pytest(pytester: pytest.Pytester, *args: str) -> pytest.RunResult:
    """Run pytest on `pytester`'s project in a fresh interpreter.

    `runpytest_subprocess` gives the child its own ``--basetemp`` inside the project. The cache
    provider is off so children never share or write a ``.pytest_cache``, and a JUnit report
    records each test's outcome for `_passed_test_names`.
    """
    return pytester.runpytest_subprocess(
        "-p",
        "no:cacheprovider",
        f"--junitxml={pytester.path / JUNIT_XML}",
        *args,
        timeout=SUBPROCESS_TIMEOUT_S,
    )


def _passed_test_names(pytester: pytest.Pytester) -> list[str]:
    """Names of the tests that passed in the last `_run_pytest`, in run order."""
    root = ET.parse(pytester.path / JUNIT_XML).getroot()
    return [
        case.attrib["name"]
        for case in root.iter("testcase")
        if not any(child.tag in {"failure", "error", "skipped"} for child in case)
    ]


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


class _UnclosedFileInACycle:
    """Garbage like an earlier test can leave behind: only the cycle collector frees it."""

    def __init__(self, path: Path) -> None:
        self.cycle = self
        self.stream = path.open("w", encoding="utf-8")


def test_child_session_is_unaffected_by_this_runs_garbage(
    project: pytest.Pytester, tmp_path: Path
) -> None:
    # Regression for #225: with an in-process session (inline_run), the child's gc.collect()
    # finalized this garbage, and its ResourceWarning failed the child test.
    (project.path / "tests" / "test_gc.py").write_text(
        "import gc\n\n\ndef test_collects():\n    gc.collect()\n", encoding="utf-8"
    )
    gc.disable()
    try:
        _UnclosedFileInACycle(tmp_path / "left-open.txt")  # garbage as soon as it's made
        result = _run_pytest(project, "tests/test_gc.py")
    finally:
        # Free the garbage here, if the child didn't, without failing this test on its warning.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", ResourceWarning)
            gc.collect()
        gc.enable()

    result.assert_outcomes(passed=1)


def test_default_run_deselects_live_tests(project: pytest.Pytester) -> None:
    result = _run_pytest(project)

    result.assert_outcomes(passed=1, deselected=1)
    assert _passed_test_names(project) == ["test_offline"]


def test_marker_expression_live_selects_only_live_tests(project: pytest.Pytester) -> None:
    result = _run_pytest(project, "-m", "live")

    result.assert_outcomes(passed=1, deselected=1)
    assert _passed_test_names(project) == ["test_live"]


@pytest.mark.parametrize("expression", ["not slow", "slow or not slow", "", "  "])
def test_marker_expression_without_live_still_deselects_live_tests(
    project: pytest.Pytester, expression: str
) -> None:
    result = _run_pytest(project, "-m", expression)

    result.assert_outcomes(passed=1, deselected=1)
    assert _passed_test_names(project) == ["test_offline"]


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
    result = _run_pytest(project, "-m", expression)

    result.assert_outcomes(passed=len(expected), deselected=2 - len(expected))
    assert _passed_test_names(project) == expected


LIVE_AND_OFFLINE_SETTINGS_TESTS = """
import gc
import os
import warnings

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

    result = _run_pytest(project, "-m", "live or not live", "tests/test_settings.py")

    result.assert_outcomes(passed=2)


def test_unregistered_markers_are_errors(project: pytest.Pytester) -> None:
    project.makepyfile(
        test_typo="""
        import pytest


        @pytest.mark.lvie
        def test_typo():
            pass
        """
    )

    result = _run_pytest(project, "test_typo.py")

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
    """Make PATH contain only `directory`, holding stand-ins for `programs` (never run).

    The managed tools folder is pointed at an empty directory too: the child session looks for
    FFmpeg there before PATH, and its conftest's isolation fixture only applies once a test's
    fixtures are set up, after the FFmpeg check has run.
    """
    directory.mkdir()
    managed_tools = directory.parent / "managed-tools"
    managed_tools.mkdir()
    monkeypatch.setenv("AUTOCLIPPER_TOOLS_DIR", os.fspath(managed_tools))
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

    result = _run_pytest(ffmpeg_project)

    result.assert_outcomes(passed=1, skipped=2)
    reason = "ffmpeg and ffprobe not found on PATH; install FFmpeg (docs/development/setup.md)"
    result.stdout.fnmatch_lines([f"*SKIPPED*{reason}*"])


def test_ffmpeg_tests_skip_locally_when_only_ffprobe_is_missing(
    ffmpeg_project: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("CI", raising=False)
    _put_on_path(monkeypatch, tmp_path / "bin", ["ffmpeg"])

    result = _run_pytest(ffmpeg_project)

    result.assert_outcomes(passed=1, skipped=2)
    result.stdout.fnmatch_lines(["*SKIPPED*ffprobe not found on PATH*"])


@pytest.mark.parametrize("ci", ["true", "TRUE", "1"])
def test_ffmpeg_tests_error_in_ci_when_ffmpeg_is_missing(
    ffmpeg_project: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, ci: str
) -> None:
    monkeypatch.setenv("CI", ci)
    _put_on_path(monkeypatch, tmp_path / "bin", [])

    result = _run_pytest(ffmpeg_project)

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

    result = _run_pytest(ffmpeg_project)

    result.assert_outcomes(passed=3)


def test_ffmpeg_tests_can_be_deselected_by_marker(
    ffmpeg_project: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Deselected tests are not checked, so this passes even in CI without FFmpeg.
    monkeypatch.setenv("CI", "true")
    _put_on_path(monkeypatch, tmp_path / "bin", [])

    result = _run_pytest(ffmpeg_project, "-m", "not live and not ffmpeg")

    result.assert_outcomes(passed=1, deselected=2)
