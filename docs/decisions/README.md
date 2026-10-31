# Architecture Decision Records

An ADR records one significant decision: the context, the choice, and its consequences. ADRs explain **why** the project is the way it is, so neither humans nor agents re-open settled questions or undo choices without understanding them.

## Statuses

| Status | Meaning | What contributors do |
|---|---|---|
| **Accepted** | The current decision | Build from it. Update it or add a superseding ADR with the implementation when the task needs a change. |
| **Superseded by ADR-NNNN** | Replaced | Read the newer ADR instead |
| **Rejected** | Considered and declined | Kept for the record |

## Index

| # | Title | Status |
|---|---|---|
| [0001](0001-record-architecture-decisions.md) | Record architecture decisions | Accepted |
| [0002](0002-language-and-tooling.md) | Python 3.12 with uv, Ruff, Pyright, pytest | Accepted |
| [0003](0003-repository-layout.md) | Single repository, `src/` layout | Accepted |
| [0004](0004-staged-scope-chat-and-audio-first.md) | Staged scope: chat + audio before video | Accepted |
| [0005](0005-local-storage-parquet-duckdb.md) | Local file storage with Parquet + DuckDB | Accepted |
| [0006](0006-license.md) | MIT License | Accepted |
| [0007](0007-web-application-stack.md) | FastAPI backend + Next.js frontend | Accepted |
| [0008](0008-trunk-based-development-squash-merge.md) | Trunk-based development, PRs with green CI, squash merge | Accepted |
| [0009](0009-analysis-and-evaluation-contract-repair.md) | Analysis and evaluation rules | Accepted |
| [0010](0010-agent-review-and-publication-workflow.md) | Agent workflow, publishing and optional second-opinion review | Accepted |
| [0011](0011-data-sources-and-local-delivery.md) | Data sources and delivery defaults | Accepted |

## Writing a new ADR

1. Copy [`0000-template.md`](0000-template.md) to `NNNN-short-title.md` with the next number.
2. Fill it in. Keep it to about a page. Link evidence (spikes, experiments).
3. Make the call: compare the options, pick the one that best serves the product, and set the status to **Accepted**. Add it to the index and include it with the implementation. Publish the PR when requested.
4. To reverse an accepted decision, write a new ADR and mark the old one **Superseded by ADR-NNNN**. Don't rewrite history.
