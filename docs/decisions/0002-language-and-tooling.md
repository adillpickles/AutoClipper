# ADR-0002: Python 3.12 with uv, Ruff, Pyright, pytest

| | |
|---|---|
| **Status** | Accepted (2026-10-16, matches the team's project design doc) |
| **Date** | 2026-10-09 |
| **Related** | [Conventions](../development/conventions.md), [Setup](../development/setup.md) |

## Context

The core of the project is data processing and ML (audio analysis, tabular models, a small neural net), wrapped in a CLI and later a web API. The team is from a data-science program, works on Windows and macOS, and relies on AI agents that do best with mainstream, well-documented tools and fast, deterministic feedback (lint/type/test).

## Decision

- **Language:** Python **3.12** for the pipeline, ML, CLI and backend.
- **Environment and packaging:** [`uv`](https://docs.astral.sh/uv/) with `pyproject.toml` + `uv.lock` (committed). Run everything via `uv run`.
- **Lint and format:** Ruff (line length 100).
- **Types:** Pyright (`standard` mode).
- **Tests:** pytest (+ pytest-cov).
- **Hooks:** pre-commit (Ruff, notebook output stripping, large-file and secret checks).
- **CLI:** Typer. **Config:** pydantic-settings.
- **Data:** pandas and/or Polars, DuckDB, PyArrow/Parquet.
- **ML:** scikit-learn, LightGBM. PyTorch in an optional dependency group.
- **Audio/media:** librosa, soundfile, FFmpeg (system binary), yt-dlp.
- **Captions:** faster-whisper (CPU-capable).
- **CI:** GitHub Actions running lint + types + tests on PRs.

## Options considered

| Option | Pros | Cons |
|---|---|---|
| **uv** (chosen) | Fast, one tool for Python versions + venvs + lockfile, cross-platform | Newer than pip/Poetry |
| Poetry | Mature | Slower. Separate Python version management. |
| pip + requirements.txt | Familiar | No lockfile discipline. Env drift. |
| conda | Good for binary deps | Heavy. Slow solves. Agent-unfriendly. |
| mypy instead of Pyright | Widely known | Slower. Pyright gives better editor integration. |

## Consequences

- **Positive:** one command to set up (`uv sync`), reproducible environments, fast feedback loops for agents.
- **Negative:** contributors must install uv. FFmpeg and TwitchDownloaderCLI remain manual system installs.
- **Follow-ups:** the scaffold PR (roadmap M0) creates `pyproject.toml`, Ruff/Pyright/pytest config, pre-commit config and the CI workflow, and updates `AGENTS.md § 5`.

## Revisit when

A dependency we need is unavailable for 3.12, or GPU training needs push us toward a different environment manager.
