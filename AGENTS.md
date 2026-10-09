# AGENTS.md

This is the operating manual for **anyone making changes to this repository**: AI coding agents (Claude Code, Codex, Cursor, Copilot, …) and humans alike. Agents should read it in full before starting any task. It's kept short enough to read in one pass. Details live in `docs/`, and this file tells you which doc to read for which task.

> If this file and another doc disagree, **this file wins** and the other doc has a bug. Fix it or flag it in your PR.

---

## 1. What this project is (30-second version)

AutoClipper takes a **Twitch VOD**, finds and ranks the moments most worth turning into **short-form clips** (TikTok / Reels / Shorts), and exports them **vertical and captioned**. It is a tool for *clippers*.

The core data-science idea: the Twitch API lists **viewer-made clips** with their offsets into the VOD, subject to query limits and availability. Those are weak labels for "this moment was clip-worthy"; collection coverage must be checked. We extract **chat** and **audio** signals (and later **video**) per time window and train models to:

1. **Rank** existing viewer clips (v1).
2. **Detect** clip-worthy moments in un-clipped VOD time (v2).

Success means **beating simple baselines** (sort by Twitch views; chat-spike heuristic), measured offline. See `docs/ml/evaluation.md`.

## 2. Current state. Read before assuming anything exists.

- **Phase:** design / pre-code. **No application code, no dependencies, no CI yet.**
- The live source of truth for project state is [`docs/project/status.md`](docs/project/status.md). Read it at the start of every task.
- Many technical choices are **proposals**. Check the ADR status in [`docs/decisions/`](docs/decisions/README.md) before treating a choice as settled. `Proposed` means *don't build an irreversible dependency on it without asking*. `Accepted` means *follow it*.

## 3. Context loading: what to read for which task

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
| Backend API or web UI | `docs/architecture/overview.md`, ADR-0007 |
| Repo layout, tooling, style | `docs/development/conventions.md`, ADR-0002, ADR-0003 |
| Tests | `docs/development/testing.md` |
| Git, PRs, commits | `docs/development/git-workflow.md` |
| Unfamiliar term | `docs/project/glossary.md` |

Don't load every doc for every task. Read what the task needs.

## 4. Repository map

```text
.
├── AGENTS.md              ← you are here (canonical agent + human operating manual)
├── CLAUDE.md              ← Claude Code entry point; imports this file
├── README.md              ← public overview
├── CONTRIBUTING.md        ← how to contribute (PR flow, reviews)
├── docs/
│   ├── product/           ← PRD, roadmap: the "what" and "why"
│   ├── architecture/      ← system design, data model: the "how"
│   ├── data/              ← sources, collection, labeling, governance
│   ├── ml/                ← modeling plan, features, evaluation, experiments
│   ├── development/       ← setup, conventions, testing, git workflow
│   ├── agents/            ← how to run an agent-driven workflow on this repo
│   ├── decisions/         ← Architecture Decision Records (ADRs)
│   └── project/           ← status, risks, glossary, labels, open questions
└── .github/               ← issue/PR templates
```

The **planned** code layout (ADR-0003, *Proposed*) is `src/autoclipper/` (Python package), `web/` (frontend), `tests/`, `experiments/`, `notebooks/`, `scripts/`, and a git-ignored `data/` and `artifacts/`. Don't create these directories speculatively. Create one only when a task needs it.

## 5. Commands

> No code exists yet, so there are no commands. When the project is scaffolded, **the PR that adds tooling must update this section**. Planned (ADR-0002, *Proposed*):

```bash
uv sync                      # install deps
uv run pytest                # tests
uv run ruff check . && uv run ruff format --check .   # lint + format check
uv run pyright               # type check
uv run autoclipper --help    # CLI entry point
```

Use `uv run …` for everything. Never `pip install` into a global environment.

## 6. Hard rules (never break these)

