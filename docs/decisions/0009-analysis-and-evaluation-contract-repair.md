# ADR-0009: Analysis and evaluation rules

| | |
|---|---|
| **Status** | Accepted (2026-10-30). Numeric parameters (window size, gaps, lengths, budgets, tolerances) stay tunable in experiments. |
| **Date** | 2026-10-13 |
| **Related** | [Data model](../architecture/data-model.md), [API contract](../architecture/api-contract.md) (#13), [Labeling](../data/labeling.md), [Features](../ml/features.md), [Modeling plan](../ml/modeling-plan.md), [Evaluation](../ml/evaluation.md), [data sources and local-first delivery](0011-data-sources-and-local-delivery.md) |

## Context

The early docs gave conflicting instructions about source time, clip snapshots, inference labels and evaluation. This ADR fixes one consistent set of rules before data exists, so labels, features, training and evaluation all agree.

## Decision

- **Time:** clip intervals and labels are source-video seconds. A trained source-time scorer already returns source-time predictions; a raw chat-time heuristic aligns once before candidate construction. Export adds no lag correction. Padding/manual trim are separate.
- **Observations:** immutable clip snapshot files, one row per clip within a snapshot, plus capture/coverage manifests. Dataset lineage pins observations; refreshes never overwrite history. Early ranking requires actual cutoff observations and cutoff-only membership/boundaries/features, with later outcomes kept separate.
- **Inference:** unlabeled `windows` contain signal geometry/muting only. Separate `window_labels` carry training/evaluation annotations and saved split. Missing annotation is not a negative. Normalize documented API empty VOD links to null and null muted-segment lists to empty lists.
- **Grouping:** `n_clips` counts distinct clip IDs, not creators or repeated observations. Half-open source intervals avoid boundary double counting. Clips within `GAP_S` merge; connected groups stay whole, and candidate length (20–45 s by default) and export length (up to 90 s) are limited separately. `moment_id` is `{video_id}:m{index}` in start order.
- **Preprocessing:** per-input unsupervised full-VOD normalization is valid offline; fitted cross-dataset transforms use train only. Version the clip-URL/announcement filter across all chat aggregates in both training and inference. Ordinary reaction keywords remain separate eligible signals. Clip-derived category is excluded from independent discovery.
- **Scorer:** one window scorer. The app ships whichever of the baselines, logistic regression and LightGBM wins on validation; viewer-clip moments are scored by the maximum window score over their span. Every candidate and window prediction carries `top_signals` (empty only when no signal stands out).
- **Target and labels:** independent discovery/scoring. Viewer clips are noisy positive labels from one snapshot taken at ingest; pilot VODs are at least 7 days old then, and younger ones are flagged and left out of view-weighted labels and metrics. Positives are weighted by moment strength: views relative to the median of the channel's own clips in the same snapshot.
- **Splits:** leave-one-streamer-out rotation over the five pilot streamers; each fold trains on 3, validates on 1 and tests on 1. Fold 0 is the primary split. Model and threshold choices use validation only.
- **Candidates:** peaks of the smoothed window score, at least 60 s apart, 20–45 s long (exports up to 90 s), for every scorer and baseline. A shortlist of the top 50 per VOD is kept; the top 10 are rendered and evaluated.
- **Evaluation:** primary metric hit rate@10 with one-to-one, score-ordered matching at ±15 s and deterministic ties; top-clip recall@10 and NDCG@10 are secondary. All annotated test windows, including training-excluded buffers, form a named weak-label PR-AUC diagnostic. Missing ground truth has explicit exclusions. Observed-clip agreement is reported separately from the blind taste test (1–5 ratings). Report the four baselines (random, chat spike, loudness, chat + loudness) on equal populations and budgets.

## Where the details live

The exact values and edge cases are in the docs, versioned with the data: grouping, windows, snapshots, relative views and splits in [labeling](../data/labeling.md); candidate generation in [modeling plan § 3](../ml/modeling-plan.md#3-candidate-generation); matching, metrics, edge cases and the taste test in [evaluation](../ml/evaluation.md); schemas in the [data model](../architecture/data-model.md). Data handling is in [data handling](../data/data-governance.md).

## API contract (#13)

The [API contract](../architecture/api-contract.md) was adopted 2026-10-30 and amended 2026-10-31 with the highlights video and local video files.

## Consequences

No existing dataset, code or result is migrated. The docs give one consistent set of rules. Training, labels and product inference cannot silently share unavailable ground truth. Later rule changes need a new label, feature or dataset version and a note here or a focused ADR, preserving reproducibility without requiring exhaustive retrieval, perfect labels or advanced PU learning.
