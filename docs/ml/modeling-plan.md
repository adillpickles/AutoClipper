# Modeling Plan

| | |
|---|---|
| **Status** | Proposal. Targets, model choices and evaluation protocol await team agreement (OQ-1/OQ-15, ADR-0009). |
| **Last updated** | 2026-10-13 |
| **Related** | [Features](features.md) · [Evaluation](evaluation.md) · [Experiments](experiments.md) · [Labeling](../data/labeling.md) |

## 1. Candidate ML problems

The product needs useful discovery and ranking, not two mandatory models. Available API clips can seed candidates directly. One learned scorer may rank signal-derived candidates across the VOD; heuristic shortlisting followed by learned reranking is another option. The table describes experiment framings, not a fixed architecture.

| | **v1: Clip ranker** | **v2: Moment detector** |
|---|---|---|
| Question | Of the moments viewers clipped in this VOD, which are best to post? | Where in this VOD are the clip-worthy moments, including un-clipped ones? |
| Unit | Moment (merged viewer clips) | Proposed windows or signal-derived shortlist → candidate moment |
| Input | Chat + audio features over the moment's span (and context) | Chat + audio features per window (and neighbors) |
| Output | Score per moment → ranking | Score per window → peaks → candidates |
| Label | See OQ-1 (below) | `label_pos` / `label_n_clips` from [labeling](../data/labeling.md) |
| Why ML is needed | Twitch views are an imperfect, delayed proxy for "worth posting" | No clips exist for un-clipped time, live streams or small channels |

### OQ-1: the v1 target needs a decision

For clips that already exist, Twitch gives us their view counts, so **predicting Twitch views of old clips from features is not useful by itself.** Candidate framings, from most to least recommended:

1. **Independent discovery/scoring.** Learn from noisy observed clips using chat/audio signals, then compare with heuristics and human judgments. Clip popularity is a proxy outcome, not objective highlight quality. This can also rank generated candidates.
2. **Early ranking (conditional experiment).** Predict later clip strength from information available at an actual, recorded inference cutoff. Stream-end claims require a stream-end observation; a day-one snapshot cannot substitute. Freeze candidates, clip membership, boundaries and all clip-derived inputs at that cutoff; later snapshots supply targets only. Compare views at the same cutoff. Adoption needs a cutoff/target mapping under OQ-1, not an obligatory first model.
3. **Short-form performance** (stretch). A separate target using external engagement data (OQ-3), not the semester product's underlying requirement.

Until OQ-1 is decided, synthetic adapters, signal extraction, UI/export and toy evaluation mechanics can proceed under explicitly authorized proposals. Do not train a claimed ranking model or finalize its harness against an unspecified target.

## 2. Model progression

Start with applicable heuristics and a simple trained model. Add complexity only for a measured weakness or an agreed learning goal. Report comparisons on validation; a model need not win to be a valid experiment. Delivery continues alongside research.

| Step | Model | Purpose |
|---|---|---|
| B0–B4 | Applicable heuristic baselines ([evaluation § baselines](evaluation.md#2-baselines)) | References on the same population and budget |
| M-1 | Logistic regression on standardized features | Sanity check. Coefficients show which signals matter. |
| M-2 | Gradient-boosted trees (LightGBM). For v1 ranking, `LGBMRanker` with `lambdarank`. | Strong tabular default. Handles non-linearity and missing values. |
| M-3 (optional) | Small MLP or temporal model (1D-CNN/GRU) | Investigate temporal context if simpler approaches leave a useful gap |
| M-4 (stretch) | Add audio embeddings (pretrained audio model) and/or video features | Measured as an ablation over M-2/M-3 |

No model family is a mandatory demo deliverable. Select the useful approach based on evaluation, cost and maintainability; include genuine trained-ML work and meaningful signal ablations even if a heuristic remains strongest.

## 3. v2 candidate generation

Window scores → candidate moments:

1. Smooth scores over time (e.g. a rolling mean over 3 windows).
2. **Peak picking:** local maxima above a threshold chosen on validation to hit a target candidates-per-hour rate.
3. **Non-max suppression:** drop peaks within `MIN_GAP_S` (e.g. 60 s) of a higher one.
4. **Boundaries:** predictions trained against source-time labels already locate the source event. Apply context padding only: proposed `PRE_S = 20`, `POST_S = 10`, tuned on validation, clamped to VOD bounds. A raw chat-time heuristic converts its peak to source time once before padding; export never shifts it again.
5. At product inference, available viewer clips may be combined with discovered candidates using an agreed deduplication policy. For independent-discovery evaluation, hide clip metadata through scoring, boundary generation and selection; join ground truth only afterward. Do not use held-out clips to refine candidates.

Candidate budgets, peak/boundary rules and provenance representation require agreement before adopting this proposal; record them in the experiment config (ADR-0009). The final pipeline need not use peak picking if another justified approach works better.

## 4. Training practices

- **Splits:** broadcaster-grouped, saved to disk ([labeling § splits](../data/labeling.md#8-splits)). Never tune on test.
- **Class imbalance:** positives are rare among windows. Use class weights or focal loss, and evaluate with PR-AUC and ranking metrics, never accuracy.
- **Hyperparameter search:** small and budgeted (e.g. ≤ 30 trials, Optuna or random search) on validation. Record the search space in the run config.
- **Seeds:** every run takes a seed. Report mean ± std over ≥ 3 seeds for the NN.
- **Feature leakage checklist:**
  - Independent-discovery inputs exclude clip metadata and clip-derived context (counts, titles, views, clip-inferred categories); apply the versioned chat clip-announcement filter in [features](features.md).
  - Fit cross-dataset preprocessing on train only. Unsupervised normalization within the current full input VOD is allowed for offline inference; see [features § Normalization](features.md#normalization).
  - No information from after the prediction time in "live" experiments.
- **Explainability (optional polish):** permutation importance or SHAP when useful; avoid presenting an explanation as proof of quality.

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
