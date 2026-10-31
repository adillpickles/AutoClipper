# ADR-0010: Agent workflow, publishing and optional second-opinion review

| | |
|---|---|
| **Status** | Accepted |
| **Date** | 2026-10-16 (first drafted 2026-10-13 in the same setup PR) |
| **Related** | [AGENTS.md §7](../../AGENTS.md#7-how-to-do-a-task), [Review loop](../agents/review-loop.md), [CONTRIBUTING](../../CONTRIBUTING.md), [ADR-0008](0008-trunk-based-development-squash-merge.md) |

## Context

Every teammate builds with AI coding agents, using different tools. Earlier drafts gave agents conflicting instructions about whether to open PRs on their own, and made a multi-step audit/repair loop mandatory for any "substantial" change. That made the instructions hard to follow and slowed everyone down.

## Decision

- **One task loop for all agents** (AGENTS.md §7): read → plan when useful → branch → implement and test → update docs → self-check → commit. Push or open/update a PR when the user requests publication; updating an existing PR includes pushing the needed commits. The human handles merging.
- **Agents make the engineering calls.** When an issue leaves a technical choice open, the agent picks the option that best serves the product and explains it in the PR instead of stopping to ask.
- **Implementation defaults can evolve.** Local execution, file storage and the initial worker are starting points. Shared collection/storage and hosted deployment are valid choices when they serve the task. Update the relevant contracts in the same change and keep working.
- **The human running the agent owns the PR.** They decide when it merges; an approving review from a teammate is welcome but not required ([ADR-0008](0008-trunk-based-development-squash-merge.md)).
- **A second-opinion AI review is optional.** For large or risky changes, contributors are encouraged to have a different model audit the branch and to verify each finding in a fresh session before opening the PR ([review loop](../agents/review-loop.md)). It's a recommendation, not a merge requirement.
- **No AI attribution** in commits, PRs or files. No AI-assistance disclosure is required in PRs: everyone uses AI, and the author owns what they merge regardless.

## Consequences

- **Positive:** short, consistent instructions for every agent; work doesn't stall on small decisions; teammates choose how much extra review their change needs.
- **Negative:** whether a big change gets an AI audit depends on the author's judgment. CI and tests are the safety net.

## Revisit when

Review comments keep catching the same agent mistakes. Then consider making the review loop required for specific areas (e.g. labels, evaluation).
