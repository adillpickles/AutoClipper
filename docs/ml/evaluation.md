# Evaluation Protocol

| | |
|---|---|
| **Status** | Proposal, amended under ADR-0009. Ratify target, population, matching and budget before reported experiments; changes require an ADR and dataset/protocol version. |
| **Last updated** | 2026-10-13 |

> **No results exist yet.** This doc defines how results will be produced. Never put numbers in docs unless they come from a run recorded in `experiments/`.

## 1. Principles

1. **Compare honestly.** Report every model alongside applicable B0–B4 on the same population and candidate budget. Baseline improvement is a research goal, not a gate preventing interface/export work.
2. **One primary metric per question**, chosen before running.
3. **Held-out channels.** The test split contains broadcasters never seen in training.
4. **Uncertainty is part of the result.** Report 95% bootstrap confidence intervals, resampling **VODs**, not windows.
5. **The test split is for final numbers only.** Iterate on validation.
6. **Report negative results.** "Video features didn't help" is a finding.

## 2. Baselines

| ID | Name | Applies to | Definition |
|---|---|---|---|
| B0 | Random | v1, v2 | Uniform random scores (mean over 100 seeds) |
| B1 | Twitch views | v1 | Rank moments by Twitch views **at the same observation time the model has** (see OQ-1) |
| B2 | Chat spike | v1, v2 | Score = `chat_msg_rate_z` (max over the moment, or per window) |
| B3 | Loudness spike | v1, v2 | Score = `audio_rms_max_z` |
| B4 | Chat + loudness | v1, v2 | Score = `chat_msg_rate_z + audio_rms_max_z`. A strong unlearned heuristic. |

If a learned model does not beat B4, report that outcome for the evaluated protocol and sample. The heuristic may remain the useful product choice; this does not prove it is universally sufficient.

B1 applies where existing clips are available to the system. It is not an independent-discovery comparator with hidden clip metadata; if shown there, label it an API-assisted reference. All baselines share filtering, source-time alignment, selection budget and matching rules. Record any unavailable signal and applicable baseline exclusions.

## 3. v1 metrics: clip ranker

Unit: the moments of one VOD. Metrics are averaged over test VODs.

| Metric | Definition | Role |
|---|---|---|
| **NDCG@10** | Graded relevance from an agreed target (OQ-1); mature clip strength is one proxy | Proposed primary |
| Precision@5 | Fraction of the top 5 in the VOD's top-20% by the agreed proxy | Proxy agreement, not verified postability |
| Spearman ρ | Rank correlation between score and mature strength | Overall ordering |

## 4. v2 metrics: moment detector

### Window level

| Metric | Definition | Role |
|---|---|---|
| PR-AUC | All annotated test windows, including unlabeled buffer windows; observed clips mark positives, other windows are treated as nonmatches for this diagnostic | Weak-label diagnostic, not true highlight precision |

The training negative-buffer filter does not alter this evaluation population. Report coverage, evaluated/excluded VOD counts and exclusions for missing ground truth. A filtered-subset diagnostic must be separately named with its denominator. Under ADR-0009, budget rounding, short VODs, VODs with no observed positives, target transformation and top-percent ties need ratification before metrics are implemented; do not choose defaults by accident.

### Moment level (what matters for the product)

A predicted candidate **matches an observed moment** under the proposed IoU ≥ 0.3 OR source-time peak-in-interval ± `TOL_S` (10 s) rule. Process candidates in descending score (candidate ID breaks ties); match at most one unclaimed eligible moment per candidate, choosing highest IoU then earliest start then moment ID. Each moment is matched at most once. Peaks and boundaries must be produced without ground-truth access. Ratify the peak representation or use an explicitly chosen interval-only protocol for methods without peaks; do not invent peaks from labels. The course design's ±15 s rule remains an alternative to ratify; do not mix protocols within a comparison.

| Metric | Definition | Role |
|---|---|---|
| **Recall@k/h** | Fraction of observed moments matched by the top *k* candidates per VOD-hour (proposed *k* = 5) | Proposed primary |
| Observed-clip agreement@k/h | Fraction of selected candidates that match an observed moment | Neither a quality precision estimate nor a guaranteed lower bound on postability |
| Strength-weighted recall | Recall weighted by moment `n_clips` | Do we find the *big* moments? |

### Held-out-clips test ("zero-clip mode")

Run independent discovery on test VODs while hiding all clip information through feature extraction, candidate generation, deduplication, boundary refinement and selection. Only then join observed moments for evaluation. This tests independence from clip availability. It does not reproduce sparse chat or demonstrate performance on small channels; report channel-size analyses separately.

### Human audit (separate quality assessment)

Observed clips are incomplete and may be poor standalone highlights. Propose a blind, shuffled sample of learned-method and baseline candidates, including matched and unmatched picks, rated independently by two reviewers. Record sampling, duplicate handling, rubric, media exclusions and agreement. The original 1–5 rubric and yes/maybe/no are alternatives under OQ-15; choose one before the study. Viewing enjoyment and suitability for posting can differ, so state the assessed use case. Report judgments separately from observed-clip agreement. No "adjusted precision" estimate is promised without a sampling/estimation protocol.

## 5. Online experiment (stretch)

**Question:** do model-picked clips get more short-form views than random or naive picks?

Design (proposal, requires team sign-off and streamer permission per [governance](../data/data-governance.md#publishing-clips)):

- One or more clip accounts, one streamer who permits reposting.
- For each VOD, take the model's top *n* and a random *n* from the same VOD's candidates (or B1's top *n*), and **randomize posting order and times**.
- Same formatting for every arm (same crop, caption style, title template).
- Outcome: views at a fixed age (e.g. 72 h), log-transformed. Compare arms with a paired test per VOD.
- Pre-register the hypothesis, sample size and analysis in the experiment README **before posting**.
- Threats to validity: account growth over time, posting-time effects, platform algorithm randomness, small sample sizes. Report them all.

Any money earned is incidental and not a metric.

## 6. Reporting format

Every reported result includes:

- Dataset version, split file, git SHA, run ID, seed(s)
- The metric with its 95% CI, next to applicable B0–B4 on the same split/population/budget
- Number of VODs, channels, moments and windows evaluated

Template table:

| Model | Features | NDCG@10 (95% CI) | P@5 | Notes |
|---|---|---|---|---|
| B0 random | — | TBD | TBD | |
| B4 chat+loudness | — | TBD | TBD | |
| LightGBM | chat + audio | TBD | TBD | |

## 7. Implementation notes

- All metrics live in `autoclipper.eval` with unit tests on hand-computed toy examples.
- The evaluation CLI takes predictions (Parquet) + a dataset version and writes `artifacts/reports/<run_id>/metrics.json` plus a Markdown summary.
- Bootstrap: 1,000 resamples of VODs with a fixed seed.
