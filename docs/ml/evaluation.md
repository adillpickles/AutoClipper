# Evaluation Protocol

| | |
|---|---|
| **Status** | Proposal. Once accepted, **changes require an ADR**, because they change what every past result means. |
| **Last updated** | 2026-10-09 |

> **No results exist yet.** This doc defines how results will be produced. Never put numbers in docs unless they come from a run recorded in `experiments/`.

## 1. Principles

1. **Beat the baselines or it doesn't count.** Every model is reported next to B0–B3.
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

If a learned model can't beat B4, the honest conclusion is that the heuristic is enough, and that's still a result.

## 3. v1 metrics: clip ranker

Unit: the moments of one VOD. Metrics are averaged over test VODs.

| Metric | Definition | Role |
|---|---|---|
| **NDCG@10** | Graded relevance = mature moment strength (per OQ-1), log-scaled | **Primary** |
| Precision@5 | Fraction of the top 5 that are in the VOD's top-20% moments by mature strength | What a clipper actually uses |
| Spearman ρ | Rank correlation between score and mature strength | Overall ordering |

## 4. v2 metrics: moment detector

### Window level

| Metric | Definition | Role |
|---|---|---|
| PR-AUC | Over all test windows (confident negatives + positives) | Diagnostic |

### Moment level (what matters for the product)

A predicted candidate **hits** a ground-truth moment if their intervals overlap by IoU ≥ 0.3, or the candidate peak lies inside the moment ± `TOL_S` (proposed: 10 s). Each ground-truth moment can be hit at most once.

| Metric | Definition | Role |
|---|---|---|
| **Recall@k/h** | Fraction of ground-truth moments hit by the top *k* candidates per VOD-hour (proposed *k* = 5) | **Primary** |
| Precision@k/h | Fraction of the top *k*/h candidates that hit a moment | Note: un-clipped true highlights count as misses, so precision is a **lower bound** |
| Strength-weighted recall | Recall weighted by moment `n_clips` | Do we find the *big* moments? |

### Held-out-clips test ("zero-clip mode")

Simulates small channels: run the detector on test VODs while hiding all clip information, then measure recall of their moments. This is the main v2 claim.

### Human audit (precision correction)

Viewer clips miss real highlights, so automated precision understates the model. On a sample of model candidates **with no matching viewer clip**, two team members independently rate "would you post this?" (yes / maybe / no) blind to the source, shuffled with real viewer-clip moments. Report the agreement rate and the adjusted precision.

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
- The metric with its 95% CI, next to B0–B4 on the same split
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
