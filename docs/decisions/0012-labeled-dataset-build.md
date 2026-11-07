# Labeled dataset builds and frozen folds

| | |
|---|---|
| **Status** | Implementation defaults for #38 (pending merge) |
| **Date** | 2026-11-06 |
| **Related** | [Labeling](../data/labeling.md), [Data model](../architecture/data-model.md) |

## Context

The adopted v0 rules specify binary overlaps, a 60 s sampling buffer and five 3/1/1
streamer folds. Training eligibility, the configurable weight transform, persisted
lineage and the small synthetic generator need concrete representations for #38.

## Decision

- Keep all covered windows annotated, including training-excluded buffers. Add
  `train_eligible`, nullable `moment_strength` and nullable `sample_weight` to the
  annotation table, separate from inference features. Exactly 60 s distance qualifies.
- Use `1 + log1p(max_views / max(channel median views, 1))` for positives and 1 for
  confident negatives. The median is local to one broadcaster and selected capture,
  including unplaced clips. Record `one-plus-log1p-relviews-v1`; keep binary rules v0.
  This is an initial training configuration, not a measured performance claim.
- Missing/incomplete captures provide no annotations. Immature captures keep binary
  labels but no training eligibility or view weights. Retain flags in lineage.
- Sort and seed-shuffle five broadcaster IDs; rotate test and validation over the
  permutation, assigning the other three to train. Fold 0 is primary. Temporal
  holdouts inside training streamers remain an experiment-level secondary check.
- Stage a new versioned output directory and exclusively create the saved split
  last. Existing versions are refused, never silently regenerated. Hash consumed
  sources and derived tables; recheck sources before publishing. One writer per root.
- Add an optional five-streamer synthetic profile with six VODs, retaining the
  normal three-VOD/two-streamer scenario. Bump generator version to 3. This allows
  the exact production folds to be tested without creating empty placeholder roles.

## Consequences

Training must select a saved fold's training streamers **and** eligible rows.
Evaluation retains binary buffer labels. Sample weights and clip metadata are
annotations only. Review samples are emitted with status pending; watching them and
building the real pilot dataset are separate from this implementation (#40).
