# Contributing to AutoClipper

Pick an issue, build it on a branch, and describe the result in a PR. Setup commands are in [setup](docs/development/setup.md); task-specific reading is in [AGENTS.md](AGENTS.md).

## 1. Ground rules

- Changes reach protected `main` through squash-merged PRs. To keep development fast, PRs don't need a teammate's approval: check your own work, yourself or with AI, before you merge.
- Keep media, datasets, model weights and credentials out of git. Small synthetic fixtures are fine ([data handling](docs/data/data-governance.md)).
- Update affected docs alongside the implementation.

## 2. Pick up an issue

Find work in [GitHub issues](https://github.com/adillpickles/AutoClipper/issues) or the [roadmap](docs/product/roadmap.md). Choose an unassigned issue whose **Depends on** items are merged, then assign yourself. Category labels show the area of work; `stretch` comes after the MVP.

If a requirement is unclear, choose a sensible default and explain it in the PR. Ask when an essential input is missing or the action falls outside the request.

## 3. Create a branch

```bash
git switch main
git pull --ff-only
git switch -c feat/23-chat-features
```

Use `<type>/<issue#>-<short-slug>`. Common types: `feat`, `fix`, `docs`, `chore`, `refactor`, `exp` and `data`. Check for overlapping work before editing shared files ([parallel work](docs/agents/parallel-work.md)).

## 4. Do the work

Follow [conventions](docs/development/conventions.md), test the changed behavior, and keep required wiring and doc updates with the feature. The normal Python checks are:

```bash
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pytest
```

Frontend checks are in [web/README.md](web/README.md). Run the checks relevant to the change and note in the PR anything that couldn't run.

## 5. Commit

Use a short [Conventional Commit](https://www.conventionalcommits.org/) message, such as `feat(features): add chat message rate` or `fix(labels): handle missing clip offsets`. Branch commits are squash-merged; the PR title becomes the commit title on `main`.

Authorship conventions are in [AGENTS.md § 11](AGENTS.md#11-notes-for-specific-agents).

## 6. Open a pull request

Push the branch and open a PR against `main`. Agents do this when requested; updating an existing PR includes pushing its commits.

```bash
git push -u origin <branch>
gh pr create --base main
```

Use the [PR template](.github/pull_request_template.md): what changed, why, how it was checked, and any remaining uncertainty. Include `Closes #N` for a completed issue, or `Part of #N` for partial work. Copy relevant category labels and add screenshots for visual changes when available. A draft is useful for work in progress.

## 7. Review

A second look helps on big changes. Check the behavior against the issue, read the diff and tests, and compare names, units and schemas with the documented contracts. The [review guide](docs/agents/reviewing-agent-prs.md) lists common mistakes in this pipeline.

Use precise comments and distinguish bugs from suggestions. Address feedback on the same branch.

## 8. Merge

Merge when the work is ready and CI is green. Use **Squash and merge**, then remove the merged branch and update your local `main`. Current repository settings are in [git workflow](docs/development/git-workflow.md).

## 9. Filing new issues

Use **Task** for implementation, data collection, experiments or investigations, and **Bug report** for incorrect behavior. Free-form issues are also available. Use plain titles and the [issue-writing guide](docs/agents/writing-tasks.md): Goal, What to build, Done when, Depends on, Not included and How to check.

Experiments include the hypothesis, baseline and metric; data tasks include the inputs and expected tables. Investigations end with a concrete finding or implementation recommendation.

## 10. Labels

Usually add one or two categories: `frontend`, `backend`, `ingestion`, `data`, `modeling`, `export` or `tooling`. Add a type when useful (`bug`, `docs`, `spike`, `experiment`), and `stretch` for work after the MVP. `blocked` marks an actual outside dependency. See [labels](docs/project/labels.md).

## 11. Working with AI agents

Give the agent the issue and let it follow `AGENTS.md`. It makes engineering choices, implements, tests and commits; it publishes branches and PRs when requested. Review the resulting behavior and verification notes at a level appropriate to the change. If a doc caused confusion, update it with the fix.

## 12. Docs and decisions

Use the existing doc for the subject, relative links and concrete examples. Explain choices in the PR and update affected contracts with the implementation. The [technical choices](docs/decisions/README.md) explain the main defaults.

## 13. Getting help

Use the issue for task-specific context and team chat for coordination. Missing technical details are usually an engineering choice: pick a practical default, document it, and continue.
