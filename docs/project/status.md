# Project Status

> **The living "what exists right now" document.** Every agent session reads it first. Update it **in the same PR** that changes the state. Don't mark anything done unless it's merged to `main` and tested.

**Last updated:** 2026-10-16
**Current phase:** M0 Foundations
**Next milestone:** M1 Data collection ([roadmap](../product/roadmap.md))
**Key dates:** Demo of Demos Fri Dec 4, 2026 · Final presentations and demo night Fri Dec 11, 2026

## Summary

Scope, architecture and team workflow are documented, and the core tech stack is decided. **There is no application code, no dataset and no model yet.** No results exist.

## What exists

| Area | State | Notes |
|---|---|---|
| Documentation | ✅ Initial set (PR #2) | Product scope, architecture, data, ML, development and agent workflow docs, ADRs |
| Repo config | ✅ Initial set (PR #2) | `.gitignore`, `.gitattributes`, `.editorconfig`, `.env.example`, issue/PR templates |
| GitHub settings | ✅ Configured | Labels; `main` ruleset (PR + 1 approval, squash only). A temporary admin bypass for initial setup PRs is active; remove it once teammates are reviewing. |
| Team access | ⚠️ Owner only | Teammates still need to be added as collaborators (OQ-11) |
| Issue backlog | ❌ Not created | Only #1 (setup review) exists |
| Python scaffold | ❌ Not started | ADR-0002/0003 accepted; first implementation task |
| CI | ❌ Not started | Added with the scaffold (GitHub Actions; free for this public repo) |
| Twitch developer app | ❌ Not registered | OQ-8 |
| Data collection | ❌ Not started | Needs channel list (OQ-4) and source/privacy decisions (OQ-9/OQ-17) before real data |
| Labels / features / models / eval | ❌ Not started | Training target is OQ-1 |
| Export | ❌ Not started | |
| Web app | ❌ Not started | FastAPI + Next.js accepted (ADR-0007); can start now with synthetic candidates |

## Decisions pending

See [open questions](open-questions.md). The ones that block upcoming work:

1. **Teammate access** (OQ-11): blocks teammates picking up issues.
2. **Channel list and collection scale** (OQ-4): blocks real collection.
3. **Source permissions and chat retention** (OQ-9/OQ-17): blocks real collection. Synthetic development can proceed.
4. **Training target** (OQ-1) and the relevant ADR-0009 contracts (OQ-15): block training and reporting a model.
5. **Shared data storage and collector host** (OQ-5): blocks collection at scale.

## Suggested next tasks

1. Add teammates as collaborators and create the first issue backlog from the roadmap.
2. Scaffold the Python project (ADR-0002/0003) with CI.
3. Next.js + FastAPI skeleton showing synthetic candidates (shortlist → preview → trim → export).
4. Register the Twitch app; implement and mock the Helix client (Get Videos, Get Clips).
5. Decide OQ-4/OQ-9/OQ-17 and start a small data pilot.

## Known issues / caveats

- Chat replay and VOD download rely on unofficial tools ([data sources](../data/data-sources.md)).
- Clip coverage, chat lag and alignment still need to be measured in a pilot.
- The team's original week-by-week schedule is behind (no code as of Oct 16); replan at the weekly sync.

## Changelog of this file

- 2026-10-09: initial documentation drafted and GitHub settings configured.
- 2026-10-13: contract and workflow repairs after the first audit.
- 2026-10-16: core ADRs accepted, scope table added to the PRD, contributor workflow simplified, optional review loop documented.
