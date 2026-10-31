# Technical Choices

These notes explain the main implementation defaults and their tradeoffs. Build from them and adapt them when the task calls for it, updating the affected docs alongside the implementation.

| Choice | Background |
|---|---|
| Python 3.12, uv, Ruff, Pyright and pytest | [Language and tooling](0002-language-and-tooling.md) |
| Chat and audio scoring before video features | [Build order](0004-staged-scope-chat-and-audio-first.md) |
| Parquet files and DuckDB, with shared storage as needed | [Storage](0005-local-storage-parquet-duckdb.md) |
| FastAPI and Next.js | [Application stack](0007-web-application-stack.md) |
| Twitch inputs, shared collection and adaptable delivery | [Data sources and delivery](0011-data-sources-and-local-delivery.md) |

The implementation contracts live in the [data model](../architecture/data-model.md), [API contract](../architecture/api-contract.md), [labeling](../data/labeling.md), [modeling plan](../ml/modeling-plan.md) and [evaluation](../ml/evaluation.md). The contributor workflow lives in [CONTRIBUTING](../../CONTRIBUTING.md).

Explain engineering choices in the PR. Add a short note here only when a substantial tradeoff would help future work.
