# AGENTS.md

The operating manual for **anyone changing this repository**: AI coding agents (Claude Code, Codex, Cursor, Copilot, …) and humans. Agents: read it before starting a task. Details live in `docs/`; this file tells you which doc to read for which task.

> If this file and another doc disagree, **this file wins** and the other doc has a bug. Fix it in your PR.

---

## 1. What we're building (30-second version)

AutoClipper finds the best moments in a long **Twitch VOD** and turns them into ready-to-post vertical clips. The user gives the app a VOD (a link, or a VOD they already downloaded). The app pulls the stream's chat and audio, finds the moments that were funniest or most hype, and comes back with the **top 10 clips already cut to 9:16, captioned and ranked**, each with a one-line reason. The user can preview a clip, nudge its start/end and download it, or join the top moments into one 16:9 **highlights video** to watch.

Three users, one app: **clippers** and **small streamers** want the vertical clips to post; **viewers** who missed a stream want the highlights video.

How it finds moments: chat explodes and the streamer gets loud right after something happens. We measure those signals for every few seconds of the VOD, use the clips viewers already made as examples of "good moments", and train a model (logistic regression, then LightGBM) to score every moment. Simple rules (biggest chat spike, loudest moment) are the baselines the model has to beat.

**Context:** AutoClipper is a student data-science project for the CUNY Tech Prep (CTP) fellowship. The initial setup uses `autoclipper serve` plus the web app in a browser. Collection and storage can be shared, and hosted deployment is an option when useful. A project website and downloadable app are planned distribution options (stretch). See [data sources and delivery defaults](docs/decisions/0011-data-sources-and-local-delivery.md).

