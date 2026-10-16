# Team Process

How the team plans, tracks and communicates. Lightweight on purpose: four part-time students, mostly asynchronous.

| | |
|---|---|
| **Status** | Living. Based on the team's project design doc; adjust it to what the team actually does. |
| **Last updated** | 2026-10-16 |

## Channels

| Channel | Use for |
|---|---|
| **GitHub** (issues, PRs, project board) | All task discussion and decisions about code, so agents and teammates can see them |
| **Official course Slack** | Decisions, blockers and weekly updates, so TAs stay in the loop |
| **Discord** | Day-to-day chat, quick pings, calls and screen-shares. Best place for urgent blockers. |

Agreed response window: **within 24 hours**. If you'll be offline, traveling or buried in exams or work, tell the team in advance.

## Weekly sync

**Fridays after class, ~30 minutes on Discord.** Each person: what I did, what's next, what's blocking me. Walk the board, replan the week, and note any decisions in the docs.

## Roles

Roles are loose starting points; everyone works across areas and owns at least one ML task.

| Role | Responsibilities |
|---|---|
| **Team manager** (course requirement) | Point of contact with program staff. Keeps the roadmap, status and issue backlog current. Runs triage. Repo admin (GitHub settings, labels, rulesets). |
| **Area owners** | One go-to person per area (ingestion, data/labels, modeling/eval, export, app) who reviews or delegates PRs in that area. Not a gate: any teammate can approve. |
| **Reviewer** | Everyone. Reviews are shared; aim for a first review within two days. |

## Tracking

- **Issues** are the unit of work: one issue → one PR. Every issue has an owner (assignee) once someone starts it.
- **Project board** (GitHub Projects): *Backlog → Ready (`agent-ready`) → In progress → In review → Done*.
- Work is grouped by **milestone** ([roadmap](../product/roadmap.md)).
- At each milestone boundary: update [status](status.md), review [risks](risks.md) and [open questions](open-questions.md).

## Norms

- **One owner per task.**
- **AI tools are fine, but you own what you merge.** You can explain every line of your PR, and a teammate reviews it before merge.
- **Ask the "dumb" questions early.** We're at different ML starting points, and that's the point.
- **Scope creep:** nothing from the PRD's Should/Could rows until the MVP runs end to end.
- **Write decisions down.** Decisions made in chat go into an ADR, [open questions](open-questions.md) (Resolved) or the issue.
- **Conflicts:** talk it out as a team first, then go to a TA. See the [Code of Conduct](../../CODE_OF_CONDUCT.md).

## Course deliverables

The course's project design doc is the source for the [PRD](../product/prd.md); keep the two in sync. Key dates are in the [roadmap](../product/roadmap.md).
