# Labeling: From Viewer Clips to Training Labels

| | |
|---|---|
| **Status** | Proposal (label rules `v0`, amended under ADR-0009). Ratify the target, grouping and evaluation contract before adoption; later changes require an ADR and a new dataset version. |
| **Last updated** | 2026-10-13 |

Viewer clips can supply **noisy, incomplete weak labels**. This proposal describes one label-building approach; they are not the sole definition of highlight quality. API candidates and inference signals remain separate from training/evaluation annotations.

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
4. **Overlong groups need a decision before implementation.** The earlier "split at largest gap" rule is withdrawn: an overlapping chain may have no gap, and membership was unspecified. Proposed alternative: retain the connected label group and limit export duration separately. If splitting is selected, the labeling ADR must define ties, all-overlap cases, membership and stable identifiers.
5. A moment's interval spans the earliest member start through latest member end, including any permitted small gaps. Aggregate stats are defined in [data model § moments](../architecture/data-model.md#moments).

| Parameter | Proposed value | Rationale |
|---|---|---|
| `GAP_S` | 5 s | Clips of the same moment usually overlap heavily |
| Export duration preset | 90 s example, configurable | Does not implicitly cap or split ground-truth groups |

`n_clips` means **distinct clip IDs**, not independent creators. Counting distinct pseudonymized creators would be a separate field/target with a privacy justification and ADR. Proposed graded relevance may use `n_clips` or views, but the target is unresolved (OQ-1); neither is objective highlight quality. Build groups from one pinned snapshot, never an implicit mixture of observations.

Use half-open intervals `[start_s, end_s)` for overlap; touching edges alone do not overlap. Before implementation, ratify group gap semantics, identifier format and ties in `label_moment_id` with synthetic examples (ADR-0009). The proposed largest-clip-count tie-break must not be left to row order.

## 3. Windows

- The VOD is cut into fixed windows of `WINDOW_S` seconds with stride `STRIDE_S`.
- Proposed: `WINDOW_S = 10`, `STRIDE_S = 10` (non-overlapping). Overlapping windows are an experiment, not the default.
- The last partial window is dropped if shorter than `WINDOW_S / 2`.
- Windows overlapping a **muted segment** are flagged `is_muted` (audio features unreliable) and kept, not dropped.

## 4. Window labels

| Label | Definition |
|---|---|
| `label_pos` | `True` if the window overlaps any moment interval |
| `label_n_clips` | Number of clips whose interval overlaps the window |
| `label_moment_id` | The overlapping moment with the most clips |

**Confident negatives (proposal):** train on observed positives and candidate negatives **at least `NEG_BUFFER_S` (60 s) from any moment**. "Confident" is shorthand for this sampling rule, not verified absence of highlights. Buffer windows outside moments are excluded from training but kept in the proposed all-window evaluation population. See [evaluation](../ml/evaluation.md#window-level); do not apply this training filter to evaluation silently.

Missing clip collection/ground truth is not `label_pos=False`. New-VOD inference uses unlabeled windows; annotated weak-label datasets require a pinned snapshot and coverage record ([data model](../architecture/data-model.md#windows)).

## 5. Chat lag

Chat reacts **after** the event: viewers see the stream with a delay, then type. Expect a lag of several seconds up to roughly 15 s. ⚠️ Estimate it from data by cross-correlating chat rate with moment starts.

- **Features handle lag, labels don't.** Labels stay in true VOD time (where the clip is). Chat features for window *t* include **lead** features from windows *t+1…t+k* (see [features](../ml/features.md)). For offline VOD analysis it's legitimate to look slightly ahead in chat.
- **Live mode** (stretch) can't look ahead, so it must use only past chat. Keep lead features in a separate, clearly named group so they can be dropped.
- **Source-time output:** viewer-clip boundaries and model predictions against these labels are already in VOD time. A heuristic locating a peak in chat time converts it once before creating candidates. **Export applies no chat-lag shift**; context padding and manual trimming are separate operations ([architecture § export](../architecture/overview.md#46-export)).

## 6. Normalization across channels

Raw view counts and chat rates differ by orders of magnitude between channels:

- **Views (unresolved):** the earlier training-period broadcaster-median formula lacks a value for unseen channels. Choose a target transformation and unseen-channel policy under OQ-1 before using normalized view labels. Within-VOD relevance is an alternative; no fallback is silently adopted. Outcome normalization is never an inference feature in independent discovery.
- **Chat/audio features:** z-scored against the same VOD's own distribution, or a rolling baseline (see [features](../ml/features.md)).
- Fit cross-dataset scalers/imputation and other learned preprocessing on training data only. Offline, unsupervised statistics from the current input VOD are available at inference and may normalize that VOD's signals. Never pool held-out VODs to fit preprocessing or use held-out labels to tune it.

## 7. View-count maturity

Clip views are fetched at different ages. To compare like with like:

- A later snapshot, proposed **7 days after the VOD's `created_at`**, may supply outcomes. This is a VOD-age observation, not equal clip ages: later-created clips have had less exposure. Record actual capture times and clip creation times.
- Missing or off-schedule snapshots are reported and excluded from fixed-horizon view comparisons unless a predeclared policy permits them. Do not silently substitute "nearest" observations; target horizon and any tolerance are OQ-1 decisions.
- An early-ranking experiment needs a separate real inference-time snapshot; day-one/day-seven observations cannot reconstruct stream-end views or candidates.

## 8. Splits

Adjacent windows are highly correlated, so **random window-level splits leak**. Rules:

1. **Split by broadcaster** (grouped). Test channels are never seen in training. This measures generalization to new streamers, which is what the product needs.
2. **Secondary temporal check:** within training channels, hold out each channel's most recent VODs.
3. Splits are generated once per dataset version with a fixed seed, saved to `data/splits/<version>.json`, and **never regenerated silently**.

Proposed proportions: 70 / 15 / 15 (train / val / test) by broadcaster. **The test split is touched only for final reported numbers.** Day-to-day iteration uses validation.

## 9. Label rules versioning

All parameters above are bundled as `label_rules_version` (currently `v0`, proposal). Changing any of them means a new version, a new dataset version, and an ADR or ADR update.

## 10. Quality checks (run on every dataset build)

- % of clips dropped for null `vod_offset_s` (alert if > 20%)
- Moments per VOD-hour, per channel (look for outliers)
- Distribution of moment lengths and overlong connected groups, under the adopted grouping rule.
- Chat coverage: % of windows with ≥ 1 message
- Positive rate per split (should be similar across splits)
- Manual spot-check: watch 10 random positives and 10 random confident negatives per dataset version, and record the findings in the dataset's README.
