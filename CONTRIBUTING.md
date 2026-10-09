# Contributing to AutoClipper

Thanks for helping build AutoClipper. This guide covers how work moves from an idea to a merged pull request. It applies to everyone, human or AI agent. Agents should also read [`AGENTS.md`](AGENTS.md), which has the hard rules and the standard task loop.

## Contents

1. [Ground rules](#1-ground-rules)
2. [Finding work](#2-finding-work)
3. [Filing issues](#3-filing-issues)
4. [Making a change](#4-making-a-change)
5. [Pull requests and review](#5-pull-requests-and-review)
6. [Working with AI coding agents](#6-working-with-ai-coding-agents)
7. [Documentation changes](#7-documentation-changes)
8. [Decisions and ADRs](#8-decisions-and-adrs)
9. [Getting help](#9-getting-help)

---

## 1. Ground rules

- Be kind and constructive. See the [Code of Conduct](CODE_OF_CONDUCT.md).
- **`main` is protected.** No direct pushes. Every change lands through a PR with **at least one approving review** from someone other than the author, and is **squash-merged**.
- **No data, media, secrets or real chat usernames in git, ever.** See [`docs/data/data-governance.md`](docs/data/data-governance.md).
- **Docs are part of the change.** A PR that changes behavior without updating the relevant docs isn't done.
- **Honesty over polish.** Don't report results you didn't reproduce, and say what you didn't test.

## 2. Finding work

- Work is tracked in **GitHub Issues**. Labels are explained in [`docs/project/labels.md`](docs/project/labels.md).
- Good starting points:
  - `good first issue`: small and well scoped
  - `status:agent-ready`: fully specified, so an AI agent or new contributor can pick it up without extra context
- **Before starting, assign yourself** (or comment "taking this") so two people don't do the same work.
- Not sure what matters most right now? Check [`docs/product/roadmap.md`](docs/product/roadmap.md) and [`docs/project/status.md`](docs/project/status.md).

## 3. Filing issues

Use the issue templates. Each one asks for what a reviewer, or an agent, needs:

| Template | Use for |
|---|---|
| **Task** | A unit of implementation work with clear acceptance criteria. This is the default. |
| **Bug report** | Something behaves incorrectly |
| **Experiment** | An ML experiment with a hypothesis, metric and baseline |
| **Research spike** | A time-boxed investigation that ends in a written finding or recommendation |
| **Data task** | Data collection, labeling or dataset changes |

A good issue is **small** (roughly one PR), **testable** (acceptance criteria you can check) and **self-contained** (links the docs a newcomer needs). See [`docs/agents/writing-tasks.md`](docs/agents/writing-tasks.md). That guide is written for agent-ready issues, but every good issue follows it.

## 4. Making a change

### Setup

Follow [`docs/development/setup.md`](docs/development/setup.md).

### Branching

Branch from an up-to-date `main`:

```bash
git switch main && git pull
git switch -c <type>/<issue#>-<short-slug>
```

| Prefix | For |
|---|---|
| `feat/` | New functionality |
| `fix/` | Bug fixes |
| `docs/` | Documentation only |
| `chore/` | Tooling, CI, deps, repo housekeeping |
| `refactor/` | Code restructuring with no behavior change |
| `exp/` | ML experiments (code + config + results summary) |
| `data/` | Data collection/labeling scripts or dataset definitions |

Example: `feat/14-audio-loudness-features`.

### Commits

Use [Conventional Commits](https://www.conventionalcommits.org/):

```text
<type>(<optional scope>): <imperative summary, ≤ 72 chars>

<optional body: what and why, wrapped at 72>

<optional footer: Refs #14>
```

Types: `feat`, `fix`, `docs`, `chore`, `refactor`, `test`, `perf`, `exp`, `data`, `ci`, `build`.
Scopes follow the planned modules: `ingest`, `features`, `labels`, `models`, `eval`, `export`, `api`, `web`, `docs`, `repo`.

PRs are squash-merged, so **the PR title becomes the commit on `main`**. Write it as a Conventional Commit too. Commits inside the branch can be messier, though small, clear commits make review easier.

See [`docs/development/git-workflow.md`](docs/development/git-workflow.md) for rebasing, conflicts and keeping branches fresh.

### Quality checks

Once tooling is in place (see [ADR-0002](docs/decisions/0002-language-and-tooling.md)), run these before pushing:

```bash
uv run ruff check . && uv run ruff format --check .
uv run pyright
uv run pytest
```

## 5. Pull requests and review

### Opening a PR

- Fill in the PR template completely. **"How was this verified?"** is the most important section.
- Link the issue with `Closes #N`.
- Keep PRs small, ideally under ~400 changed lines excluding generated files. Large PRs get split.
- Open as a **draft** if you want early feedback.
- Mark **AI assistance** in the template. It's not a judgment. It tells the reviewer where to look harder (see §6).

### Review requirements (enforced on `main`)

- **≥ 1 approving review** from someone other than the author
- **New commits dismiss stale approvals.** If you push after an approval, it needs re-approval.
- **The most recent push must be approved by someone other than its pusher**
- **All review conversations must be resolved** before merging
- **Squash merge only**, with linear history
- Once CI exists, required status checks must pass

### What reviewers check

1. **Correctness:** does it do what the issue asks? Are edge cases handled (missing VOD, null `vod_offset`, empty chat, muted segments)?
2. **Data integrity:** schemas, label definitions and metrics unchanged, or changed with an ADR. No leakage between train and test splits.
3. **Tests:** meaningful, not just present.
4. **Conventions:** matches [`docs/development/conventions.md`](docs/development/conventions.md) and the surrounding code.
5. **Docs:** updated where behavior changed.
6. **Safety:** no secrets, media or usernames. No new network calls or dependencies without justification.

Reviewers: be specific and kind. Mark suggestions as **blocking** or **nit**. Approve once blocking items are resolved. Aim to give a first review within **two working days**.

### Merging

The **author** merges after approval, using **Squash and merge**, and checks that the squash title is a good Conventional Commit. The branch is deleted automatically.

## 6. Working with AI coding agents

This project is built mostly with AI coding agents, and that's encouraged. The full playbook is in [`docs/agents/`](docs/agents/README.md). The essentials:

- **You own what you submit.** If an agent wrote it, you're still responsible for understanding it, verifying it and defending it in review.
- **Give agents good tasks.** A well-written issue matters more than a clever prompt. See [`docs/agents/writing-tasks.md`](docs/agents/writing-tasks.md).
- **Verify, don't trust.** Run the code. Check that tests actually exercise the change. Agents are confident about things that are wrong, especially APIs, file formats and metrics.
- **Review agent PRs harder** in these areas: invented API fields, silently swallowed errors, tests that assert the implementation instead of the behavior, unrequested scope creep, and fabricated numbers in docs.
- **Keep context files current.** If an agent made a mistake because the docs were missing something, fix the docs in the same PR. That's how the system improves.

## 7. Documentation changes

- Docs live in `docs/` as Markdown and are reviewed like code.
- Diagrams use [Mermaid](https://mermaid.js.org/) so they render on GitHub and diff cleanly.
- Label proposals clearly as **Proposed** and decisions as **Accepted** (see §8).
- Use relative links between docs. Don't link to branches that will be deleted.
- Style: short sentences, active voice, concrete examples, and no marketing language.

## 8. Decisions and ADRs

Significant technical decisions are recorded as **Architecture Decision Records** in [`docs/decisions/`](docs/decisions/README.md). Write or update an ADR when you:

- choose or replace a core technology (language, framework, storage, model family)
- change a data schema, label definition, train/test split policy or evaluation metric
- change a project-wide convention

Process: copy `docs/decisions/0000-template.md`, open a PR with status **Proposed**, discuss it in review, and set it to **Accepted** when the team agrees. Never edit the substance of an accepted ADR. Supersede it with a new one.

## 9. Getting help

- Questions about a task: comment on its issue.
- General questions: open an issue labeled `question`.
- Security issues: see [`SECURITY.md`](SECURITY.md). Don't file a public issue.