1. **Never commit data, media, model weights, or secrets.** No `.mp4/.mkv/.wav/.m4a`, no VOD or chat dumps, no `.env`, no tokens, no `.pt/.onnx/.pkl` artifacts. `data/` and `artifacts/` are git-ignored. Small, synthetic **test fixtures** are allowed under `tests/fixtures/` and must not contain real usernames.
2. **Never commit real chat usernames or user IDs.** Pseudonymize at ingestion (see `docs/data/data-governance.md`).
3. **Never push to `main`.** Every change is a PR from a branch. `main` is protected and needs one approving review.
4. **Don't change a data schema, label definition, or evaluation metric without an ADR** (or an ADR update) in the same PR. These silently invalidate past results.
5. **Don't add a dependency** without saying why in the PR description. Prefer the standard library and the libraries already chosen.
6. **Don't invent results.** Never write metrics, numbers or claims into docs, READMEs or PRs unless they come from a reproducible run that's linked in `experiments/`. Prefer "TBD" over a guess.
7. **Don't silently resolve open decisions.** If a task depends on something listed in `docs/project/open-questions.md` or a `Proposed` ADR, stop and ask in the issue or PR. If you must proceed, choose the documented proposal and say so explicitly in the PR.
8. **Respect platform terms.** No scraping or automation that breaks Twitch/TikTok terms beyond what `docs/data/data-governance.md` permits. No code that posts to social accounts without an explicit, reviewed task.

## 7. How to do a task (the standard loop)

1. **Understand.** Read the issue, the docs from §3, and the code you'll touch. If the acceptance criteria are unclear or contradict the docs, comment on the issue before writing code.
2. **Branch.** `git switch -c <type>/<issue#>-<short-slug>`, e.g. `feat/12-chat-rate-features`. One issue per branch.
3. **Plan small.** Aim for a PR a reviewer can read in about 15 minutes (< ~400 changed lines excluding generated files). Split bigger work and say so in the issue.
4. **Implement**, following `docs/development/conventions.md`. Match the surrounding code.
5. **Test.** Add or adjust tests for behavior you change (`docs/development/testing.md`). Run lint, types and tests locally.
6. **Update docs in the same PR.** If behavior, schemas, commands or decisions changed, update the relevant doc. If project state changed (e.g. a milestone item is done), update `docs/project/status.md`.
7. **Commit** using [Conventional Commits](https://www.conventionalcommits.org/): `feat(features): add chat message-rate per window`.
8. **Open a PR** using the template. Link the issue (`Closes #12`), describe what you verified and how, and list anything you weren't sure about.

## 8. Definition of done

A change is done when **all** of these are true:

- [ ] Acceptance criteria in the issue are met (or deviations are explained in the PR).
- [ ] Lint, format, type check and tests pass locally (once tooling exists).
- [ ] New logic has tests. Bug fixes include a regression test.
- [ ] Docs affected by the change are updated in the same PR.
- [ ] No data, media, secrets or real usernames are in the diff.
- [ ] PR description says what was verified, how, and what's still uncertain.

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

- **Ambiguous requirement:** ask on the issue. Don't guess at product behavior.
- **Doc is wrong or stale:** fix it in your PR (small) or open a `docs` issue (large).
- **Found an unrelated bug:** don't fix it in this PR. Open an issue labeled `bug`.
- **Blocked on a decision:** label the issue `needs-decision`, write down the options and your recommendation, and stop.
- **Something you did might be risky** (data deletion, history rewrite, external API writes): don't do it. Describe it in the PR and let a human decide.

## 11. Notes for specific agents

- **Claude Code** reads `CLAUDE.md`, which imports this file. Put Claude-specific notes there, not here.
- **Codex / other agents** read this file directly.
- **Parallel agents:** use separate git worktrees and separate issues. Don't have two agents edit the same files at the same time. See `docs/agents/parallel-work.md`.
- **Windows hosts:** the repo enforces LF line endings via `.gitattributes`. Don't commit CRLF. Use forward slashes in docs and code paths.
- **Don't add AI attribution lines, co-author, contributor or ownership trailers, or "generated by" footers** to commits, PRs or files. Do not add an AI agent as an author, contributor or owner. Authorship is the human who opens the PR. Keep required third-party license/source acknowledgments and the PR template's AI-assistance disclosure.
