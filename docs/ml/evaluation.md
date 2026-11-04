# Evaluation Protocol

| | |
|---|---|
| **Status** | Current. Start with § 0; the later sections are the fuller protocol to grow into. |
| **Last updated** | 2026-10-31 |

> **No results exist yet.** This doc defines how results will be produced. Never put numbers in docs unless they come from a run recorded in `experiments/`.

## 0. Start simple

**Step 1: first numbers.** For each VOD of the test streamer, take the scorer's top 10 candidate moments. **Hit rate@10** is the fraction that match a viewer-clip moment within ±15 seconds, one to one (§ 3). Average over VODs and compare the four baselines: random, chat spike, loudness, and chat + loudness. Also report **top-clip recall@10**: of the moments holding the VOD's 5 most-viewed clips, how many our top 10 found.

**Step 2: sharper.** NDCG@10 with relevance from relative clip views; rotate the test streamer across all five folds; 95% bootstrap intervals over VODs; the small-streamer simulation (§ 6).

**Step 3: people.** Blind taste test: classmates rate our clips and the chat-spike baseline's clips from 1 to 5 (§ 7).

Build step 1 first. Everything below is the fuller version.

## 1. Principles

1. **Compare honestly.** Report every model alongside the four baselines on the same population and candidate budget. Beating the baselines is a research goal, not a gate on interface or export work.
2. **One primary metric**, chosen before running: hit rate@10. NDCG@10 and top-clip recall@10 are secondary.
3. **Held-out streamers.** Splits are a leave-one-streamer-out rotation over the five pilot streamers: each fold trains on 3, validates on 1 and tests on 1 ([labeling § 8](../data/labeling.md#8-splits)). Fold 0 is the primary split. The test streamer is never seen in training.
4. **Uncertainty is part of the result.** Report 95% bootstrap confidence intervals, resampling **VODs**, not windows.
5. **The test streamer's data is for final numbers only.** Model and threshold choices use validation.
6. **Report negative results.** "Video features didn't help" is a finding.

## 2. Baselines

| Name | ID | Definition |
|---|---|---|
| Random | `random` | Uniform random window scores (mean over 100 seeds) |
| Chat spike | `chat_spike` | Window score = `chat_msg_rate_z` |
| Loudness | `loudness` | Window score = `audio_rms_max_z` |
| Chat + loudness | `chat_loudness` | Window score = `chat_msg_rate_z + audio_rms_max_z`. A strong unlearned heuristic. |

The ID is the name used in configs, reports and the API's `scorer.name`. Implemented in `autoclipper.models.baselines` on the shared `Scorer` interface (`autoclipper.models.base`: `fit` / `score` / `save` / `load`, created by name with `make_scorer`). `random` hashes the seed and `window_id`, so a seed gives the same ranking whatever the row order; a missing feature value counts as 0 (the VOD's average) and is logged. Every baseline is a window scorer and goes through the same filtering, source-time alignment, candidate generation ([modeling plan § 3](modeling-plan.md#3-candidate-generation)), top-10 budget and matching rules as the trained models. If a signal is unavailable for a VOD (e.g. muted audio), record it and the baselines it affects.

If a learned model does not beat chat + loudness, report that outcome for the evaluated protocol and sample. The heuristic may then be the scorer the app ships; this does not prove it is universally sufficient.

## 3. Top-10 metrics

Unit: the top 10 candidates of one test VOD. Metrics are averaged over test VODs.

**Matching.** A candidate `[s, e)` matches an observed moment `[ms, me)` when the intervals overlap after widening the moment by `TOL_S = 15` s on each side: `s < me + 15` and `e > ms − 15`. Matching is one-to-one: process candidates in descending score (earlier start, then candidate ID breaks ties); each takes the unclaimed matching moment with the largest overlap with its widened interval `[ms − 15, me + 15)` (then earliest start, then `moment_id`). Each moment is matched at most once. Candidates are produced without ground-truth access.

| Metric | Definition | Role |
|---|---|---|
| **Hit rate@10** | Fraction of the top 10 candidates per VOD that match an observed moment | **Primary** |
| Top-clip recall@10 | Fraction of the moments holding the VOD's 5 most-viewed clips that the top 10 match. The 5 are chosen among all of the VOD's clips (view ties to the smaller clip ID); one without a VOD offset holds no moment and isn't replaced | Secondary: do we find the *big* moments? |
| NDCG@10 | Relevance of a candidate = strength of its matched moment (0 if none; [labeling § 6](../data/labeling.md#6-normalization-across-channels)); ideal DCG from the VOD's strongest moments | Secondary: ranking quality |
| Recall@10 | Fraction of all observed moments matched by the top 10 | Diagnostic coverage; capped by 10 / number of moments |

**Edge cases.** A VOD that yields fewer than 10 candidates is scored on what it returns (hit rate divides by the number returned). VODs with no observed moments are excluded from these metrics and counted in the report. VODs whose clip snapshot is flagged ([labeling § 7](../data/labeling.md#7-view-count-maturity)) are excluded from the view-weighted metrics (top-clip recall@10 and NDCG@10).

## 4. Window-level diagnostics

| Metric | Definition | Role |
|---|---|---|
| PR-AUC | All annotated test windows, including the training-excluded buffer windows; observed clips mark positives, other windows count as nonmatches for this diagnostic | Weak-label diagnostic, not true highlight precision |

The training negative-buffer filter does not alter this evaluation population. Report coverage, evaluated/excluded VOD counts and exclusions for missing ground truth. A filtered-subset diagnostic must be separately named with its denominator.

## 5. Ranking agreement with viewer clips (secondary)

Does the scorer order the moments viewers clipped the way their views do? Score each observed moment of a test VOD with the scorer's maximum window score over its span, then compare with moment strength. VODs with a flagged clip snapshot are excluded.

| Metric | Definition |
|---|---|
| Spearman ρ | Rank correlation between moment score and moment strength |
| Precision@5 | Fraction of the 5 highest-scored moments that are in the VOD's top 20% of moments by strength (ties at the cutoff count as top 20%) |

This measures agreement with views, not verified postability.

## 6. Held-out clips and the small-streamer simulation

### Held-out-clips test ("zero-clip mode")

Run independent discovery on test VODs while hiding all clip information through feature extraction, candidate generation, deduplication, boundary refinement and selection. Only then join observed moments for evaluation. This is the default setup for every metric above; product runs that also use viewer clips are reported separately and labeled as API-assisted.

### Small-streamer simulation

The pilot channels are all large, so the "few clips" case is simulated on held-out VODs:

- **No clips:** score each held-out VOD with its viewer clips hidden from the scorer (the zero-clip mode above). Discovery runs on chat and audio alone, so this is the product's behavior for a channel nobody clips.
- **Few clips:** for API-assisted runs, randomly keep only a fraction of the VOD's clips (e.g. 50%, 20%, 5%, with a fixed seed) before building candidates, then evaluate against the full clip snapshot. Report the metrics as a curve over the kept fraction.

This tests how much the scorer depends on viewer clips being available. It is **not** a test of small channels: the held-out VODs still have a large channel's busy chat, and a small channel's quieter chat and audience are a different input. Report that limitation with every result. If time allows, add one or two small channels' VODs as an extra, unlabeled spot check in the taste test (§ 7).

## 7. Blind taste test

Observed clips are incomplete and may be poor standalone highlights, so people judge too. Take a shuffled sample of our top clips and the chat-spike baseline's top clips from held-out VODs, rendered identically, with the method hidden. At least two classmates rate each clip from 1 to 5 on "would you watch/post this as a short?". Record the sampling, duplicate handling and inter-rater agreement. Report the mean rating per method with a 95% bootstrap CI, separately from observed-clip agreement.

## 8. Reporting format

Every reported result includes:

- Dataset version, split file and fold, git SHA, run ID, seed(s)
- The metric with its 95% CI, next to the four baselines on the same split, population and budget
- Number of VODs, channels, moments and windows evaluated, plus excluded VODs and why

Template table:

| Model | Features | Hit rate@10 (95% CI) | Top-clip recall@10 | NDCG@10 | Notes |
|---|---|---|---|---|---|
| Random | — | TBD | TBD | TBD | |
| Chat spike | — | TBD | TBD | TBD | |
| Loudness | — | TBD | TBD | TBD | |
| Chat + loudness | — | TBD | TBD | TBD | |
| Logistic regression | chat + audio | TBD | TBD | TBD | |
| LightGBM | chat + audio | TBD | TBD | TBD | |

## 9. Implementation notes

- All metrics live in `autoclipper.eval` with unit tests on hand-computed toy examples.
- The evaluation CLI writes `artifacts/reports/<run_id>/metrics.json` plus a Markdown summary (`summary.md`) with the dataset version, git commit (`-dirty` if tracked files changed) and seed; the summary `metrics.json` is copied into the experiment's folder ([experiments](experiments.md)). For now it takes the scorer's candidates directly, since candidate generation from window predictions is #27:

  ```bash
  uv run autoclipper evaluate --candidates c.parquet --moments moments.parquet --clips clips.parquet \
      --dataset-version v1 --scorer chat_spike --seed 0 [--run-id <id>] [--force] \
      [--vods test-vods.txt] [--flagged flagged.txt] [--split "<split file> fold 0"]
  ```

  `--vods` lists the VODs to evaluate (the test streamer's), one ID per line; by default every VOD with candidates is evaluated and other VODs' moments are ignored. `--flagged` lists VODs whose clip snapshot is flagged (left out of top-clip recall). `--split` is recorded in the report. The git commit is that of the installed code, wherever the command runs from.

  Candidates have `video_id`, `candidate_id`, `start_s`, `end_s` and `score`; only each VOD's top 10 by score count. Moments and clips come from the same snapshot. Step 1 is implemented in `autoclipper.eval` (`metrics`, `bootstrap`, `harness`): hit rate@10 and top-clip recall@10 per VOD, averaged, with VOD-resampled 95% intervals. A VOD with moments but no candidates scores 0.
- Bootstrap: 1,000 resamples of VODs with a fixed seed.
