# Reviewing Agent-Written PRs

Agent-written code usually *looks* right. Review checks whether it *is* right. Use this alongside the general checklist in [CONTRIBUTING § What reviewers check](../../CONTRIBUTING.md#what-reviewers-check).

## Before review (the submitter's job)

The person who opened the PR must have:

- run the code and the tests themselves
- read the whole diff
- filled in "How was this verified?" with specifics

If they haven't, send it back. Don't spend review time on unverified output.

## Common agent failure modes in this project

| Failure | How to spot it | Example here |
|---|---|---|
| **Invented API details** | Fields/params not in [data sources](../data/data-sources.md) or the official reference | A `video_id` filter on Get Clips (there isn't one) |
| **Silent unit/time bugs** | Mixed seconds and milliseconds, naive datetimes, IDs cast to int | `vod_offset` treated as ms, `created_at` without tz |
| **Swallowed errors** | `except Exception: pass`, returning empty frames on failure | A chat download failure leading to a VOD with "no chat" |
| **Tests that test the implementation** | Tests re-compute the expected value with the same code, or mock everything | Asserting `compute_z(x) == compute_z(x)` |
| **Leakage** | Normalizing with all data, random window splits, clip-derived features in window models | `StandardScaler().fit(all_windows)` |
| **Scope creep** | Changes to files the issue didn't mention, "while I was here" refactors | Renaming feature columns across the codebase |
| **Contract drift** | Column names that don't match [data model](../architecture/data-model.md) / [features](../ml/features.md) | `chat_rate` instead of `chat_msg_rate` |
| **Fabricated numbers** | Metrics or sizes in docs or comments with no linked run | "Achieves 0.82 NDCG" in a docstring |
| **Unjustified dependencies** | New entries in `pyproject.toml` | Pulling in a whole framework for one helper |
| **Over-engineering** | Abstract base classes, plugin systems or config layers no task asked for | A "FeatureFactoryRegistry" for four functions |
| **Stale docs** | Behavior changed but docs didn't | New CLI flag missing from `AGENTS.md § 5` |

## Review steps

1. **Read the issue first**, then the PR description, then the diff.
2. **Check the contract:** names, types and units against the docs.
3. **Read the tests before the code.** Do they cover the acceptance criteria and edge cases? Would they fail if the logic were wrong?
4. **Run it** if the change is non-trivial: `gh pr checkout <n> && uv run pytest`.
5. **Look for what's missing:** error handling, logging of dropped rows, doc updates.
6. **Comment precisely:** mark **blocking** vs. **nit**, and quote the doc section that applies.

## When the agent was wrong because the docs were unclear

Fix the docs. Either ask the author to update the relevant doc in this PR, or open a `type:docs` issue. Note the gap in your review so the pattern is visible.
