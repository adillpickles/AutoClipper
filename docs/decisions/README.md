# Architecture Decision Records

An ADR records one significant decision: the context, the choice, and its consequences. ADRs explain **why** the project is the way it is, so neither humans nor agents re-litigate settled questions or undo choices without understanding them.

## Statuses

| Status | Meaning | What contributors do |
|---|---|---|
| **Proposed** | A recommendation not yet ratified by the team | Adopt only with agreement. An explicitly authorized reversible prototype may use it while recording the assumption and preserving proposal status. |
| **Accepted** | Agreed by the team | Follow it. Changing it requires a new ADR that supersedes it. |
| **Superseded by ADR-NNNN** | Replaced | Read the newer ADR instead |
| **Rejected** | Considered and declined | Kept for the record |

## Index

| # | Title | Status |
|---|---|---|
| [0001](0001-record-architecture-decisions.md) | Record architecture decisions | Accepted |
| [0002](0002-language-and-tooling.md) | Python 3.12 with uv, Ruff, Pyright, pytest | Proposed |
| [0003](0003-repository-layout.md) | Single repository, `src/` layout | Proposed |
| [0004](0004-staged-scope-chat-and-audio-first.md) | Staged scope: rank viewer clips first, chat + audio before video | Proposed |
| [0005](0005-local-storage-parquet-duckdb.md) | Local file storage with Parquet + DuckDB | Proposed |
| [0006](0006-license.md) | License | Proposed |
| [0007](0007-web-application-stack.md) | FastAPI backend + Next.js frontend | Proposed |
| [0008](0008-trunk-based-development-squash-merge.md) | Trunk-based development, reviewed PRs, squash merge | Accepted |
| [0009](0009-analysis-and-evaluation-contract-repair.md) | Coherent proposed analysis/evaluation contracts | Proposed |
| [0010](0010-agent-review-and-publication-workflow.md) | Shared agent audit/repair and publication workflow | Accepted (owner-directed practice) |

## Writing a new ADR

1. Copy [`0000-template.md`](0000-template.md) to `NNNN-short-title.md` with the next number.
2. Fill it in. Keep it to about a page. Link evidence (spikes, experiments).
3. Open a PR with status **Proposed**, and add it to the index above.
4. When the team agrees, change the status to **Accepted** (in that PR or a follow-up).
5. To reverse an accepted decision, write a new ADR and mark the old one **Superseded by ADR-NNNN**. Don't rewrite history.
