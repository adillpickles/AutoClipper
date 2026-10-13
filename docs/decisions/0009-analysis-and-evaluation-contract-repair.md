# ADR-0009: Coherent proposed analysis and evaluation contracts

| | |
|---|---|
| **Status** | Proposed. Documentation repair, not ratification of a training target or shared implementation contract. |
| **Date** | 2026-10-13 |
| **Related** | [Data model](../architecture/data-model.md), [Labeling](../data/labeling.md), [Features](../ml/features.md), [Evaluation](../ml/evaluation.md), OQ-1/OQ-9/OQ-15/OQ-17 |

## Context

The pre-code foundation gave incompatible instructions about source time, clip snapshots, inference labels and evaluation. Repairs must be traceable even before data exists. The original course design and owner's clarified direction require useful discovery/review/export and honest ML work, while exact models, labels and metrics remain team choices.

## Proposed contract repairs

- **Time:** clip intervals and labels are source-video seconds. A trained source-time scorer already returns source-time predictions; a raw chat-time heuristic aligns once before candidate construction. Export adds no lag correction. Padding/manual trim are separate.
- **Observations:** immutable clip snapshot files, one row per clip within a snapshot, plus capture/coverage manifests. Dataset lineage pins observations; refreshes never overwrite history. Early ranking requires actual cutoff observations and cutoff-only membership/boundaries/features, with later outcomes kept separate.
- **Inference:** unlabeled `windows` contain signal geometry/muting only. Separate `window_labels` carry training/evaluation annotations and saved split. Missing annotation is not a negative. Normalize documented API empty VOD links to null and null muted-segment lists to empty lists.
- **Grouping:** `n_clips` counts distinct clip IDs, not creators or repeated observations. Half-open source intervals avoid boundary double counting. The underspecified largest-gap split is withdrawn; retaining connected label groups separately from export-length limits is proposed. Ratify gap semantics, identifier uniqueness, ties and any splitting/member assignment before implementation.
- **Preprocessing:** per-input unsupervised full-VOD normalization is valid offline; fitted cross-dataset transforms use train only. Version the clip-URL/announcement filter across all chat aggregates in both training and inference. Ordinary reaction keywords remain separate eligible signals. Clip-derived category is excluded from independent discovery.
- **Evaluation:** propose all annotated test windows, including training-excluded buffers, for a named weak-label PR-AUC diagnostic. Missing ground truth has explicit exclusions. Proposed moment matching is one-to-one, score-ordered with deterministic ties. Observed-clip agreement is separate from human quality ratings and has no guaranteed postability bound. Omit unspecified adjusted precision. Report applicable B0–B4 on equal populations/budgets.

## Adoption boundaries

Before a real labeled dataset or reported experiment, resolve the first learning/relevance target and its horizon, unseen-channel view transformation, grouping/window rules, snapshot population, candidate budget/rounding, zero-positive/short-VOD handling, top-percent ties and matching protocol (including peaks versus interval-only methods). An authorized ingestion pilot needs source, privacy and observation contracts, without waiting for model-family selection. Choose the human-study rubric/sampling before that study, and provenance/output shape before app integration. Record adopted subsets and versions here or in focused ADRs; accepting storage ADR-0005 alone does not approve these contracts.

Privacy/retention and real-source authorization are OQ-9/OQ-17, not inferred from this ADR. Exact model families, video features and cloud services may stay open. Reversible synthetic prototypes may use explicitly authorized subsets with their proposal status intact.

## Consequences

No existing dataset, code or result is migrated. The documents now offer a consistent proposal and explicit boundaries where a choice is still required. Training, labels and product inference cannot silently share unavailable ground truth. Later adopted-contract changes need a new version and ADR, preserving reproducibility without requiring exhaustive retrieval, perfect labels or advanced PU learning.
