# Project Status

> **The living "what exists right now" document.** Every agent session reads it first. Update it **in the same PR** that changes the state. Don't mark anything done unless it's merged to `main` and tested.

**Last updated:** 2026-10-09
**Current phase:** M0 Foundations (design / pre-code)
**Next milestone:** M1 Data collection ([roadmap](../product/roadmap.md))

## Summary

The project idea, scope and plan are documented. **There is no application code, no dataset and no model yet.** No results exist.

## What exists

| Area | State | Notes |
|---|---|---|
| Documentation | ✅ Initial set | Product, architecture, data, ML, development, agent workflow, ADRs |
| Repo config | ✅ | `.gitignore`, `.gitattributes`, `.editorconfig`, `.env.example`, issue/PR templates |
| GitHub settings | ✅ | Labels, `main` ruleset (PR + 1 approval + squash only), squash-only merges, auto-delete branches |
| Python scaffold | ❌ Not started | Waiting on [ADR-0002](../decisions/0002-language-and-tooling.md) ratification |
| CI | ❌ Not started | |
| Twitch developer app | ❌ Not registered | OQ-8 |
| Data collection | ❌ Not started | **Urgent:** VODs expire ([collection plan](../data/collection-plan.md)) |
| Labels / features / models / eval | ❌ Not started | |
| Export | ❌ Not started | |
| Web app | ❌ Not started | M5 |

## Decisions pending

See [open questions](open-questions.md). The ones that block upcoming work:

1. **Ratify the tooling and layout ADRs** (0002, 0003, 0005), which block the scaffold.
2. **Channel list and collection targets** (OQ-4), which block collection.
3. **Shared data storage and collector host** (OQ-5), which block collection at scale.
4. **v1 ranking target** (OQ-1), which blocks v1 modeling (not M1/M2).

## Suggested next tasks

1. Scaffold the Python project (ADR-0002/0003) with CI.
2. Register the Twitch app and implement the Helix client (Get Users / Get Videos / Get Clips).
3. Spike: Get Clips completeness for large channels (chunking strategy).
4. Spike: TwitchDownloader chat replay on 3 VODs (schema, size, speed).
5. Implement the collector and start collecting.

## Known issues / caveats

- Chat replay and VOD download rely on unofficial tools ([data sources](../data/data-sources.md)).
- Several facts in the data docs are marked ⚠️ and need verification (pagination completeness, retention periods, chat lag, clip-start bias).

## Changelog of this file

- 2026-10-09: initial version (documentation and repo setup).
