# ADR-0001: Record architecture decisions

| | |
|---|---|
| **Status** | Accepted |
| **Date** | 2026-10-09 |
| **Related** | [ADR index](README.md) |

## Context

The project is built by a small team using AI coding agents heavily. Agents start each session without memory, and team members join tasks midway. Decisions that live only in chat threads get lost, re-argued or accidentally reversed by an agent "improving" something.

## Decision

We record significant decisions about schemas, label rules, evaluation metrics, core technologies and project-wide conventions in `docs/decisions/`, using the template in `0000-template.md` and the lifecycle in the [index](README.md). Include the record with the implementation. Smaller changes need an explanation in the PR and updates to affected contracts, without a separate decision process.

## Consequences

- **Positive:** a durable "why" for agents and newcomers. Current choices and later replacements stay understandable without reopening the same questions.
- **Negative:** a small amount of writing overhead per significant decision.
- **Follow-ups:** `AGENTS.md` and `CONTRIBUTING.md` reference ADRs as the source of truth for decisions.
