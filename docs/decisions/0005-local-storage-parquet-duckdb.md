# ADR-0005: Local file storage with Parquet + DuckDB

| | |
|---|---|
| **Status** | Accepted (2026-10-16). |
| **Date** | 2026-10-09 |
| **Related** | [Data model](../architecture/data-model.md), [Architecture § Storage](../architecture/overview.md#5-storage-layout) |

## Context

Data volumes are moderate: hundreds of VODs, millions of chat messages, ~10⁵–10⁶ windows. The initial pipeline runs on laptops, and the same stages can run on a worker. Stages should be independently runnable and inspectable, and agents need simple, predictable I/O.

## Decision

- Store tabular data as **Parquet** files under a git-ignored `data/` tree (raw → interim → processed), and query with **DuckDB** and/or Polars/pandas.
- Store media (audio) as files under `data/raw/`.
- Track collection state in a `catalog.parquet` table.
- Version datasets via `data/splits/<dataset_version>.json` (see [data model § Dataset versioning](../architecture/data-model.md#dataset-versioning)).
- The initial API keeps job records in memory and writes results and exports as files under `artifacts/` ([API contract](../architecture/api-contract.md)). A database server is unnecessary for that setup; add persistence or hosted storage with the feature or deployment that needs it ([delivery defaults](0011-data-sources-and-local-delivery.md)).

## Options considered

| Option | Pros | Cons |
|---|---|---|
| **Parquet + DuckDB** (chosen) | Fast columnar analytics on a laptop. Zero ops. Files are easy to share and inspect. | No concurrent writers. Manual versioning. |
| PostgreSQL | Concurrency, constraints | Ops overhead. Overkill for batch analytics. |
| SQLite | Simple | Row-oriented and slow for analytics |
| CSV/JSON | Universal | Slow, large, loses types |
| DVC for data versioning | Real versioning | Extra tool to learn. Needs remote storage. Can be added later. |

## Consequences

- **Positive:** simple, fast, portable. Each stage reads and writes files.
- **Negative:** shared files need coordinated writes (one collector process per catalog/output), whether the storage is local, on a team drive or synchronized from an object store.
- **Follow-ups:** consider DVC if dataset sharing becomes painful.

## Revisit when

Live mode needs concurrent writes, or the web app needs multi-user persistence.
