# ADR-0008: Trunk-based development, PRs with green CI, squash merge

| | |
|---|---|
| **Status** | Accepted |
| **Date** | 2026-10-09 (revised 2026-10-21: approvals optional) |
| **Related** | [Git workflow](../development/git-workflow.md), [CONTRIBUTING](../../CONTRIBUTING.md) |

## Context

Several people, and several AI agents, will change the codebase in parallel. Most code is written with AI agents, so automated checks carry most of the load and every change still goes through a PR. History on `main` should be easy to read and bisect.

## Decision

- `main` is the only long-lived branch. Work happens on short-lived branches named `<type>/<issue#>-<slug>`.
- `main` is protected by a repository ruleset: no direct pushes, no force pushes, no deletion. Every change lands through a PR. An approving review is welcome but **not required**, so nobody waits on a reviewer. History stays linear.
- **Squash merge only.** The PR title (a Conventional Commit) becomes the commit on `main`. Head branches are deleted after merge.
- When CI exists, its checks become required status checks.

## Consequences

- **Positive:** nobody is blocked waiting for review. Clean, linear, one-commit-per-PR history.
- **Negative:** CI and the author's self-check are the main safety net, so tests matter. Small fixes still need a PR.
- **Follow-ups:** make the CI checks required on `main` (#3).

## Revisit when

Bad merges start breaking `main`; then require one approval for the affected areas.
