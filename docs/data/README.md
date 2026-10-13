# Data Documentation

How AutoClipper gets, labels and handles data. Read in this order:

| Doc | What it covers |
|---|---|
| [data-governance.md](data-governance.md) | **Read first.** Privacy, platform terms, what may never go in git. |
| [data-sources.md](data-sources.md) | Twitch API endpoints, chat replay, media download, backups |
| [collection-plan.md](collection-plan.md) | Which channels, how many VODs, the collector design, storage |
| [labeling.md](labeling.md) | How viewer clips become moments and window labels. Splits. |
| [../architecture/data-model.md](../architecture/data-model.md) | Table schemas (the contract between pipeline stages) |

**Quick facts**

- Labels come from **viewer-made Twitch clips** (`vod_offset` + `duration`).
- Inside the pipeline, time is **seconds from VOD start** (`*_s`).
- Chat usernames are **pseudonymized at ingestion**.
- Data lives in git-ignored `data/` and is **never committed**.
- VODs expire, so begin a small **authorized** pilot promptly; synthetic development can proceed while OQ-9/OQ-17 are resolved.
