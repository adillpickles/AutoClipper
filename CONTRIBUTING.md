# Contributing to AutoClipper

How work moves from an issue to a merged pull request. It applies to everyone, whether you write the code yourself or hand the issue to an AI agent. Agents also follow [`AGENTS.md`](AGENTS.md).

**The short version:** pick an issue → assign yourself → branch → build and test → open a PR that says `Closes #N` → get one approval → squash and merge.

## Contents

1. [Ground rules](#1-ground-rules)
2. [Pick up an issue](#2-pick-up-an-issue)
3. [Create a branch](#3-create-a-branch)
4. [Do the work](#4-do-the-work)
5. [Commit](#5-commit)
6. [Open a pull request](#6-open-a-pull-request)
7. [Review](#7-review)
8. [Merge](#8-merge)
9. [Filing new issues](#9-filing-new-issues)
10. [Labels](#10-labels)
11. [Working with AI agents](#11-working-with-ai-agents)
12. [Docs and decisions](#12-docs-and-decisions)
13. [Getting help](#13-getting-help)

---

## 1. Ground rules

- **One issue → one branch → one PR.**
- **`main` is protected.** No direct pushes. Every change lands through a PR with **one approving review** from someone other than the author, and is **squash-merged**.
- **You own what you merge.** If an AI wrote it, you still need to understand it and be able to explain it.
- **No data, media, secrets or real chat usernames in git, ever.** See [data governance](docs/data/data-governance.md).
- **Docs are part of the change.** If behavior changes, the docs change in the same PR.
- **Be kind.** See the [Code of Conduct](CODE_OF_CONDUCT.md).

## 2. Pick up an issue

1. **Find one.** Open the repo's **Issues** tab. Good starting filters:
   - `agent-ready`: fully specified, ready to hand to you or an AI agent
   - `good first issue`: small and well scoped
   - A category you want to work on, e.g. `frontend` or `modeling`

   From the terminal: `gh issue list --label agent-ready`. The [backlog](docs/project/backlog.md) lists the planned issues in dependency order.
2. **Make sure you can work on it independently.**
   - Nobody else is assigned.
   - Everything under **Depends on** in the issue is already merged. If not, pick another issue or ask the person working on the dependency.
   - It doesn't have the `blocked` or `needs-decision` label.
   - It won't edit the same files as someone else's open PR (check its scope / likely files).
3. **Claim it.** Assign yourself: issue page → right sidebar → **Assignees** → yourself. Or run `gh issue edit <N> --add-assignee @me`. If you can't assign, comment "taking this".
4. **Unclear?** Ask in a comment on the issue before you start, not halfway through.

## 3. Create a branch

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
| `chore/` | Tooling, CI, dependencies, repo housekeeping |
| `refactor/` | Restructuring with no behavior change |
| `exp/` | ML experiments (code + config + results summary) |
| `data/` | Data collection/labeling scripts or dataset definitions |

Example: `feat/14-audio-loudness-features`.

## 4. Do the work

- Set up your environment with [setup](docs/development/setup.md).
- Follow [conventions](docs/development/conventions.md) and add tests ([testing](docs/development/testing.md)).
- Keep it small: aim for **under ~400 changed lines**. If it's growing past that, split the issue.
- Before pushing, run the checks (once tooling exists):

  ```bash
  uv run ruff check . && uv run ruff format --check .
  uv run pyright
  uv run pytest
  ```

## 5. Commit

Use short [Conventional Commits](https://www.conventionalcommits.org/):

```text
<type>(<optional scope>): <imperative summary, ≤ 72 chars>
```

- **Types:** `feat`, `fix`, `docs`, `chore`, `refactor`, `test`, `perf`, `exp`, `data`, `ci`, `build`
- **Scopes** follow the modules: `ingest`, `features`, `labels`, `models`, `eval`, `export`, `api`, `web`, `docs`, `repo`
- **Examples:** `feat(features): add chat message rate`, `fix(labels): skip clips with null offset`

Commits inside your branch can be messy; they get squashed into one commit when merged.

## 6. Open a pull request

1. **Push your branch:** `git push -u origin <branch>`.
2. **Open the PR** against `main`, either on GitHub (the yellow "Compare & pull request" banner) or with `gh pr create --base main`. The [PR template](.github/pull_request_template.md) fills in automatically.
3. **Title:** a Conventional Commit, because it becomes the commit message on `main`.
   - ✅ `feat(export): burn captions into vertical clips`
   - ❌ `Update stuff`, `Fixed issue 14`
4. **Description:** fill in the template. Keep it short:
   - **Summary:** 1–3 sentences on what and why.
   - **Closes #N:** see the next step.
   - **Changes:** a bullet list of what you changed. Mention anything surprising or uncertain.
   - **Screenshots:** for anything visual (see step 7).
   - **Checklist:** tick what's true; explain anything you couldn't tick.
5. **Link the issue.** Write `Closes #14` in the description (or `Fixes #14`). GitHub links the PR to the issue and closes the issue automatically when the PR merges. For several issues, use one line each (`Closes #14`, `Closes #15`). If the PR only does part of an issue, write `Part of #14` instead so the issue stays open.
6. **Add labels.** Copy the issue's **category** labels onto the PR (e.g. `export`). PR page → right sidebar → **Labels**, or `gh pr edit --add-label export`. See [§10](#10-labels).
7. **Screenshots for UI or visual work.** If your change affects anything you can see (web UI, exported video framing, captions, charts), add a screenshot. If it changes something that already existed, add **before and after**. Drag and drop the image into the description box on GitHub. A side-by-side table works well:

   ```markdown
   | Before | After |
   |---|---|
   | ![before](paste-image-here) | ![after](paste-image-here) |
   ```

   For video exports, a screenshot of a frame or a short screen recording is enough. Never attach real VOD files.
8. **Request a reviewer** in the right sidebar (**Reviewers**), or `gh pr edit --add-reviewer <username>`.
9. **Not ready yet?** Open it as a **draft** to get early feedback, then click "Ready for review" when done.

## 7. Review

**For the author:** respond to every comment. Push fixes as new commits on the same branch, then re-request review. Pushing new commits after an approval resets it, so the reviewer needs to approve again.

**For the reviewer:** aim to give a first review within **two days**. Check:

1. **Does it do what the issue asks?** Edge cases handled (missing VOD, null `vod_offset`, empty chat, muted segments)?
2. **Tests:** do they actually test the behavior?
3. **Contracts:** column names, units and time handling match the [data model](docs/architecture/data-model.md) and [features](docs/ml/features.md). No train/test leakage.
4. **Docs:** updated where behavior changed.
5. **Safety:** no secrets, media or usernames. New dependencies explained.
6. **Screenshots** present for UI changes.

Mark each comment **blocking** or **nit**. Approve once the blocking items are fixed. [Reviewing agent PRs](docs/agents/reviewing-agent-prs.md) lists the mistakes AI agents typically make.

## 8. Merge

A PR can merge when it has **one approval**, **all conversations are resolved**, and (once CI exists) **checks are green**.

1. The **author** merges: PR page → **Squash and merge**.
2. Check that the commit title is still your Conventional Commit title, then confirm.
3. GitHub deletes the branch and closes the linked issue automatically.
4. Clean up locally:

   ```bash
   git switch main && git pull
   git branch -d <your-branch>
   ```

From the terminal: `gh pr merge --squash --delete-branch`.

## 9. Filing new issues

Use the templates (Issues → **New issue**):

| Template | Use for |
|---|---|
| **Task** | A unit of implementation work with acceptance criteria. The default. |
| **Bug report** | Something behaves incorrectly |
| **Experiment** | An ML experiment with a hypothesis, metric and baseline |
| **Research spike** | A time-boxed investigation that ends in a written finding |
| **Data task** | Data collection, labeling or dataset changes |

A good issue is **small** (one PR), **testable** (acceptance criteria you can check), **self-contained** (links the docs someone needs) and lists what it **depends on**. See [writing tasks](docs/agents/writing-tasks.md).

## 10. Labels

Labels show what kind of work an issue is. Add what's useful, usually one or two **categories** (`frontend`, `backend`, `ingestion`, `data`, `modeling`, `export`, `tooling`) plus a **type** if it isn't plain feature work (`bug`, `docs`, `spike`, `experiment`, `decision`). Readiness labels: `agent-ready` (ready to pick up), `needs-decision` (waits on a team decision), `blocked` (stuck on something external). There are no priority or size labels.

**How to add one:** issue or PR page → right sidebar → **Labels** → tick the labels. Or `gh issue edit <N> --add-label frontend,feature` (`gh pr edit` works the same way for PRs).

Full list and meanings: [labels](docs/project/labels.md).

## 11. Working with AI agents

Most of this project is built with AI coding agents. The basics:

- **Hand the agent the issue.** Starter prompts are in [prompts](docs/agents/prompts.md). The agent reads `AGENTS.md` automatically and finds the rest from there.
- **Verify, don't trust.** Run the code. Check that the tests actually test the change. Agents are confidently wrong about APIs, file formats and metrics.
- **You open the PR.** Agents don't push or open PRs unless you tell them to, and never merge on their own.
- **Fix the docs when the agent was misled.** If an agent made a mistake because a doc was missing or unclear, fix the doc in the same PR.
- **Optional second opinion.** For big or risky changes, consider having a different model audit the branch before you open the PR. That workflow is in [review loop](docs/agents/review-loop.md). It's a recommendation, not a requirement.

## 12. Docs and decisions

- Docs live in `docs/` as Markdown and are reviewed like code. Use relative links and [Mermaid](https://mermaid.js.org/) for diagrams.
- Style: short sentences, active voice, concrete examples.
- **Significant technical decisions** get an ADR in [`docs/decisions/`](docs/decisions/README.md): choosing or replacing a core technology, or changing a data schema, label definition, split policy or evaluation metric. Copy the template, open a PR with status **Proposed**, and mark it **Accepted** once the team agrees. To reverse an accepted ADR, write a new one that supersedes it.

## 13. Getting help

- Questions about a task: comment on its issue.
- General questions: the team Discord, or an issue labeled `question`.
- Security issues: see [`SECURITY.md`](SECURITY.md). Don't file a public issue.
