# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project will follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html) once it has releases.

PRs that change user-visible behavior, data schemas, label definitions or evaluation metrics should add a line under **Unreleased**.

## [Unreleased]

### Added

- Initial project documentation: README, agent and contributor guides, product requirements with scope table, roadmap, architecture, data, ML and development docs, and GitHub issue/PR templates.
- ADRs: core stack accepted (Python/uv tooling, `src/` layout, chat + audio first, Parquet + DuckDB, FastAPI + Next.js); analysis/evaluation contracts; agent workflow with an optional second-opinion review loop.
- Repository configuration: `.gitignore` (synthetic fixtures allowed without exposing secrets), `.gitattributes`, `.editorconfig`, `.env.example`.
- Implementation backlog: GitHub issues for the MVP and optional polish, linked from `docs/project/backlog.md` and ordered in the roadmap.
- Category-first label policy without priority or size labels (`docs/project/labels.md`).
- Python package scaffold: uv project for Python 3.12 (`src/` layout, committed `uv.lock`), an `autoclipper` CLI with `--help` and `--version`, Ruff, Pyright and pytest configuration (live tests deselected by default), pre-commit hooks, and a GitHub Actions CI workflow on Ubuntu and Windows.

### Changed

- Product docs describe vertical clips and a highlights video, with shared collection/storage and optional hosted deployment. Local execution is the initial setup.
- Agent guidance supports autonomous implementation decisions and publishes branches/PRs when requested.
