# CLAUDE.md

Claude Code reads this file automatically. The instructions for all agents live in `AGENTS.md`, imported below. Keep this file to Claude-specific additions.

@AGENTS.md

## Claude Code specifics

- **Be decisive.** This is a fast-moving student project. Make reasonable calls, build, and explain them in the PR rather than stopping to ask.
- **Planning:** use plan mode for large tasks if it helps; small and medium issues can go straight to implementation.
- **Subagents and worktrees:** use them when they speed things up. Give each parallel session its own worktree and branch (`docs/agents/parallel-work.md`).
- **Memory:** project facts belong in the repo docs, not personal memory.
- Authorship and attribution rules are in `AGENTS.md` §11.
