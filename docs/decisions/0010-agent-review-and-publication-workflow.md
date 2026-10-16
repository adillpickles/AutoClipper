# ADR-0010: Agent workflow, publishing and optional second-opinion review

| | |
|---|---|
| **Status** | Accepted |
| **Date** | 2026-10-16 (first drafted 2026-10-13 in the same setup PR) |
| **Related** | [AGENTS.md §7](../../AGENTS.md#7-how-to-do-a-task), [Review loop](../agents/review-loop.md), [CONTRIBUTING](../../CONTRIBUTING.md), [ADR-0008](0008-trunk-based-development-squash-merge.md) |

## Context

Every teammate builds with AI coding agents, using different tools. Earlier drafts gave agents conflicting instructions about whether to open PRs on their own, and then made a multi-step audit/repair loop mandatory for any "substantial" change. That loop is one teammate's personal practice; making it compulsory adds process the team didn't agree to and makes the instructions hard to follow.

## Decision

- **One task loop for all agents** (AGENTS.md §7): read → plan when the task isn't small → branch → implement and test → update docs → self-check → commit locally → hand off to the human.
- **Agents don't publish on their own.** They push, open or update PRs, or comment on GitHub only when their human asks. They never merge without a human's go-ahead.
- **The required review is human.** One teammate approval per PR, as in ADR-0008.
- **A second-opinion AI review is optional.** For large or risky changes, contributors are encouraged to have a different model audit the branch and to verify each finding in a fresh session before opening the PR ([review loop](../agents/review-loop.md)). It's a recommendation, not a merge requirement.
- **No AI attribution** in commits, PRs or files. No AI-assistance disclosure is required in PRs: everyone uses AI, and the author owns what they merge regardless.

## Consequences

- **Positive:** short, consistent instructions for every agent; no surprise pushes; teammates choose how much extra review their change needs.
- **Negative:** whether a big change gets an AI audit depends on the author's judgment. Human review stays the safety net.

## Revisit when

Review comments keep catching the same agent mistakes. Then consider making the review loop required for specific areas (e.g. labels, evaluation).
