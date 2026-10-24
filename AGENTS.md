# AGENTS.md

The operating manual for **anyone changing this repository**: AI coding agents (Claude Code, Codex, Cursor, Copilot, …) and humans. Agents: read it in full before starting any task. Details live in `docs/`; this file tells you which doc to read for which task.

> If this file and another doc disagree, **this file wins** and the other doc has a bug. Fix it or flag it in your PR.

---

## 1. What we're building (30-second version)

AutoClipper finds the highlights in a long **Twitch VOD**. A user pastes a VOD link; the app pulls the stream's chat, audio and any viewer-made clips, finds and ranks the best moments, and shows them in a web app. The user previews a moment, adjusts its start/end, and exports it as a vertical (9:16) MP4 with captions.

It's for **viewers** who missed a stream, **streamers** without an editor, and **clippers/editors** who don't want to scrub hours of footage. All three use the same app.

Under the hood: cheap signals (chat activity, audio loudness) are computed for the whole VOD; viewer clips act as noisy training labels; a trained model (logistic regression, then LightGBM) is compared against simple baselines. The app must still find moments when a VOD has no viewer clips. Report results honestly, even when a baseline wins; app and export work never wait on the model.

The full scope (must / should / could / won't) is in [`docs/product/prd.md`](docs/product/prd.md#scope-at-a-glance).

## 2. Current state. Read before assuming anything exists.

- **Phase:** M0 Foundations, with early ingestion components implemented. Python tooling and CI, configuration/logging, a shared FFmpeg runner, the `autoclipper synth` generator, the Helix metadata client, the chat replay parser and VOD audio download/normalization exist. The Next.js app has a placeholder page. **No analysis pipeline, backend, trained model or real dataset yet.** Vertical export (#20) remains an unmerged draft.
- Planned work is in GitHub issues #3–#50, drafted in dependency order in [`docs/project/backlog.md`](docs/project/backlog.md) (its index maps drafts to issue numbers). Don't start a task whose **Depends on** items aren't merged or whose label is `needs-decision`.
- [`docs/project/status.md`](docs/project/status.md) is the source of truth for what exists. Read it at the start of every task.
- **Decided** (ADRs marked *Accepted*, follow them): Python 3.12 + uv tooling, single repo with `src/` layout, chat + audio before video, Parquet + DuckDB for pipeline data, FastAPI backend + Next.js frontend. See [`docs/decisions/`](docs/decisions/README.md).
- **Still open** (don't decide these yourself; see [open questions](docs/project/open-questions.md)): the exact training target (OQ-1), chat retention and data permissions (OQ-9/OQ-17), hosting and app database (OQ-18), and the detailed label/evaluation contracts in ADR-0009.

## 3. What to read for which task

Always read: this file → `docs/project/status.md` → the issue you're working on.
Then, by task type:

| Task touches… | Also read |
|---|---|
| Twitch API, VOD/chat download | `docs/data/data-sources.md`, `docs/data/data-governance.md` |
| Datasets, schemas, storage layout | `docs/architecture/data-model.md`, `docs/data/collection-plan.md` |
| Labels, windows, chat lag | `docs/data/labeling.md` |
| Feature extraction, models, training | `docs/ml/modeling-plan.md`, `docs/ml/features.md` |
| Metrics, experiments, results | `docs/ml/evaluation.md`, `docs/ml/experiments.md` |
| Cutting / cropping / captioning | `docs/architecture/overview.md` § Export |
| Backend API or web UI | `docs/architecture/overview.md`, `docs/architecture/api-contract.md` (proposed), ADR-0007 |
| Repo layout, tooling, style | `docs/development/conventions.md`, ADR-0002, ADR-0003 |
| Tests | `docs/development/testing.md` |
| Git, PRs, commits | `CONTRIBUTING.md`, `docs/development/git-workflow.md` |
| Writing or picking issues, labels | `docs/project/backlog.md`, `docs/agents/writing-tasks.md`, `docs/project/labels.md` |
| Unfamiliar term | `docs/project/glossary.md` |

Don't load every doc for every task. Read what the task needs.

## 4. Repository map

```text
.
├── AGENTS.md              ← you are here (agent + human operating manual)
├── CLAUDE.md              ← Claude Code entry point; imports this file
├── README.md              ← public overview and "start here"
├── CONTRIBUTING.md        ← step by step: issue → branch → PR → merge
├── pyproject.toml         ← Python project: dependencies and Ruff/Pyright/pytest config
├── uv.lock                ← locked dependency versions (commit with pyproject.toml)
├── .pre-commit-config.yaml ← git hooks (Ruff, notebook stripping, large files, private keys)
├── src/autoclipper/       ← the Python package; cli.py is the `autoclipper` CLI
├── tests/                 ← pytest suite
├── docs/
│   ├── product/           ← PRD (scope), roadmap: the "what" and "why"
│   ├── architecture/      ← system design, data model: the "how"
│   ├── data/              ← sources, collection, labeling, governance
│   ├── ml/                ← modeling plan, features, evaluation, experiments
│   ├── development/       ← setup, conventions, testing, git workflow
│   ├── agents/            ← working with AI agents (tasks, prompts, optional review loop)
│   ├── decisions/         ← Architecture Decision Records (ADRs)
│   └── project/           ← status, backlog (issue drafts), open questions, risks, labels, glossary
└── .github/               ← issue/PR templates, CI workflow (workflows/ci.yml)
```

Code layout (ADR-0003, *Accepted*): `src/autoclipper/` (Python package), `web/` (Next.js frontend), `tests/`, `experiments/`, `notebooks/`, `scripts/`, and git-ignored `data/` and `artifacts/`. Create a directory only when a task needs it.

## 5. Commands

Run from the repo root. First-time setup (installing uv, PowerShell variants) is in [setup](docs/development/setup.md).

```bash
uv sync                        # create .venv with Python 3.12 and the locked deps, dev tools included
uv run pre-commit install      # once per clone: run the git hooks on every commit
uv run ruff check .            # lint (add --fix to apply safe fixes)
uv run ruff format --check .   # format check (uv run ruff format . reformats)
uv run pyright                 # type check (src/ and tests/)
uv run pytest                  # tests; live tests are deselected (-m live runs only those)
uv run autoclipper --help      # CLI entry point
uv run autoclipper synth --seed 1   # synthetic dev dataset in data/synthetic/seed-1 (docs/development/testing.md)
```

CI (`.github/workflows/ci.yml`) runs `uv sync --locked`, which fails if `uv.lock` is out of date, then the Ruff, Pyright and pytest commands above on Ubuntu and Windows for every PR and push to `main`. Add dependencies with `uv add <pkg>` (dev tools: `uv add --dev <pkg>`) and commit `pyproject.toml` and `uv.lock` together. Tool settings live in `pyproject.toml`.

Use `uv run …` for everything. Never `pip install` into a global environment.

## 6. Hard rules (never break these)

1. **Never commit data, media, model weights or secrets.** No `.mp4/.mkv/.wav/.m4a`, no VOD or chat dumps, no `.env`, no tokens, no `.pt/.onnx/.pkl` files. `data/` and `artifacts/` are git-ignored. Small synthetic test fixtures are allowed under `tests/fixtures/` and must not contain real usernames.
2. **Never commit real chat usernames or user IDs.** Pseudonymize at ingestion ([data governance](docs/data/data-governance.md)).
3. **Never push to `main`.** Every change is a PR from a branch.
4. **Don't change a data schema, label definition or evaluation metric without an ADR** (or ADR update) in the same PR. These silently invalidate past results.
5. **Don't add a dependency** without saying why in the PR description. Prefer the standard library and libraries already chosen in ADR-0002.
6. **Don't invent results.** No metrics or performance claims in docs, READMEs or PRs unless they come from a reproducible run linked in `experiments/`. Write "TBD" instead of guessing.
7. **Don't decide open questions yourself.** If your task depends on something in [open questions](docs/project/open-questions.md) or a *Proposed* ADR, stop and ask your human. Keep working on the parts that don't depend on it.
8. **Respect platform terms.** Follow [data governance](docs/data/data-governance.md). Never write code that posts to social accounts.
9. **Don't push, open or update PRs, comment on GitHub, or merge unless your human tells you to.** Committing locally is fine. Merging always needs a human's go-ahead.

## 7. How to do a task

1. **Read.** This file, `status.md`, the issue, the §3 docs for your task type, and the code you'll touch. Check the issue's **Depends on** list: if a dependency isn't merged yet, stop and tell your human.
2. **Plan.** For a small, well-specified issue, go ahead. For anything bigger or unclear, give a short plan (files, approach, tests, open questions) and wait for your human's OK. Use plan mode if your tool has one.
3. **Branch.** `<type>/<issue#>-<short-slug>`, e.g. `feat/14-audio-loudness`. Keep the diff reviewable (aim for < ~400 changed lines, excluding generated files).
4. **Implement and test.** Follow [conventions](docs/development/conventions.md). Test the changed behavior; bug fixes get a regression test ([testing](docs/development/testing.md)).
5. **Update docs** in the same change: affected behavior, schemas, commands, and `docs/project/status.md` if project state changed.
6. **Check your own work.** Run lint, type check and tests. Read your full diff. Walk through the definition of done (§8).
7. **Commit** with short [Conventional Commits](https://www.conventionalcommits.org/) messages, e.g. `feat(features): add chat message rate`.
8. **Hand off.** Tell your human what changed, how you verified it and what's uncertain. Draft the PR description using [the template](.github/pull_request_template.md). Push and open the PR only when asked.

**Optional second-opinion review.** Some teammates have a different agent audit the finished branch before opening the PR, then verify each finding in a fresh session. It's recommended for large or risky changes, not required. If your human asks you to audit a branch or to verify audit findings, follow [docs/agents/review-loop.md](docs/agents/review-loop.md).

## 8. Definition of done

- [ ] The issue's acceptance criteria are met (or the PR explains why not).
- [ ] Lint, format, type check and tests pass locally (§5).
- [ ] New logic has tests. Bug fixes include a regression test.
- [ ] Docs affected by the change are updated in the same PR.
- [ ] No data, media, secrets or real usernames in the diff.
- [ ] UI changes include a screenshot (before/after if something changed visually).
- [ ] The PR description says what changed, how it was verified and what's still uncertain.

## 9. Conventions at a glance

Full detail in `docs/development/conventions.md`.

- **Python:** 3.12, type hints everywhere, Ruff formatting (line length 100), `snake_case` modules and functions, `PascalCase` classes.
- **Time:** all offsets inside the pipeline are **float seconds from VOD start** (`*_s` suffix, e.g. `vod_offset_s`). All wall-clock timestamps are **UTC, ISO-8601**. Never mix the two.
- **IDs:** use Twitch's string IDs as-is (`video_id`, `clip_id`, `broadcaster_id`). Don't cast them to int.
- **Config:** environment variables (see `.env.example`) loaded through one config module. No hard-coded paths, keys or streamer names in library code.
- **Paths:** use `pathlib.Path`. Code must run on Windows, macOS and Linux.
- **Logging:** the `logging` module, not `print`, in library code. CLI output may print.
- **Determinism:** every training or eval entry point takes a `seed`. Splits are saved to disk, not recomputed.
- **Data:** tabular data is stored as Parquet. Query it with DuckDB or Polars/pandas. Schemas are in `docs/architecture/data-model.md`.

## 10. When you're unsure

- **Ambiguous requirement:** ask your human. Don't guess at product behavior.
- **Doc is wrong or stale:** fix it in your PR (small) or suggest a `docs` issue (large).
- **Found an unrelated bug:** don't fix it in this PR. Suggest a `bug` issue.
- **Blocked on a decision:** write down the options and your recommendation for your human. Continue with independent work.
- **Something might be risky** (deleting data, rewriting history, writing to external services): don't do it. Describe it and let a human decide.

## 11. Notes for specific agents

- **Claude Code** reads `CLAUDE.md`, which imports this file. Put Claude-specific notes there.
- **Codex, Cursor, Copilot and others** read this file directly.
- **Parallel agents:** use separate git worktrees and separate issues. Don't have two agents edit the same files at once. See `docs/agents/parallel-work.md`.
- **Windows hosts:** `.gitattributes` enforces LF except `.bat`, `.cmd` and `.ps1` (CRLF). Use forward slashes in docs and code paths.
- **No AI attribution.** Don't add co-author, contributor or "generated by" lines to commits, PRs or files, and don't list an AI agent as an author or owner. The human who opens the PR is the author.
