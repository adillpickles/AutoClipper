# Testing Strategy

| | |
|---|---|
| **Status** | Proposal. Binding once the scaffold lands. |
| **Last updated** | 2026-10-09 |

## Goals

Tests protect the parts where bugs are **silent**: a label shifted by 10 seconds or a z-score computed with test data won't crash anything. It will quietly make results wrong. Test those parts hardest.

## Test types

| Type | What | Where | Network? | Runs in CI |
|---|---|---|---|---|
| **Unit** | Pure functions: parsing, moment merging, windowing, features, metrics | `tests/<module>/` | ❌ never | ✅ always |
| **Contract** | A stage's output matches [data model](../architecture/data-model.md) (columns, dtypes, keys) | `tests/contracts/` | ❌ | ✅ |
| **Integration** | Several stages chained on a tiny synthetic VOD (generated audio + fake chat + fake clips) | `tests/integration/` | ❌ | ✅ |
| **Live API** | Authorized real Twitch API / tool calls | `tests/live/`, marked `@pytest.mark.live` and excluded by default | ✅ | ❌ (manual, needs `.env` and source authorization) |
| **Metric checks** | Eval metrics match hand-computed values on toy inputs | `tests/eval/` | ❌ | ✅ |

## Must-have tests (by area)

- **Duration parsing:** `"3h8m33s"`, `"45m"`, `"59s"`, `"1h0m0s"` → seconds.
- **Moment merging:** overlapping, gap-boundary, disjoint, long connected chains, null offsets, duplicate snapshot observations and deterministic ties under the adopted grouping rule.
- **Windowing:** exact boundaries, last partial window, muted-segment flags.
- **Labels:** window/moment overlap at edges (touching ≠ overlapping, unless the rule says so), confident-negative buffer.
- **Features:** empty chat window → 0 not NaN, zero-variance z-score → 0, lead/lag at VOD edges.
- **Pseudonymization:** the same name + salt gives the same hash, a different salt gives a different hash, and the raw name never appears in output.
- **Splits:** no broadcaster appears in two splits. Deterministic given the seed.
- **Metrics:** NDCG, recall@k, IoU matching and bootstrap on tiny hand-worked examples.
- **Export:** FFmpeg command construction (as data, without running FFmpeg in unit tests). One integration test on a generated 5-second clip.

## Fixtures

- Live in `tests/fixtures/`. **Synthetic only**: fake usernames (`user_001`), invented messages, generated audio (sine bursts, noise).
- Keep them tiny (KB). Generate larger ones on the fly in a fixture function.
- Recorded API responses (for client tests) must be **scrubbed**: replace usernames and IDs with fakes.

## Conventions

- pytest, with plain `assert`s. Use `pytest.approx` for floats.
- File naming: `tests/<module>/test_<file>.py`. Test names describe behavior: `test_merge_clips_joins_clips_within_gap`.
- One behavior per test. Use parametrize for edge-case tables.
- No sleeps, no network, no reliance on wall-clock time in unit tests. Inject clocks and HTTP clients.
- Test **behavior, not implementation**. A refactor that keeps behavior shouldn't break tests.
- Bug fixes come with a regression test that fails before the fix.
- The scaffold must configure default pytest selection to exclude live tests (a marker alone does not exclude them). Explicit live runs need the appropriate credentials/authorization and never run in ordinary CI.

## Commands (planned)

```bash
uv run pytest                    # unit + contract + integration
uv run pytest -m live            # live API tests (needs .env); never in CI
uv run pytest --cov=autoclipper  # coverage report
```

Coverage target (proposal): ≥ 80% for `labels/`, `features/` and `eval/`. No hard target elsewhere. Meaningful tests beat a coverage number.
