# ADR-0008: Trunk-based development, reviewed PRs, squash merge

| | |
|---|---|
| **Status** | Accepted |
| **Date** | 2026-10-09 |
| **Related** | [Git workflow](../development/git-workflow.md), [CONTRIBUTING](../../CONTRIBUTING.md) |

## Context

Several people, and several AI agents, will change the codebase in parallel. Agent-written code needs a human check before it lands. History on `main` should be easy to read and bisect.

## Decision

- `main` is the only long-lived branch. Work happens on short-lived branches named `<type>/<issue#>-<slug>`.
- `main` is protected by a repository ruleset: no direct pushes, no force pushes, no deletion. PRs need **one approving review** from someone other than the author. Stale approvals are dismissed on new commits. The last push must be approved by someone other than its pusher. Review threads must be resolved. History stays linear.
- **Squash merge only.** The PR title (a Conventional Commit) becomes the commit on `main`. Head branches are deleted after merge.
- When CI exists, its checks become required status checks.

## Consequences

- **Positive:** every change on `main` has been seen by a second person. Clean, linear, one-commit-per-PR history.
- **Negative:** no one can merge alone, including the repository owner. A teammate must be available to review. Small fixes still need a PR.
- **Follow-ups:** add required status checks once CI exists (roadmap M0).

## Revisit when

The team size or cadence changes enough that review becomes a bottleneck.
