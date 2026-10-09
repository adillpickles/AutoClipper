# Modeling Plan

| | |
|---|---|
| **Status** | Proposal. Model choices are a starting plan. Only the evaluation protocol is meant to be stable. |
| **Last updated** | 2026-10-09 |
| **Related** | [Features](features.md) · [Evaluation](evaluation.md) · [Experiments](experiments.md) · [Labeling](../data/labeling.md) |

## 1. The two ML problems

| | **v1: Clip ranker** | **v2: Moment detector** |
|---|---|---|
| Question | Of the moments viewers clipped in this VOD, which are best to post? | Where in this VOD are the clip-worthy moments, including un-clipped ones? |
| Unit | Moment (merged viewer clips) | 10-second window → candidate moment |
| Input | Chat + audio features over the moment's span (and context) | Chat + audio features per window (and neighbors) |
| Output | Score per moment → ranking | Score per window → peaks → candidates |
| Label | See OQ-1 (below) | `label_pos` / `label_n_clips` from [labeling](../data/labeling.md) |
| Why ML is needed | Twitch views are an imperfect, delayed proxy for "worth posting" | No clips exist for un-clipped time, live streams or small channels |

### OQ-1: the v1 target needs a decision

For clips that already exist, Twitch gives us their view counts, so **predicting Twitch views of old clips from features is not useful by itself.** Candidate framings, from most to least recommended:

1. **Early ranking (recommended).** Predict a moment's *mature* strength (7-day `n_clips` / normalized views) using only signals available **when the stream ends**: chat, audio, early clip counts. Baseline: rank by views at stream end. This is the clipper's real situation, since they want to post fast, before views mature.
2. **Clip-worthiness without popularity.** Train on features only, without views as input, and evaluate on held-out channels against mature strength. The "sort by views" baseline is then an *oracle-ish upper reference*, not a competitor.
3. **Short-form performance** (stretch). Target is normalized views on TikTok/Reels from external data. This is the true product target, but it depends on that data source (OQ-3).

Until OQ-1 is decided, v1 work should build the shared pieces (features, moments, eval harness) that every option needs.

## 2. Model progression

Each step must beat the previous one on validation **and** stay explainable enough to debug. Don't skip steps: the simple models are the baselines that make the fancy ones credible.

| Step | Model | Purpose |
|---|---|---|
| B0–B3 | Heuristic baselines ([evaluation § baselines](evaluation.md#2-baselines)) | The floor every model must beat |
| M-1 | Logistic regression on standardized features | Sanity check. Coefficients show which signals matter. |
| M-2 | Gradient-boosted trees (LightGBM). For v1 ranking, `LGBMRanker` with `lambdarank`. | Strong tabular default. Handles non-linearity and missing values. |
| M-3 | Small neural net: an MLP on window features, then a **temporal model** (1D-CNN or GRU) over a sequence of windows | Captures build-up and payoff patterns over time, so chat lag doesn't need hand-crafted lead features |
| M-4 (stretch) | Add audio embeddings (pretrained audio model) and/or video features | Measured as an ablation over M-2/M-3 |

**Proposal:** M-2 is the expected "production" model for the demo. M-3 is the main neural-network deliverable. Keep both if M-3 doesn't clearly win.

## 3. v2 candidate generation

Window scores → candidate moments:

1. Smooth scores over time (e.g. a rolling mean over 3 windows).
2. **Peak picking:** local maxima above a threshold chosen on validation to hit a target candidates-per-hour rate.
3. **Non-max suppression:** drop peaks within `MIN_GAP_S` (e.g. 60 s) of a higher one.
4. **Boundaries:** start at `peak - PRE_S`, end at `peak + POST_S`, then adjust for chat lag. Proposed: `PRE_S = 20`, `POST_S = 10`, tuned on validation against human clip boundaries.
5. Merge candidates that overlap existing viewer-clip moments, and tag them `source=viewer_clips` vs. `source=model`.

## 4. Training practices

- **Splits:** broadcaster-grouped, saved to disk ([labeling § splits](../data/labeling.md#8-splits)). Never tune on test.
- **Class imbalance:** positives are rare among windows. Use class weights or focal loss, and evaluate with PR-AUC and ranking metrics, never accuracy.
- **Hyperparameter search:** small and budgeted (e.g. ≤ 30 trials, Optuna or random search) on validation. Record the search space in the run config.
- **Seeds:** every run takes a seed. Report mean ± std over ≥ 3 seeds for the NN.
- **Feature leakage checklist:**
  - No features computed from clip metadata in window models (clip count, clip titles) unless the experiment is explicitly "early ranking" with timestamps checked.
  - Normalization statistics are computed on train only.
  - No information from after the prediction time in "live" experiments.
- **Explainability:** SHAP or permutation importance for M-2. Report the top signals per candidate for the UI.

## 5. Ablations we plan to report

Each one answers a question a judge or reviewer would ask:

1. **Chat only vs. audio only vs. chat + audio.** Which signal matters?
2. **+ lead (look-ahead) chat features vs. without.** How much does chat lag matter? This also measures the cost of live mode.
3. **+ video features (stretch).** "Does video add anything beyond chat and audio?" This is a legitimate result whichever way it goes.
4. **Seen vs. unseen channels.** How well does the model generalize to new streamers?
5. **Large vs. small channels.** Does it work where clips are scarce?

## 6. Related work (for orientation)

- Chat-based highlight detection in live-streamed esports (e.g. Fu et al., *Video Highlight Prediction Using Audience Chat Reactions*, EMNLP 2017) found that audience chat reactions alone are strongly predictive of highlights. That supports chat-first.
- Commercial auto-clippers (Eklipse, Medal, Opus Clip) show there's demand. Our differentiation is learning from viewer clips and transparent evaluation.

> Any claim in project materials comparing AutoClipper to these tools must be backed by our own evaluation, not assumed.
