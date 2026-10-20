"""Shared pytest hooks and fixtures.

Every test runs isolated from the developer's settings, data and logging setup
(`_isolate_from_local_setup`), and `live` tests run only when `-m` names them
(`pytest_configure`).

FFmpeg-dependent tests (docs/development/testing.md § FFmpeg tests): a test that runs the real
ffmpeg/ffprobe binaries carries the `ffmpeg` marker, either explicitly or because it uses the
`make_test_media` fixture. When a binary is missing from PATH such a test is skipped locally, but
it errors in CI, so a CI runner without FFmpeg can't pass by skipping them.
"""

import os
import re
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from autoclipper.config import Settings, get_settings
from autoclipper.ffmpeg import (
    FFMPEG,
    FFPROBE,
    SETUP_DOC,
    FFmpegNotFoundError,
    ffmpeg_command,
    find_program,
    run_command,
)
from autoclipper.logging import reset_logging

# `pytester` runs throwaway pytest sessions; tests/test_pytest_config.py uses it.
pytest_plugins = ["pytester"]

_MENTIONS_LIVE = re.compile(r"\blive\b")


def pytest_configure(config: pytest.Config) -> None:
    """Keep `live` tests out of every run whose `-m` expression doesn't mention them.

    A plain `-m "not live"` in `addopts` would be replaced by any `-m` given on the command
    line, so `uv run pytest -m "not slow"` would start calling real services. Instead, an
    expression that doesn't name `live` is narrowed to `(<expression>) and not live`; one that
    does (`-m live`, `-m "live or slow"`, `-m "not live"`) is used as given.
    """
    expression = config.option.markexpr.strip()
    if _MENTIONS_LIVE.search(expression):
        return
    config.option.markexpr = f"({expression}) and not live" if expression else "not live"


# The environment variable behind each setting, e.g. TWITCH_CLIENT_ID, AUTOCLIPPER_DATA_DIR.
SETTINGS_ENV_VARS = frozenset(
    field.validation_alias.upper()
    for field in Settings.model_fields.values()
    if isinstance(field.validation_alias, str)
)


@pytest.fixture(autouse=True)
def _isolate_from_local_setup(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    """Keep every test away from the developer's .env, environment, data and logging setup.

    Each test runs with its own empty ``tmp_path`` as the working directory, so `get_settings`
    finds no ``.env`` unless the test writes one there, and the default data and artifacts
    directories point inside it. The variables behind AutoClipper's settings are removed from
    the environment, the settings cache is cleared before and after the test, and any logging
    setup is undone afterwards.

    `live` tests are the exception: they call real services with the developer's credentials,
    so they keep the working directory (and its ``.env``) and the environment. The settings
    cache and logging are still reset around them.
    """
    if request.node.get_closest_marker("live") is None:
        monkeypatch.chdir(tmp_path)
        for name in list(os.environ):
            # Settings match variable names case-insensitively, so remove every spelling.
            if name.upper() in SETTINGS_ENV_VARS:
                monkeypatch.delenv(name)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
    reset_logging()


# Fixtures that run FFmpeg: tests that use them get the `ffmpeg` marker automatically.
FFMPEG_FIXTURES = frozenset({"make_test_media"})

# Generating a few seconds of tiny test media takes well under a second.
TEST_MEDIA_TIMEOUT_S = 60.0


def _running_in_ci() -> bool:
    # GitHub Actions sets CI=true (most CI services set true or 1). Tests and conftest may read
    # the environment directly; library code reads it only through autoclipper.config.
    return os.environ.get("CI", "").strip().lower() in {"true", "1"}


def _missing_ffmpeg_programs() -> list[str]:
    missing: list[str] = []
    for program in (FFMPEG, FFPROBE):
        try:
            find_program(program)
        except FFmpegNotFoundError:
            missing.append(program)
    return missing


# tryfirst: the marker must be in place before pytest applies `-m` to deselect tests.
@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Add the `ffmpeg` marker to every test that uses one of the `FFMPEG_FIXTURES`."""
    for item in items:
        if isinstance(item, pytest.Function) and FFMPEG_FIXTURES.intersection(item.fixturenames):
            item.add_marker(pytest.mark.ffmpeg)


# tryfirst: check before fixtures are set up, so `make_test_media` never runs without FFmpeg.
@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item: pytest.Item) -> None:
    """Skip an `ffmpeg` test when FFmpeg is missing locally; error instead in CI."""
    if item.get_closest_marker("ffmpeg") is None:
        return
    missing = _missing_ffmpeg_programs()
    if not missing:
        return
    reason = f"{' and '.join(missing)} not found on PATH; install FFmpeg ({SETUP_DOC})"
    if _running_in_ci():
        pytest.fail(f"{reason}. In CI (CI=true), FFmpeg tests must run, not skip.", pytrace=False)
    pytest.skip(reason)


@pytest.fixture
def make_test_media(tmp_path: Path) -> Callable[..., Path]:
    """Return a function that generates a small media file in `tmp_path` with FFmpeg.

    The function takes these keyword arguments (all optional) and returns the file's path:

    - ``name`` (str, default ``"test.mp4"``): file name, may include subdirectories; the
      extension picks the container.
    - ``duration_s`` (float, default 1.0): length in seconds.
    - ``video_size`` (``(width, height)`` or None, default ``(320, 180)``): a ``testsrc2``
      test pattern encoded as H.264 (libx264, yuv420p); None for an audio-only file.
    - ``frame_rate`` (int, default 25): video frames per second.
    - ``sample_rate_hz`` (int, default 48000), ``channels`` (int, default 2), ``tone_hz``
      (float, default 440.0): a constant ``sine`` tone. Its codec is the container's FFmpeg
      default (AAC for ``.mp4``/``.m4a``, 16-bit PCM for ``.wav``).

    Media is generated at test time and never committed (AGENTS.md § 6 rule 1). Generating a
    file that already exists raises `FFmpegExitError` instead of overwriting it.
    """

    def make(
        name: str = "test.mp4",
        *,
        duration_s: float = 1.0,
        video_size: tuple[int, int] | None = (320, 180),
        frame_rate: int = 25,
        sample_rate_hz: int = 48_000,
        channels: int = 2,
        tone_hz: float = 440.0,
    ) -> Path:
        output = tmp_path / name
        output.parent.mkdir(parents=True, exist_ok=True)
        inputs: list[str] = []
        video_options: list[str] = []
        if video_size is not None:
            width, height = video_size
            pattern = f"testsrc2=size={width}x{height}:rate={frame_rate}:duration={duration_s}"
            inputs += ["-f", "lavfi", "-i", pattern]
            video_options += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
        tone = f"sine=frequency={tone_hz}:sample_rate={sample_rate_hz}:duration={duration_s}"
        inputs += ["-f", "lavfi", "-i", tone]
        command = ffmpeg_command("-n", *inputs, *video_options, "-ac", str(channels), output)
        run_command(command, timeout_s=TEST_MEDIA_TIMEOUT_S)
        return output

    return make
