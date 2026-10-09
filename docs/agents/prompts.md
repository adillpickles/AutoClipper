# Prompt Library

Reusable prompts for common tasks. They work with Claude Code, Codex and similar agents. Replace `<…>` placeholders. These prompts are deliberately short: the repo docs carry the context, so the prompt only needs to point at it.

## Start a task

```text
Work on issue #<N> in this repo.

1. Read AGENTS.md, docs/project/status.md, and the issue (gh issue view <N>).
2. Read the docs the issue links and the code you'll touch.
3. Before writing code, give me a short plan: files to change, tests to add, and anything
   in the issue that is ambiguous or conflicts with the docs. Wait for my OK.
4. Then implement on branch <type>/<N>-<slug>, following the task loop in AGENTS.md §7.
5. Finish by running lint/types/tests, updating docs/status as needed, and drafting the PR
   description using .github/pull_request_template.md. Do not push or open the PR yourself.
```

## Continue after review feedback

```text
PR #<N> got review comments (gh pr view <N> --comments). Address each blocking comment.
For each one, reply in your summary with: what you changed, or why you disagree.
Don't make unrelated changes. Re-run tests. If a comment reveals a docs gap, fix the doc too.
```

## Draft issues from the roadmap

```text
Read docs/product/roadmap.md milestone <M> and the docs it links.
Draft agent-ready issues for each unchecked item, following docs/agents/writing-tasks.md
and the Task template (.github/ISSUE_TEMPLATE/1-task.yml). Split anything larger than M.
Output them as Markdown for me to review. Do not create them on GitHub.
```

## Review a PR (as a second opinion)

```text
Review PR #<N> against docs/agents/reviewing-agent-prs.md and CONTRIBUTING.md.
Check contracts (docs/architecture/data-model.md, docs/ml/features.md), units/time handling,
leakage, test quality, and doc updates. List findings as blocking / nit, with file:line and
the doc section that applies. Don't modify code.
```

## Investigate (research spike)

```text
Spike for issue #<N>: <question>. Time box: <T>.
Investigate using official docs and small throwaway scripts outside src/ (scripts/spikes/).
Write findings as an issue comment draft: answer, evidence, recommendation, and which docs
or ADRs should change. Do not change src/ or commit data.
```

## Update docs after a change

```text
The change in <branch/PR> modified <behavior>. Find every doc that describes it
(AGENTS.md, docs/**, README.md) and update them to match the code. Keep the existing tone and
structure. List each file changed and why.
```

## Sync project status

```text
Compare docs/project/status.md and docs/product/roadmap.md against the code on main and merged
PRs (gh pr list --state merged --limit 30). Propose edits so both accurately reflect what exists.
Do not mark anything done that isn't merged and tested.
```
