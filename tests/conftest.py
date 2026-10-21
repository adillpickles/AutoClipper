"""Shared pytest hooks and fixtures."""

import os
import re
from collections.abc import Iterator
from pathlib import Path

import pytest

from autoclipper.config import Settings, get_settings
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
