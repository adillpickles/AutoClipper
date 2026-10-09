# Team Process

How the team plans, tracks and communicates. Lightweight on purpose: four part-time contributors, mostly asynchronous.

| | |
|---|---|
| **Status** | Proposal. Adjust it to what the team actually does. |
| **Last updated** | 2026-10-09 |

## Roles

Roles describe responsibilities, not people. One person can hold several, and roles can rotate.

| Role | Responsibilities |
|---|---|
| **Team manager** (course requirement) | Point of contact with program staff. Keeps the roadmap and status current. Runs triage. |
| **Repo admin** | GitHub settings, labels, rulesets, secrets policy |
| **Area owners** (proposal) | One go-to person per area (ingestion, data/labels, modeling/eval, export, app) who reviews or delegates PRs in that area. Not a gate: any teammate can approve. |
| **Reviewer** | Everyone. Reviews are shared. |

## Planning

- Work is planned per **milestone** ([roadmap](../product/roadmap.md)). Each milestone gets a GitHub milestone and a set of issues.
- **Weekly async check-in** (proposal), in the team chat: what I merged, what I'm on, what's blocking me.
- At each milestone boundary: update [status](status.md), review [risks](risks.md) and [open questions](open-questions.md), and ratify any ADRs that are ready.

## Tracking

- **Issues** are the unit of work. One issue per PR, where possible.
- Optional: a GitHub Project board with columns *Backlog → Ready (`agent-ready`) → In progress → In review → Done*.
- Discussion about a task stays **on the issue or PR**, not only in chat, so agents and future teammates can see it.

## Communication norms

- Decisions made in chat get written down: in an ADR, in [open questions](open-questions.md) (Resolved), or on the issue.
- Mention people for reviews. Don't expect them to notice silently.
- Assume good intent. Critique code, not people. See the [Code of Conduct](../../CODE_OF_CONDUCT.md).

## Course deliverables

The course requires a team project design document (PRD). [`docs/product/prd.md`](../product/prd.md) is written to serve as its source, so keep the two in sync if the course version lives elsewhere. Deliverable dates are tracked in OQ-13 until they're known.
