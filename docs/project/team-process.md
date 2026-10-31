# Team Process

How the team plans, tracks and communicates. Lightweight on purpose: four part-time students, mostly asynchronous.

| | |
|---|---|
| **Status** | Living. Adjust it to what the team actually does. |
| **Last updated** | 2026-10-31 |

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
| **Area owners** | One go-to person per area (ingestion, data/labels, modeling/eval, export, app) who knows that code best. Not a gate. |
| **Reviewer** | Optional. Ask for a review on large or risky PRs; approvals aren't required to merge. |

## Tracking

- **Issues** are the unit of work: one issue → one PR. Every issue has an owner (assignee) once someone starts it.
- **Pick an issue** whose **Depends on** items are merged, in an area you're comfortable with (the category labels: `frontend`, `backend`, `ingestion`, `data`, `modeling`, `export`, `tooling`). Leave `stretch` issues until the MVP works.
- **Project board** (GitHub Projects): *Backlog → Ready → In progress → In review → Done*.
- Work is grouped by **milestone** ([roadmap](../product/roadmap.md)). No priority or size labels; the roadmap's weeks and each issue's **Depends on** list set the sequence.
- At each milestone boundary: update [status](status.md) and review [risks](risks.md).

## Norms

- **One owner per task.**
- **Vibe coding is fine; you own what you merge.** Let your agent make the engineering calls, check that CI is green and that the PR says what changed and why.
- **Ask the "dumb" questions early.** We're at different ML starting points, and that's the point.
- **Scope creep:** nothing from the PRD's Should/Could rows until the MVP runs end to end.
- **Write decisions down.** A technical choice goes in the PR description; one that changes how the project works goes in an [ADR](../decisions/README.md).
- **Conflicts:** talk it out as a team first, then go to a TA. See the [Code of Conduct](../../CODE_OF_CONDUCT.md).

## Course deliverables

The [PRD](../product/prd.md) is the source of truth for scope; when it changes, update the course's project design doc from it. Key dates are in the [roadmap](../product/roadmap.md).
