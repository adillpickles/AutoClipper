"""Shared pytest hooks and fixtures."""

import re

import pytest

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
