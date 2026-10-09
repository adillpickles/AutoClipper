# CLAUDE.md

Claude Code reads this file automatically. The canonical instructions for all agents live in `AGENTS.md`, imported below. Keep this file limited to Claude-specific additions so the two never drift apart.

@AGENTS.md

## Claude Code specifics

- **Start of task:** read `docs/project/status.md` and the linked issue before planning. Use plan mode for anything that touches more than one module or changes a schema.
- **Subagents:** fine for read-only research such as exploring the codebase or summarizing docs. Implementation should happen in the main session so the diff stays coherent.
- **Worktrees:** when running several sessions in parallel, give each one its own worktree and branch (see `docs/agents/parallel-work.md`).
- **Memory:** don't store project facts in personal memory if they belong in the repo. If you learn something that every future session needs, propose adding it to `AGENTS.md` or the relevant doc in your PR.
- **Commits and PRs:** follow `docs/development/git-workflow.md`. Don't add `Co-Authored-By` trailers or "Generated with" footers. Don't push or open PRs unless the human asks you to.
