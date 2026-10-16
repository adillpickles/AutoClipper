# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project will follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html) once it has releases.

PRs that change user-visible behavior, data schemas, label definitions or evaluation metrics should add a line under **Unreleased**.

## [Unreleased]

### Added

- Initial project documentation: README, agent and contributor guides, product requirements with scope table, roadmap, architecture, data, ML and development docs, and GitHub issue/PR templates.
- ADRs: core stack accepted (Python/uv tooling, `src/` layout, chat + audio first, Parquet + DuckDB, FastAPI + Next.js); proposed analysis/evaluation contracts (ADR-0009); agent workflow with an optional second-opinion review loop (ADR-0010).
- Repository configuration: `.gitignore` (synthetic fixtures allowed without exposing secrets), `.gitattributes`, `.editorconfig`, `.env.example`.
