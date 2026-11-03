# Reviewing Changes

Use this checklist when a second opinion would help. [CONTRIBUTING § 7](../../CONTRIBUTING.md#7-review) covers the PR workflow.

## Verification context

The PR should say how the change was checked and what remains unverified. If useful checks are missing, run them or point out the gap.

## Common agent failure modes in this project

| Failure | How to spot it | Example here |
|---|---|---|
| **Invented API details** | Fields/params not in [data sources](../data/data-sources.md) or the official reference | A `video_id` filter on Get Clips (there isn't one) |
| **Silent unit/time bugs** | Mixed seconds and milliseconds, naive datetimes, IDs cast to int | `vod_offset` treated as ms, `created_at` without tz |
| **Swallowed errors** | `except Exception: pass`, returning empty frames on failure | A chat download failure leading to a VOD with "no chat" |
| **Tests that test the implementation** | Tests re-compute the expected value with the same code, or mock everything | Asserting `compute_z(x) == compute_z(x)` |
| **Leakage** | Fitting preprocessing on pooled held-out data, random window splits, hidden clip metadata used in discovery | `StandardScaler().fit(all_windows)`; per-input offline normalization is allowed |
| **Scope creep** | Unrelated changes that don't help meet the task's acceptance criteria; required wiring, dependencies and doc updates belong with the feature | Renaming unrelated feature columns across the codebase |
| **Contract drift** | Column names that don't match [data model](../architecture/data-model.md) / [features](../ml/features.md) | `chat_rate` instead of `chat_msg_rate` |
| **Fabricated numbers** | Metrics or sizes in docs or comments with no linked run | "Achieves 0.82 NDCG" in a docstring |
| **Unjustified dependencies** | New entries in `pyproject.toml` | Pulling in a whole framework for one helper |
| **Over-engineering** | Abstract base classes, plugin systems or config layers no task asked for | A "FeatureFactoryRegistry" for four functions |
| **Stale docs** | Behavior changed but docs didn't | New CLI flag missing from `AGENTS.md § 5` |

## Review steps

1. **Read the issue first**, then the PR description, then the diff.
2. **Check the contract:** names, types and units against the docs.
3. **Read the tests before the code.** Do they cover the acceptance criteria and edge cases? Would they fail if the logic were wrong?
4. **Run relevant checks** when they would resolve uncertainty; use the author's recorded results where sufficient.
5. **Look for what's missing:** error handling, logging of dropped rows, doc updates.
6. **Comment precisely:** mark **blocking** vs. **nit**, and quote the doc section that applies.

## When the agent was wrong because the docs were unclear

Fix the docs. Either ask the author to update the relevant doc in this PR, or open a `docs` issue. Note the gap in your review so the pattern is visible.
