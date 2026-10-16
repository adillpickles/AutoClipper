# Issue and PR Labels

Labels use plain names, without prefixes. The sections below explain their purpose. Each task should have **one type**, **at least one area** and, once triaged, **one priority** and **one size**. Status labels are added as needed. Questions and resolution labels do not need task labels.

`data` covers both data tasks and the data area; add it once. For a data task, it satisfies both requirements. For another task type (for example, `bug` + `data`), it identifies the area.

## How to add labels

- **On GitHub:** open the issue or PR → right sidebar → **Labels** (gear icon) → tick the labels → click away to save.
- **From the terminal:** `gh issue edit <N> --add-label feature,export` or `gh pr edit <N> --add-label feature,export`. Remove with `--remove-label`.
- **Issues:** the templates add `needs-triage` (and a type label for bugs, experiments, spikes and data tasks). Whoever triages adds the rest.
- **PRs:** copy the linked issue's type and area labels onto the PR.

## Type (exactly one)

| Label | Use for |
|---|---|
| `feature` | New functionality |
| `bug` | Something is broken |
| `docs` | Documentation only |
| `chore` | Tooling, CI, dependencies, refactors, repo housekeeping |
| `experiment` | An ML experiment with a hypothesis and metric |
| `spike` | A time-boxed investigation that ends in a written finding |
| `data` | Data collection, schemas, labeling, splits and storage; also the data area |

## Area (one or more)

| Label | Covers | Planned module |
|---|---|---|
| `ingestion` | Twitch API, chat replay, media download, collector | `ingest/` |
| `data` | Schemas, moments, windows, labels, splits, storage | `labels/`, data docs |
| `features` | Chat/audio/video feature extraction | `features/` |
| `modeling` | Training, models, evaluation | `models/`, `eval/` |
| `export` | Cutting, reframing, captions, encoding | `export/` |
| `api` | Backend API, jobs | `api/` |
| `web` | Frontend | `web/` |
| `infra` | CI, tooling, repo settings, dev environment | — |

## Priority (one, once triaged)

| Label | Meaning |
|---|---|
| `p0` | Critical: blocks a milestone or breaks `main`. Drop other work. |
| `p1` | Important for the current milestone |
| `p2` | Nice to have / later |

## Size (one, once triaged)

| Label | Rough effort |
|---|---|
| `small` | < 2 hours |
| `medium` | Half a day to a day |
| `large` | Multiple days. **Split before starting.** |

## Status

| Label | Meaning |
|---|---|
| `needs-triage` | New. Needs type/area/priority/size. Added automatically by templates. |
| `agent-ready` | Fully specified per [writing tasks](../agents/writing-tasks.md). An agent or newcomer can pick it up. |
| `needs-decision` | Blocked on a team decision (often an OQ or a proposed ADR) |
| `blocked` | Blocked by another issue or an external factor. Say what in a comment. |

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

Whoever triages (rotating, proposal: weekly) removes `needs-triage` after adding type, area, priority and size. Issues that are ready for pickup get `agent-ready`.

## Changing labels

Labels are configured in GitHub. To add, rename or remove one, update this doc and affected issue templates/references in a PR, then apply the change through the repository's **Issues → Labels** page (or with `gh label`). Rename existing labels to preserve assignments; before combining labels, transfer their issue/PR assignments to the surviving label.
