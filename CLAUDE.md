# CLAUDE.md

Claude Code reads this file automatically. The canonical instructions for all agents live in `AGENTS.md`, imported below. Keep this file limited to Claude-specific additions so the two never drift apart.

@AGENTS.md

## Claude Code specifics

- **Planning:** use plan mode for the "Plan" step in `AGENTS.md §7` when the task is bigger than a small fix.
- **Subagents:** only when your human asks for one. For the optional second-opinion review, follow `docs/agents/review-loop.md`.
- **Worktrees:** when running several sessions in parallel, give each one its own worktree and branch (see `docs/agents/parallel-work.md`).
- **Memory:** don't store project facts in personal memory if they belong in the repo. If you learn something that every future session needs, propose adding it to `AGENTS.md` or the relevant doc in your PR.
- Authorship, pushing/PR and merge rules live in `AGENTS.md` (§6 rule 9, §11); don't duplicate or override them here.
