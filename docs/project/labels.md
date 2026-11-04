# Issue and PR Labels

| | |
|---|---|
| **Status** | The live GitHub labels match this doc. |
| **Last updated** | 2026-10-31 |

Labels say **what part of the project** an issue touches and, optionally, what kind of work it is. Usually one or two labels per issue.

## Category

| Label | Covers | Code |
|---|---|---|
| `frontend` | The Next.js app: screens, components, the API client | `web/` |
| `backend` | FastAPI app, jobs, analysis orchestration | `src/autoclipper/api/` |
| `ingestion` | Twitch API, chat replay, media download | `src/autoclipper/ingest/` |
| `data` | Windows, moments, labels, splits, datasets | `src/autoclipper/labels/` |
| `modeling` | Features, baselines, models, evaluation, experiments | `src/autoclipper/features/`, `models/`, `eval/` |
| `export` | Cutting, reframing, captions, encoding | `src/autoclipper/export/` |
| `tooling` | CI, dev environment, repo settings, housekeeping | — |

## Type (optional)

| Label | Use for |
|---|---|
| `feature` | New functionality |
| `bug` | Something is broken |
| `docs` | Documentation only |
| `spike` | A short investigation that ends in a written finding |
| `experiment` | An ML experiment with a question, metric and baseline |
| `stretch` | After the MVP works (Should/Could items in the PRD) |

## Status

| Label | Meaning |
|---|---|
| `blocked` | Stuck on something outside the issue list. Say what in a comment. |
| `good first issue` | Small and well scoped |
| `question`, `duplicate`, `wontfix` | Closing reasons |

An issue is ready to pick up when the issues under its **Depends on** are merged. No extra label needed.

## How to add labels

- **On GitHub:** issue or PR → right sidebar → **Labels**.
- **From the terminal:** `gh issue edit <N> --add-label frontend` (remove with `--remove-label`).
