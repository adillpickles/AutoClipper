# Modeling Plan

| | |
|---|---|
| **Status** | Current. One window scorer; model progression: baselines → logistic regression → LightGBM. Training target: independent discovery/scoring ([data sources and delivery defaults](../decisions/0011-data-sources-and-local-delivery.md)). Metrics and matching: [evaluation](evaluation.md). |
| **Last updated** | 2026-10-31 |
| **Related** | [Features](features.md) · [Evaluation](evaluation.md) · [Experiments](experiments.md) · [Labeling](../data/labeling.md) |

## 0. The plan in plain English

**What the model learns.** For every 10-second window of a VOD: how likely is it that this moment is worth clipping? Training examples come from the clips viewers already made. A window that overlaps a viewer clip is a "good moment", and clips with more views than that streamer's typical clip count more. Windows more than a minute away from any clip are "not clipped" examples. Windows close to a clip are left out of training because their label is unclear.

**What it looks at.** Chat and audio signals for the window and its neighbors ([features](features.md)). Chat reacts 5–15 seconds late, so the model also sees the next few windows' chat.

**From scores to clips.** Smooth the scores, take the peaks, keep peaks at least 60 seconds apart, and turn each into a clip that starts about 20 seconds before the peak and runs 20–45 seconds by default (configurable). The best 50 make a shortlist; the top 10 become the ranked vertical clips; the same top moments in stream order make the highlights video ([API contract § 7](../architecture/api-contract.md#7-highlights-video)). Later, snap the start and end to natural pauses in speech.

**Phases (each one ships before the next starts):**

1. **Baselines:** random, biggest chat spike, loudest moment, and chat + loudness combined. No training.
2. **Logistic regression** on the Phase 1 features. Shows which signals matter.
3. **LightGBM.** Learns combinations such as "loud *and* chat laughing".
4. **Polish:** pretrained laughter/scream/cheer detection and Whisper speech features for the shortlist (#64); then an LLM reads the transcript and a chat summary of the top ~30 shortlisted moments, picks the best 10 and writes each clip's reason and title (#65). This last step is how tools like Opus Clip judge whether a moment works as a standalone short.

**Which scorer ships.** One: whichever of the baselines, logistic regression and LightGBM scores best on validation. It scores every window, and the same scores rank the candidates.

**Train and test.** Leave-one-streamer-out rotation over the five pilot streamers: each fold trains on 3 streamers, validates on 1 and tests on 1, and the folds rotate so every streamer is tested once. Fold 0 is the primary split. Model and threshold choices use validation only. Windows from one streamer never end up on two sides.

## 1. What the scorer predicts

The product uses **one scorer**. It scores every 10-second window of the VOD, and candidates come from its peaks (§ 3). When the VOD has viewer clips, they can add candidates, which the same scorer ranks.

| | |
|---|---|
| Unit | Window (10 s) → candidate moment |
| Input | Chat + audio features per window and its neighbors ([features](features.md)) |
| Output | Score per window → peaks → candidates |
| Label | `label_pos` from [labeling](../data/labeling.md#4-window-labels); positive windows are weighted by moment strength (relative views, [labeling § 6](../data/labeling.md#6-normalization-across-channels)) |
| Why ML is needed | No clips exist for un-clipped time or small channels, and views are only a rough proxy for "worth posting" |

### Training target

For clips that already exist, Twitch gives us their view counts, so **predicting Twitch views of old clips from features is not useful by itself.** The target is:

- **Independent discovery/scoring.** Learn from noisy viewer clips (weighted by views relative to the channel's usual clips) using chat and audio signals, then compare with the baselines and human judgments. Clip popularity is a proxy outcome, not objective highlight quality. The same scorer ranks generated candidates.

Not in the MVP:

- **Early ranking.** Predicting later clip strength from what is known at a recorded cutoff (e.g. stream end) needs a clip snapshot taken at that cutoff. The pilot ingests each VOD once, at least 7 days after the stream ([labeling § 7](../data/labeling.md#7-view-count-maturity)), so it has no such snapshot; a scheduled collector would come with channel tracking (#37).
- **Short-form performance.** Out of scope this semester ([PRD scope](../product/prd.md#scope-at-a-glance)). It would need a separate target built from external engagement data.

## 2. Model progression

Start with the baselines and a simple trained model. Add complexity only for a measured weakness. Compare on validation; a model need not win to be a valid experiment. Delivery continues alongside research.

| Step | Model | Purpose |
|---|---|---|
| Baselines | Random, chat spike, loudness, chat + loudness ([evaluation § 2](evaluation.md#2-baselines)) | References on the same population and budget |
| Logistic regression | On standardized features | Sanity check. Coefficients show which signals matter. |
| LightGBM | Gradient-boosted trees (`LGBMClassifier`) on the same window labels and weights | Strong tabular default. Handles non-linearity and missing values. |
| Stretch features | Pretrained audio events and Whisper speech features on the shortlist; video features | Measured as ablations on the chosen scorer |

The app ships whichever of these scores best on validation hit rate@10 ([evaluation](evaluation.md#3-top-10-metrics)), even if it's a baseline. Include genuine trained-ML work and meaningful signal ablations either way.

## 3. Candidate generation

Window scores → candidate moments. These rules apply to every scorer, baselines included:

1. Smooth scores over time (default: a rolling mean over 3 windows).
2. **Peak picking:** local maxima of the smoothed score.
3. **Non-max suppression:** drop peaks within `MIN_GAP_S` (default 60 s) of a higher one.
4. **Boundaries:** predictions trained against source-time labels already locate the source event. Start `PRE_S = 20` s before the peak window's start; end `POST_S = 10` s after the last consecutive window whose smoothed score stays at or above half the peak's. Then clamp the length to `[MIN_LEN_S, MAX_LEN_S] = [20, 45]` s (keeping the peak inside) and to VOD bounds. A raw chat-time heuristic converts its peak to source time once before this step; export never shifts it again.
5. **Shortlist and top 10:** keep the top `SHORTLIST_K = 50` candidates per VOD by score (ties by earlier start) as the shortlist. The top 10 of the shortlist are rendered as the vertical clips, and evaluation scores the same 10. The stretch Whisper features (#64) and the LLM rerank (#65) work on the shortlist; the rerank reads its top ~30 and picks the final 10.
6. **Viewer clips (product only):** a viewer-clip moment overlapping a discovered candidate marks it `source = both`; other moments become `viewer_clips` candidates, scored by the scorer's maximum window score over their span and cut to the same length rules around their highest-scoring window. For independent-discovery evaluation, hide clip metadata through scoring, boundary generation and selection; join ground truth only afterward.

All of these values are config parameters tuned on validation and recorded in each experiment's config.

**Implementation:** `autoclipper.models.candidates.generate_candidates(scores, duration_s=..., features=..., moments=..., params=CandidateParams(...))` returns contract `Candidate`s (`autoclipper.analysis`, the `AnalysisResult` models with the § 4.5 invariants). Choices the rules leave open:

- Each smoothed peak moves to the highest raw score within the smoothing span; a candidate's score is that window's raw score, and non-max suppression uses the same score. A plateau is a peak only if it's higher than both neighbouring runs, so a step on the way up isn't one.
- "Half the peak" is measured from the VOD's median smoothed score, because scores can be negative (sums of z-scores).
- A candidate cut short by the VOD's start or end is moved inside it, keeping its length where possible.
- Viewer-clip moments are matched against every surviving peak, not just the top 50, so a clipped peak just outside the shortlist comes back as `both` rather than `viewer_clips`. Clip-backed candidates are added after the top-50 shortlist, up to the contract's 200. A moment overlapping several discovered candidates marks each `both`; moments never merge into each other. A moment outside the scored windows is dropped with a warning.
- `top_signals` lists up to 3 standardized features (`chat_msg_rate_z`, `chat_unique_rate_z`, `chat_msg_rate_rz`, `chat_msg_rate_lead1/2`, `audio_rms_max_z`, `audio_rms_rz`) whose maximum over the candidate's windows is at least 1.0, strongest first.

## 4. Training practices

- **Splits:** leave-one-streamer-out rotation (3 train / 1 validation / 1 test per fold, fold 0 primary), saved to disk ([labeling § 8](../data/labeling.md#8-splits)). Model and threshold choices use validation only; never tune on test.
- **Class imbalance:** positives are rare among windows. Use class weights or focal loss, and evaluate with PR-AUC and ranking metrics, never accuracy.
- **Hyperparameter search:** small and budgeted (e.g. ≤ 30 trials, Optuna or random search) on validation. Record the search space in the run config.
- **Seeds:** every run takes a seed. Where training is stochastic, report mean ± std over ≥ 3 seeds.
- **Feature leakage checklist:**
  - Independent-discovery inputs exclude clip metadata and clip-derived context (counts, titles, views, clip-inferred categories); apply the versioned chat clip-announcement filter in [features](features.md).
  - Fit cross-dataset preprocessing on train only. Unsupervised normalization within the current full input VOD is allowed for offline inference; see [features § Normalization](features.md#normalization).
  - No information from after the prediction time in "live" experiments.
- **Explainability:** every window prediction and candidate carries `top_signals`, the features that contributed most ([data model § predictions](../architecture/data-model.md#predictions)); the app turns them into each clip card's one-line reason. Permutation importance or SHAP help with analysis. Avoid presenting an explanation as proof of quality.

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