Full scope (must / should / could / won't): [`docs/product/prd.md`](docs/product/prd.md#scope-at-a-glance). Plan and dates: [`docs/product/roadmap.md`](docs/product/roadmap.md).

## 2. Current state

- [`docs/project/status.md`](docs/project/status.md) is the source of truth for what exists. Read it first.
- **Built:** Python tooling and CI, config/logging, a shared FFmpeg runner, the `autoclipper synth` fake-data generator, the Twitch API client, viewer-clip fetching, the chat replay parser, VOD audio download with 16 kHz mono normalization, vertical-MP4 export (cut and reframe, no captions yet), VOD time windows and viewer-clip moments, chat and audio features per window, and the step-1 evaluation (`autoclipper evaluate`). The Next.js app is a placeholder.
- **Not built yet:** the analysis pipeline, the backend, the app screens, a trained model and a real dataset.
- **Work items** are GitHub issues. Pick any open issue whose **Depends on** items are merged.
- **Decided:** everything needed to build the MVP. Stack and layout are in [`docs/decisions/`](docs/decisions/README.md); data sources, pilot channels, chat handling, the model target and delivery are in [data sources and delivery defaults](docs/decisions/0011-data-sources-and-local-delivery.md); the backend/frontend API, including the highlights video, is in [`docs/architecture/api-contract.md`](docs/architecture/api-contract.md). There is no list of open questions: when something isn't specified, make the engineering call (§ 10).

## 3. What to read for which task

Always read: this file → `docs/project/status.md` → the issue. Then, by task type:

| Task touches… | Also read |
|---|---|
| Twitch API, VOD/chat download | `docs/data/data-sources.md` |
| Datasets, schemas, storage layout | `docs/architecture/data-model.md`, `docs/data/collection-plan.md` |
| Labels, windows, chat lag | `docs/data/labeling.md` |
| Features, models, training | `docs/ml/modeling-plan.md`, `docs/ml/features.md` |
| Metrics, experiments, results | `docs/ml/evaluation.md`, `docs/ml/experiments.md` |
| Cutting, cropping, captioning, highlights video | `docs/architecture/overview.md` § Export, `docs/architecture/api-contract.md` |
| Backend API or web UI | `docs/architecture/api-contract.md`, `docs/architecture/overview.md`, `docs/product/prd.md` § 4 |
| Repo layout, tooling, style | `docs/development/conventions.md` |
| Tests | `docs/development/testing.md` |
| Git, PRs, commits | `CONTRIBUTING.md`, `docs/development/git-workflow.md` |
| Writing issues | `docs/agents/writing-tasks.md` |
| Unfamiliar term | `docs/project/glossary.md` |

## 4. Repository map

```text
.
├── AGENTS.md              ← you are here
├── CLAUDE.md              ← Claude Code entry point; imports this file
├── README.md              ← public overview and "start here"
├── CONTRIBUTING.md        ← issue → branch → PR → merge
├── pyproject.toml, uv.lock
├── configs/               ← channels.yaml and other non-secret config
├── src/autoclipper/       ← the Python package; cli.py is the `autoclipper` CLI
├── tests/                 ← pytest suite
├── web/                   ← Next.js frontend
├── docs/                  ← product, architecture, data, ml, development, agents, decisions, project
└── .github/               ← issue/PR templates, CI
```

Also planned: `experiments/`, `notebooks/`, `scripts/`, and git-ignored `data/` and `artifacts/`. Create a directory when a task needs it.

## 5. Commands

Run from the repo root. First-time setup is in [setup](docs/development/setup.md).

```bash
uv sync                        # create .venv with Python 3.12 and the locked deps
uv run pre-commit install      # once per clone
uv run ruff check .            # lint (add --fix to apply safe fixes)
uv run ruff format --check .   # format check (uv run ruff format . reformats)
uv run pyright                 # type check
uv run pytest                  # tests; live tests are deselected (-m live runs only those)
uv run autoclipper --help      # CLI entry point
uv run autoclipper synth --seed 1   # fake dev dataset in data/synthetic/seed-1
uv run autoclipper evaluate --help  # hit rate@10 report for a scorer's candidates
```

CI runs `uv sync --locked`, Ruff, Pyright and pytest on Ubuntu and Windows. Add dependencies with `uv add <pkg>` (dev tools: `uv add --dev <pkg>`) and commit `pyproject.toml` and `uv.lock` together. Use `uv run …` for everything.

## 6. Engineering basics

1. **Nothing heavy or secret in git.** No video/audio files, VOD or chat dumps, model weights, `.env`, tokens or keys. `data/` and `artifacts/` are git-ignored. Small synthetic test fixtures under `tests/fixtures/` are fine.
2. **Every change goes through a PR** from a branch (`main` is protected). Keep `main` green.
3. **Don't invent results.** Metrics and performance claims come from a reproducible run in `experiments/`. Write "TBD" otherwise.
4. **Keep docs true.** If you change behavior, a schema, a label definition or a metric, update the doc that describes it in the same PR.

## 7. How to do a task

1. **Read** this file, `status.md`, the issue, the docs from §3 and the code you'll touch.
2. **Decide and build.** Make sensible decisions yourself and note them in the PR description. For a large change, write a short plan first (in the PR or the issue) and then go.
3. **Branch:** `<type>/<issue#>-<short-slug>`, e.g. `feat/24-audio-features`.
4. **Implement and test.** Follow [conventions](docs/development/conventions.md). Test the changed behavior; bug fixes get a regression test.
5. **Update docs** in the same change, including `docs/project/status.md` when project state changes.
6. **Check your work:** run the checks relevant to the change and read your full diff. Say in the PR what you ran and anything that couldn't run.
7. **Commit** with short [Conventional Commits](https://www.conventionalcommits.org/) messages, e.g. `feat(features): add chat message rate`.
8. **Publish when requested.** Implement, test and commit autonomously. Push branches or open/update PRs when the user requests publication; a request to update an existing PR includes pushing the needed commits. Use [the template](.github/pull_request_template.md): what changed, the decisions you made, how you verified it, what's uncertain. Your human handles merging.

For substantial changes, an optional second opinion can help. See the [review guide](docs/agents/reviewing-agent-prs.md) for project-specific checks.

## 8. Definition of done

- [ ] The issue's "Done when" list is met (or the PR says why not).
- [ ] Relevant checks pass, or the PR states what remains unverified.
- [ ] New logic has tests.
- [ ] Docs affected by the change are updated.
- [ ] No media, data or secrets in the diff.
- [ ] UI changes include a screenshot when available.

## 9. Conventions at a glance

Full detail in `docs/development/conventions.md`.

- **Python:** 3.12, type hints everywhere, Ruff formatting (line length 100), `snake_case` modules and functions, `PascalCase` classes.
- **Time:** offsets inside the pipeline are **float seconds from VOD start** (`*_s` suffix). Wall-clock timestamps are **UTC, ISO-8601**. Never mix the two.
- **IDs:** Twitch's string IDs as-is (`video_id`, `clip_id`, `broadcaster_id`). Don't cast them to int.
- **Config:** environment variables (see `.env.example`) loaded through `autoclipper.config`. No hard-coded paths, keys or streamer names in library code.
- **Paths:** `pathlib.Path`. Code runs on Windows, macOS and Linux.
- **Logging:** the `logging` module in library code; the CLI may print.
- **Determinism:** training and eval entry points take a `seed`. Splits are saved to disk.
- **Data:** tabular data is Parquet, queried with DuckDB or Polars/pandas. Schemas: `docs/architecture/data-model.md`.

## 10. When you're unsure

- **Build from the documented defaults.** They make the first implementation concrete and can evolve with the product. Shared collection, shared storage and hosted deployment are valid options; choose the arrangement that fits the task and resources. Update affected contracts in the same change.
- **Ambiguous requirement:** pick the option that best serves the product in §1, note the assumption in the PR, and keep going.
- **Technical choice the docs don't cover** (a library, a threshold, a file layout, model parameters): make the call yourself, explain it in the PR, and update the doc if it changes a documented contract. Don't stop to ask, and don't open "needs a decision" issues.
- **Doc is wrong or stale:** fix it in your PR.
- **Found an unrelated bug:** open a `bug` issue (or fix it in a separate PR).

## 11. Notes for specific agents

- **Claude Code** reads `CLAUDE.md`, which imports this file.
- **Codex, Cursor, Copilot and others** read this file directly.
- **Parallel agents:** separate git worktrees and separate issues; don't edit the same files at once. See `docs/agents/parallel-work.md`.
- **Windows hosts:** `.gitattributes` enforces LF except `.bat`, `.cmd` and `.ps1` (CRLF). Use forward slashes in docs and code paths.
- **No AI attribution.** Don't add co-author, contributor or "generated by" lines to commits, PRs or files, and don't list an AI agent as an author or owner. The human who opens the PR is the author.
