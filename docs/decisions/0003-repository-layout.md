# ADR-0003: Single repository, `src/` layout

| | |
|---|---|
| **Status** | Accepted (2026-10-16) |
| **Date** | 2026-10-09 |
| **Related** | [Conventions § Repository layout](../development/conventions.md#1-repository-layout-planned), [Architecture](../architecture/overview.md) |

## Context

The project has a Python pipeline/ML package, a CLI, a web API, a web frontend, experiments and docs. A small team with AI agents benefits from having all context in one place: an agent can read the docs, the contracts and the code it touches in a single checkout.

## Decision

One repository containing:

- `src/autoclipper/`: the Python package (pipeline, models, eval, export, API), in the `src` layout.
- `tests/`: mirrors the package structure.
- `web/`: the Next.js frontend ([ADR-0007](0007-web-application-stack.md)).
- `experiments/`, `configs/`, `notebooks/`, `scripts/`, `docs/`.
- Git-ignored `data/` and `artifacts/`.

AutoClipper is a **standalone** project. It doesn't depend on any team member's private or personal codebase. Reusing outside code requires a compatible license and a note in the PR.

## Options considered

| Option | Pros | Cons |
|---|---|---|
| **Single repo** (chosen) | All context in one place for agents. Atomic changes across API + UI. One set of issues and labels. | Mixed Python/Node tooling in one tree |
| Separate repos for ML and app | Clean separation | Cross-repo changes, duplicated process, agents lose context |
| Flat layout (no `src/`) | Slightly simpler | Tests can accidentally import the working tree instead of the installed package |

## Consequences

- **Positive:** one place for docs, contracts, issues and CI.
- **Negative:** CI must handle Python and (later) Node jobs.
- **Follow-ups:** create only directories required by actual tasks. Prototype the chosen app alongside analysis; M5 integrates/polishes it.

## Revisit when

The frontend grows a separate team or deployment cadence.
