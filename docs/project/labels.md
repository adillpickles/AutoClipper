# Issue and PR Labels

Labels are grouped by prefix. Each issue should have **exactly one `type:`**, **at least one `area:`** and, once triaged, **one `priority:`** and **one `size:`**. `status:` labels are added as needed.

## Type (exactly one)

| Label | Use for |
|---|---|
| `type:feature` | New functionality |
| `type:bug` | Something is broken |
| `type:docs` | Documentation only |
| `type:chore` | Tooling, CI, dependencies, refactors, repo housekeeping |
| `type:experiment` | An ML experiment with a hypothesis and metric |
| `type:spike` | A time-boxed investigation that ends in a written finding |
| `type:data` | Data collection, labeling or dataset definition |

## Area (one or more)

| Label | Covers | Planned module |
|---|---|---|
| `area:ingestion` | Twitch API, chat replay, media download, collector | `ingest/` |
| `area:data` | Schemas, moments, windows, labels, splits, storage | `labels/`, data docs |
| `area:features` | Chat/audio/video feature extraction | `features/` |
| `area:modeling` | Training, models, evaluation | `models/`, `eval/` |
| `area:export` | Cutting, reframing, captions, encoding | `export/` |
| `area:api` | Backend API, jobs | `api/` |
| `area:web` | Frontend | `web/` |
| `area:infra` | CI, tooling, repo settings, dev environment | — |

## Priority (one, once triaged)

| Label | Meaning |
|---|---|
| `priority:p0` | Critical: blocks a milestone or breaks `main`. Drop other work. |
| `priority:p1` | Important for the current milestone |
| `priority:p2` | Nice to have / later |

## Size (one, once triaged)

| Label | Rough effort |
|---|---|
| `size:s` | < 2 hours |
| `size:m` | Half a day to a day |
| `size:l` | Multiple days. **Split before starting.** |

## Status

| Label | Meaning |
|---|---|
| `status:needs-triage` | New. Needs type/area/priority/size. Added automatically by templates. |
| `status:agent-ready` | Fully specified per [writing tasks](../agents/writing-tasks.md). An agent or newcomer can pick it up. |
| `status:needs-decision` | Blocked on a team decision (often an OQ or a proposed ADR) |
| `status:blocked` | Blocked by another issue or an external factor. Say what in a comment. |

## Community and resolution (GitHub defaults kept)

| Label | Meaning |
|---|---|
| `good first issue` | Small, well scoped, good for getting familiar with the repo |
| `help wanted` | Extra attention or volunteers needed |
| `question` | A question, not a task |
| `duplicate` | Already tracked elsewhere (link it) |
| `invalid` | Not actionable or not a real issue |
| `wontfix` | Deliberately not doing this (explain why) |
| `accessibility` | Affects accessibility (e.g. captions, UI) |

## Triage

Whoever triages (rotating, proposal: weekly) removes `status:needs-triage` after adding type, area, priority and size. Issues that are ready for pickup get `status:agent-ready`.

## Changing labels

Labels are configured in GitHub. To add, rename or remove one, update this doc in a PR and apply the change in **Settings → Labels** (or with `gh label`).
