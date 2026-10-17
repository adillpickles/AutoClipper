# Prompt Library

Copy-paste prompts for common tasks. They work with Claude Code, Codex, Cursor and similar agents. Replace `<…>` placeholders. The prompts are short on purpose: the repo docs carry the context, so the prompt only needs to point at them.

Prompts for the optional audit → verify loop are in [review-loop.md](review-loop.md).

## Start a task

```text
Work on issue #<N> in this repo.

1. Read AGENTS.md, docs/project/status.md and the issue (gh issue view <N>).
2. Check the issue's "Depends on" list. If anything there isn't merged, stop and tell me.
3. Read the docs the issue links and the code you'll touch.
4. If the task is more than a small, well-specified change, give me a short plan
   (files, approach, tests, open questions) and wait for my OK.
5. Implement on branch <type>/<N>-<slug>. Add tests, update affected docs and status.md.
6. Run lint, type check and tests. Read your full diff against main.
7. Commit locally with short Conventional Commit messages. Don't push.
8. Tell me what changed, how you verified it and what's uncertain, and draft the PR
   description using .github/pull_request_template.md.
```

## Open the PR (after you've checked the work)

```text
Push this branch and open a PR against main using .github/pull_request_template.md.
Title: a Conventional Commit. Include "Closes #<N>". Add the issue's category labels.
Don't merge.
```

## Address review comments

```text
PR #<N> got review comments (gh pr view <N> --comments).
Check each comment against the actual code before changing anything. Fix the valid ones;
for any you disagree with, explain why. Don't make unrelated changes. Re-run tests, commit
locally, and summarize what you changed and what you pushed back on. If a comment reveals
a docs gap, fix the doc too. Draft replies for me; don't post them.
```

## Add issue drafts to the backlog

```text
Read docs/project/backlog.md (format and existing drafts), docs/agents/writing-tasks.md and
the docs relevant to <new work>. Add drafts for it in the same format: exact title, labels from
docs/project/labels.md, goal, scope, docs, acceptance criteria, "Depends on" (exact existing
titles or None), parallel work and verification. Place them after their dependencies, keep each
to one reviewable PR, and mark anything waiting on a decision `needs-decision`. Don't create
GitHub issues.
```

## Publish backlog drafts as GitHub issues (owner)

```text
Create GitHub issues for the backlog drafts in docs/project/backlog.md, in index order,
from <first title> through <last title>. Use each heading as the exact title and the matching
issue template's fields; replace each "Depends on" title with the created issue's #number.
Apply the suggested labels (they must already exist on GitHub). List what you created; don't
create anything else.
```

## Review someone's PR

```text
Review PR #<N> against CONTRIBUTING.md § 7 and docs/agents/reviewing-agent-prs.md.
Look at the complete diff (gh pr diff <N>), not just the description. Check the issue's
acceptance criteria, contracts (docs/architecture/data-model.md, docs/ml/features.md),
units/time handling, leakage, test quality and doc updates. List findings as blocking / nit
with file:line. Don't modify code or post comments.
```

## Investigate (research spike)

```text
Spike for issue #<N>: <question>. Time box: <T>.
Investigate using official docs and small throwaway scripts outside src/ (scripts/spikes/).
Write findings as an issue comment draft: answer, evidence, recommendation, and which docs
or ADRs should change. Don't change src/ or commit data.
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
Don't mark anything done that isn't merged and tested.
```
