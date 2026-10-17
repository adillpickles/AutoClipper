# ADR-0001: Record architecture decisions

| | |
|---|---|
| **Status** | Accepted |
| **Date** | 2026-10-09 |
| **Related** | [ADR index](README.md) |

## Context

The project is built by a small team using AI coding agents heavily. Agents start each session without memory, and team members join tasks midway. Decisions that live only in chat threads get lost, re-argued or accidentally reversed by an agent "improving" something.

## Decision

We record significant decisions as Architecture Decision Records in `docs/decisions/`, using the template in `0000-template.md` and the lifecycle in the [index](README.md). Decisions about schemas, label rules, evaluation metrics, core technologies and project-wide conventions require an ADR.

## Consequences

- **Positive:** a durable "why" for agents and newcomers. Fewer re-litigated debates. A clear signal (Proposed vs. Accepted) of what's safe to depend on.
- **Negative:** a small amount of writing overhead per significant decision.
- **Follow-ups:** `AGENTS.md` and `CONTRIBUTING.md` reference ADRs as the source of truth for decisions.
