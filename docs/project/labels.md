# Issue and PR Labels

| | |
|---|---|
| **Status** | Binding for this repo's docs and templates. **Live GitHub labels still use the old set** until the owner applies the [mapping below](#changing-live-labels). |
| **Last updated** | 2026-10-16 |

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

### Pending mapping (owner action)

The live repo still has the old 31 labels. Apply this once this doc is on `main`:

| Old label | New label | Action | Current assignments (2026-10-16) |
|---|---|---|---|
| `web` | `frontend` | Rename | none |
| `api` | `backend` | Rename | none |
| `infra` | `tooling` | Rename (keeps the assignment) | issue #1 |
| `chore` | `tooling` | Merge into `tooling`, then delete | none |
| `features` | `modeling` | Merge into `modeling`, then delete | none |
| `invalid` | `wontfix` | Merge into `wontfix`, then delete | none |
| `p0`, `p1`, `p2` | — | Delete (no priority labels) | `p1`: issue #1 |
| `small`, `medium`, `large` | — | Delete (no size labels) | `small`: issue #1 |
| `needs-triage` | — | Delete | none |
| `help wanted`, `accessibility` | — | Delete (unused) | none |
| — | `decision` | Create | — |
| `feature`, `bug`, `docs`, `spike`, `experiment`, `ingestion`, `data`, `modeling`, `export`, `agent-ready`, `needs-decision`, `blocked`, `good first issue`, `question`, `duplicate`, `wontfix` | same | Keep | `docs`: issue #1 |

Only issue #1 has labels today; PR #2 has none. Deleting `p1` and `small` removes them from issue #1 on purpose. Also update label descriptions to match the tables above (e.g. `modeling` now includes features). Afterwards, `gh label list` should show exactly the 20 labels documented above.
