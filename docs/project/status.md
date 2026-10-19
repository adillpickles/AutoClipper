# Project Status

> **The living "what exists right now" document.** Every agent session reads it first. Update it **in the same PR** that changes the state. Don't mark anything done unless it's merged to `main` and tested.

**Last updated:** 2026-10-19
**Current phase:** M0 Foundations
**Next milestone:** M1 Data collection ([roadmap](../product/roadmap.md))
**Key dates:** Demo of Demos Fri Dec 4, 2026 · Final presentations and demo night Fri Dec 11, 2026

## Summary

Scope, architecture and team workflow are documented, and the core tech stack is decided. The Python package scaffold (an `autoclipper` CLI with no subcommands yet, lint/type/test tooling, pre-commit hooks and CI) exists. **There is no pipeline or app code, no dataset and no model yet.** No results exist.

## What exists

| Area | State | Notes |
|---|---|---|
| Documentation | ✅ Initial set (PR #2) | Product scope, architecture, data, ML, development and agent workflow docs, ADRs |
| Repo config | ✅ Initial set (PR #2) | `.gitignore`, `.gitattributes`, `.editorconfig`, `.env.example`, issue/PR templates |
| GitHub settings | ✅ Configured | `main` ruleset (PR + 1 approval, squash only). A temporary admin bypass for initial setup PRs is active; remove it once teammates are reviewing. |
| Labels | ✅ Simplified set live | 20 category-first labels per [labels](labels.md) |
| Team access | ⚠️ Owner only | Teammates still need to be added as collaborators (OQ-11) |
| Issue backlog | ✅ Published | Waves 0–6 are issues #3–#50 (numbers in the [backlog](backlog.md) index); optional drafts not published yet |
| Python scaffold | ✅ In place (#5) | uv project (Python 3.12, `src/` layout, `uv.lock`), `autoclipper` CLI shell with `--help`/`--version`, Ruff, Pyright (`standard`), pytest (live tests deselected by default) and pre-commit hooks. Commands: [`AGENTS.md` § 5](../../AGENTS.md#5-commands) |
| CI | ✅ In place (#5) | GitHub Actions `ci.yml`: `uv sync --locked`, Ruff, Pyright and pytest on Ubuntu and Windows for every PR and push to `main`. Not a required check yet (#3) |
| Twitch developer app | ❌ Not registered | OQ-8 |
| Data collection | ❌ Not started | Needs channel list (OQ-4) and source/privacy decisions (OQ-9/OQ-17) before real data |
| Labels / features / models / eval | ❌ Not started | Training target is OQ-1 |
| Export | ❌ Not started | |
| Web app | ❌ Not started | FastAPI + Next.js accepted (ADR-0007); can start now with synthetic candidates |

## Decisions pending

See [open questions](open-questions.md). Each one has a draft in Wave 0 (team access) or Wave 1 (`decision` tasks) of the [backlog](backlog.md):

1. **Teammate access** (OQ-11): blocks teammates picking up issues.
2. **Job and candidate API contract** (ADR-0009): blocks the FastAPI and Next.js screens.
3. **Dataset construction rules and v0 feature set** (ADR-0009): block windows, moments and features.
4. **Source permissions and chat retention** (OQ-9/OQ-17): block real collection. Synthetic development can proceed.
5. **Channels, collection scale and storage** (OQ-4/OQ-5): block the pilot and collector.
6. **Training target** (OQ-1) and **evaluation protocol** (ADR-0009): block labels, metrics and reported models.

## Suggested next tasks

Work through the [backlog](backlog.md) in wave order. Startable now: the Wave 0 owner tasks (#3 team access, #4 Twitch app), the Next.js scaffold (#6), the config module (#14), and the five Wave 1 decisions that have no dependencies (#7, #9, #10, #11, #13). Once the config module merges, the mocked ingestion adapters, synthetic dataset and export tasks can start; they're already `agent-ready`.

## Known issues / caveats

- The Python setup was tested locally on Windows only; Linux runs in CI (Ubuntu). Nobody has tried macOS yet.
- Chat replay and VOD download rely on unofficial tools ([data sources](../data/data-sources.md)).
- Clip coverage, chat lag and alignment still need to be measured in a pilot.
- The team's original week-by-week schedule is behind (no code as of Oct 16); replan at the weekly sync.

## Changelog of this file

- 2026-10-09: initial documentation drafted and GitHub settings configured.
- 2026-10-13: contract and workflow repairs after the first audit.
- 2026-10-16: core ADRs accepted, scope table added to the PRD, contributor workflow simplified, optional review loop documented.
- 2026-10-16: implementation backlog drafted; label policy simplified.
- 2026-10-17: simplified labels applied on GitHub; backlog Waves 0–6 published as issues #3–#50.
- 2026-10-19: Python package scaffold, tooling and CI added (#5).
