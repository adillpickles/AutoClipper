# Labeling: From Viewer Clips to Training Labels

| | |
|---|---|
| **Status** | Adopted as the starting rules (`v0`). Tune the numbers (window size, lag, buffers) from the pilot data and bump the version when they change. |
| **Last updated** | 2026-10-31 |

Viewer clips are **noisy, incomplete positive labels**: a clip says viewers cared about a stretch of the VOD; no clip doesn't mean nobody would. These rules turn clips into training and evaluation labels. Analyzing a new VOD never needs them.

---

## 1. What a viewer clip tells us (and what it doesn't)

| A clip **does** tell us | A clip **doesn't** tell us |
|---|---|
| At least one viewer found this stretch worth saving | That un-clipped time is boring (small audiences clip less) |
| Roughly where in the VOD it happened (`vod_offset`) | Exactly where the *moment* is inside the clip |
| How many people watched the clip on Twitch (`view_count`) | How it would perform as a standalone short on TikTok/Reels |
| When it was made (`created_at`) | Whether the clip was cut well |

Known biases to design around:

- **Clip-start bias.** Clips are created *after* something happens, from a buffer of recent stream. By default the clip covers the seconds leading up to the click, so the payoff is often in the **later** part of the clip. ⚠️ Verify this empirically: plot chat-rate peaks relative to clip start and end across many clips.
- **Duplicate clips.** Big moments get clipped many times with slightly different boundaries. That's a strength signal, but it's also duplication that must be merged.
- **Popularity bias.** Bigger channels produce more clips and views. Normalize per broadcaster.
- **Positive-unlabeled (PU) data.** Absence of a clip ≠ negative, especially in small streams.
- **View-count maturity.** Views keep growing for days, so compare clips at a consistent age.

## 2. From clips to moments

**Moment merging** clusters a VOD's clips into distinct moments:

1. Drop clips with null `vod_offset_s` or null `video_id` (count and log them).
2. Represent each clip as the interval `[vod_offset_s, vod_offset_s + duration_s]`.
3. Sort by start. Merge clips whose intervals **overlap or are within `GAP_S` seconds** of the current cluster.
4. **Long groups stay whole.** A long chain of overlapping clips is one moment for labeling. Clip length is handled separately: candidates are 20–45 s by default ([modeling plan § 3](../ml/modeling-plan.md#3-candidate-generation)) and exports can be trimmed up to 90 s.
5. A moment's interval spans the earliest member start through the latest member end, including gaps up to `GAP_S`. Aggregate stats are defined in [data model § moments](../architecture/data-model.md#moments).

| Parameter | Value | Rationale |
|---|---|---|
| `GAP_S` | 5 s | Clips of the same moment usually overlap heavily |
| Candidate length | 20–45 s default, configurable | Set at candidate generation; doesn't cap or split label moments |
| Export maximum | 90 s, configurable | Limits user trims; doesn't cap or split label moments |

`n_clips` counts **distinct clip IDs**, not distinct creators. A moment's strength comes from its clips' relative views (§ 6). Build moments from one pinned snapshot (the clip snapshot taken at ingest, § 7), never a mixture of observations.

Implemented in `autoclipper.labels.moments.merge_clips_into_moments` (`clip_ids` in start order, then by ID; a clip listed twice with the same interval counts once). Use half-open intervals `[start_s, end_s)` for overlap; touching edges alone don't overlap (but they are within `GAP_S`, so they merge). `moment_id` is `{video_id}:m{index}`, with `index` 0-based in `start_s` order; merged moments never overlap, so starts are unique within a VOD. Synthetic tests cover gaps, touching edges, chains and ties.

## 3. Windows

- The VOD is cut into fixed windows of `WINDOW_S` seconds with stride `STRIDE_S`.
- Default: `WINDOW_S = 10`, `STRIDE_S = 10` (non-overlapping). Overlapping windows are an experiment, not the default.
- The last partial window is dropped if shorter than `WINDOW_S / 2`.
- Windows overlapping a **muted segment** are flagged `is_muted` (audio features unreliable) and kept, not dropped.
- Implemented in `autoclipper.labels.windows` (`make_windows` for one VOD, `windows_for_vods` for a `vods` table). A window ends at the VOD's end if cut short there.

## 4. Window labels

| Label | Definition |
|---|---|
| `label_pos` | `True` if the window overlaps any moment interval |
| `label_n_clips` | Number of clips whose interval overlaps the window |
| `label_moment_id` | The overlapping moment with the most clips; ties go to the earlier `start_s` |

Positive windows are weighted in training by the strength of their `label_moment_id` moment (§ 6).

**Confident negatives:** train on observed positives and negatives **at least `NEG_BUFFER_S` (60 s) from any moment**. "Confident" is shorthand for this sampling rule, not verified absence of highlights. Buffer windows outside moments are excluded from training but kept in the window-level evaluation population. See [evaluation](../ml/evaluation.md#4-window-level-diagnostics); do not apply this training filter to evaluation silently.

Missing clip collection/ground truth is not `label_pos=False`. New-VOD inference uses unlabeled windows; annotated weak-label datasets require a pinned snapshot and coverage record ([data model](../architecture/data-model.md#windows)).

## 5. Chat lag

Chat reacts **after** the event: viewers see the stream with a delay, then type. Expect a lag of several seconds up to roughly 15 s. ⚠️ Estimate it from data by cross-correlating chat rate with moment starts.

- **Features handle lag, labels don't.** Labels stay in true VOD time (where the clip is). Chat features for window *t* include **lead** features from windows *t+1…t+k* (see [features](../ml/features.md)). For offline VOD analysis it's legitimate to look slightly ahead in chat.
- **Live mode** (future work, not this semester) can't look ahead, so it must use only past chat. Keep lead features in a separate, clearly named group so they can be dropped.
- **Source-time output:** viewer-clip boundaries and model predictions against these labels are already in VOD time. A heuristic locating a peak in chat time converts it once before creating candidates. **Export applies no chat-lag shift**; context padding and manual trimming are separate operations ([architecture § export](../architecture/overview.md#46-export)).

## 6. Normalization across channels

Raw view counts and chat rates differ by orders of magnitude between channels:

- **Views:** a clip's relative views are `rel_views = view_count / median(view_count)` over the channel's own clips in the same snapshot. A moment's strength is the `rel_views` of its most-viewed clip (`max_views` / channel median). Each channel is normalized by its own clips, so this works for held-out streamers too. Strength weights positive windows in training (the exact weighting, e.g. `log1p`, is an experiment config value) and sets graded relevance for NDCG@10 ([evaluation](../ml/evaluation.md)). It's a label quantity only, never an inference feature.
- **Chat/audio features:** z-scored against the same VOD's own distribution, or a rolling baseline (see [features](../ml/features.md)).
- Fit cross-dataset scalers/imputation and other learned preprocessing on training data only. Offline, unsupervised statistics from the current input VOD are available at inference and may normalize that VOD's signals. Never pool held-out VODs to fit preprocessing or use held-out labels to tune it.

## 7. View-count maturity

Clip views keep growing for days after a stream. To compare like with like:

- Pilot VODs are picked to be **at least 7 days old** (from the VOD's `created_at`) when they are ingested. The viewer-clip snapshot is taken once, at ingest (`autoclipper ingest`, #34/#35), and its capture time is recorded along with each clip's `created_at`. This fixes a minimum VOD age, not clip age: later-created clips have had less exposure.
- A VOD younger than 7 days at capture, or one whose snapshot is missing, is flagged in the manifest and left out of view-weighted labels and metrics. Never silently substitute another snapshot.
- Relative views (§ 6) compare a clip only with the channel's own clips in the same snapshot.

## 8. Splits

Adjacent windows are highly correlated, so **random window-level splits leak**. Rules:

1. **Leave one streamer out.** Each fold uses the five pilot streamers as 3 for training, 1 for validation and 1 for test. Test streamers are never seen in training. This measures generalization to new streamers, which is what the product needs.
2. **Rotate:** five folds, so every pilot streamer is the test streamer once. Fold 0 is the dataset version's primary split; the other folds are saved alongside it.
3. **Model and threshold choices use validation only.**
4. **Secondary temporal check:** within training channels, hold out each channel's most recent VODs.
5. Splits are generated once per dataset version with a fixed seed, saved to `data/splits/<dataset_version>.json`, and **never regenerated silently**.

**The test streamer's data is touched only for final reported numbers.** Day-to-day iteration uses validation.

## 9. Label rules versioning

All parameters above are bundled as `label_rules_version` (currently `v0`). When a change affects labels or existing results, increment the label and dataset versions and update this document with the implementation. Explain the change in the PR so earlier runs remain reproducible.

## 10. Quality checks (run on every dataset build)

- % of clips dropped for null `vod_offset_s` (alert if > 20%)
- Moments per VOD-hour, per channel (look for outliers)
- Distribution of moment lengths, including long connected groups
- Chat coverage: % of windows with ≥ 1 message
- Positive rate per split (should be similar across splits)
- Manual spot-check: watch 10 random positives and 10 random confident negatives per dataset version, and record the findings in the dataset's README.
