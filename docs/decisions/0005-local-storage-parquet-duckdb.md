# ADR-0005: Local file storage with Parquet + DuckDB

| | |
|---|---|
| **Status** | Accepted for pipeline and dataset storage (2026-10-16). The app's job/result store and hosting are deferred to OQ-18. |
| **Date** | 2026-10-09 |
| **Related** | [Data model](../architecture/data-model.md), [Architecture § Storage](../architecture/overview.md#5-storage-layout-proposal) |

## Context

Data volumes are moderate: hundreds of VODs, millions of chat messages, ~10⁵–10⁶ windows. The pipeline runs on laptops. Stages should be independently runnable and inspectable, and agents need simple, predictable I/O.

## Decision

- Store tabular data as **Parquet** files under a git-ignored `data/` tree (raw → interim → processed), and query with **DuckDB** and/or Polars/pandas.
- Store media (audio) as files under `data/raw/`.
- Track collection state in a `catalog.parquet` table.
- Version datasets via `data/splits/<dataset_version>.json` (see [data model § Dataset versioning](../architecture/data-model.md#dataset-versioning)).
- The data pipeline needs no database server. Where the web app keeps job state and results (local files/SQLite or hosted Postgres such as Supabase, as in the project design doc) is decided together with hosting under OQ-18.

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
- **Negative:** the team must share data out-of-band (OQ-5), and concurrent writes need care (one collector process).
- **Follow-ups:** decide the shared storage location (OQ-5). Consider DVC if dataset sharing becomes painful.

## Revisit when

Live mode needs concurrent writes, or the web app needs multi-user persistence.
