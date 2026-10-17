# Issue and PR Labels

| | |
|---|---|
| **Status** | Binding. The live GitHub labels match this doc. |
| **Last updated** | 2026-10-17 |

Labels tell someone scanning the issue list **what kind of work an issue is** and whether it's ready to pick up. Use plain names and add what's useful: usually one or two categories, plus a type if it isn't plain feature work. There's no required number of labels, and no priority or size labels.

## Category: what part of the project

| Label | Covers | Planned code |
|---|---|---|
| `frontend` | The Next.js web app: screens, components, the API client | `web/` |
| `backend` | FastAPI app, jobs, the analysis orchestration | `src/autoclipper/api/` |
| `ingestion` | Twitch API, chat replay, media download, the collector | `src/autoclipper/ingest/` |
| `data` | Schemas, windows, moments, labels, splits, datasets, data permissions | `src/autoclipper/labels/`, data docs |
| `modeling` | Features, baselines, models, evaluation, experiments | `src/autoclipper/features/`, `models/`, `eval/` |
| `export` | Cutting, reframing, captions, encoding | `src/autoclipper/export/` |
| `tooling` | Scaffold, CI, dev environment, repo settings, housekeeping | — |

## Type: what kind of work (optional)

| Label | Use for |
|---|---|
| `feature` | New functionality (optional; most category-labeled tasks are features) |
| `bug` | Something is broken |
| `docs` | Documentation only |
| `spike` | A time-boxed investigation that ends in a written finding |
| `experiment` | An ML experiment with a hypothesis, metric and baseline |
| `decision` | A team decision recorded in an ADR or [open questions](open-questions.md). Agents can draft options but don't make the call. |

## Workflow: is it ready?

| Label | Meaning |
|---|---|
| `agent-ready` | Fully specified per [writing tasks](../agents/writing-tasks.md), with no open decision upstream. A teammate or AI agent can pick it up once its **Depends on** issues are merged. |
| `needs-decision` | Waits on an open `decision` issue, directly or through a dependency. When the decision closes, re-check the issue and swap this for `agent-ready`. |
| `blocked` | Stuck on something outside the issue list (an external service, access, a person). Say what in a comment. |
| `good first issue` | Small and well scoped; a good way to learn the repo |

Plain unfinished dependencies don't need a label: the issue's **Depends on** section already says what must merge first.

## Closing labels

| Label | Meaning |
|---|---|
| `question` | A question, not a task |
| `duplicate` | Already tracked elsewhere (link it) |
| `wontfix` | Deliberately not doing this, or not actionable (explain why) |

## How to add labels

- **On GitHub:** open the issue or PR → right sidebar → **Labels** (gear icon) → tick the labels → click away to save.
- **From the terminal:** `gh issue edit <N> --add-label frontend,feature` or `gh pr edit <N> --add-label frontend`. Remove with `--remove-label`.
- **Issues:** the Bug, Experiment, Spike and Data templates add their type or category automatically. Add a category when you file a Task. Drafts in the [backlog](backlog.md) list suggested labels.
- **PRs:** copy the linked issue's category labels onto the PR.

## Changing live labels

Change the labels in this doc and every template/reference in a PR, then apply the change on GitHub (**Issues → Labels**, or `gh label`). **Rename** a label to keep its assignments; to **merge** two labels, move the old label's issues/PRs to the surviving label before deleting it.

### Last change (2026-10-17)

The old 31-label set was mapped to this one: `web`, `api` and `infra` were renamed to `frontend`, `backend` and `tooling`; `chore`, `features` and `invalid` were folded into `tooling`, `modeling` and `wontfix`; the priority (`p0`–`p2`), size (`small`/`medium`/`large`), `needs-triage`, `help wanted` and `accessibility` labels were deleted; `decision` was added.
