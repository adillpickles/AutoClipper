# CLAUDE.md

Claude Code reads this file automatically. The canonical instructions for all agents live in `AGENTS.md`, imported below. Keep this file limited to Claude-specific additions so the two never drift apart.

@AGENTS.md

## Claude Code specifics

- **Planning:** use Claude's plan mode where it helps fulfill the shared planning rules in `AGENTS.md §7`.
- **Subagents:** an independent reviewer context can audit completed work. Give it the actual base/head, full diff and prior findings; follow shared isolation guidance.
- **Worktrees:** when running several sessions in parallel, give each one its own worktree and branch (see `docs/agents/parallel-work.md`).
- **Memory:** don't store project facts in personal memory if they belong in the repo. If you learn something that every future session needs, propose adding it to `AGENTS.md` or the relevant doc in your PR.
- Shared authorship, publication and merge rules live in `AGENTS.md` and `docs/development/git-workflow.md`; do not duplicate or override them here.
