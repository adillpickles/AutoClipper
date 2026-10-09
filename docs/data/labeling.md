# Labeling: From Viewer Clips to Training Labels

| | |
|---|---|
| **Status** | Proposal (label rules `v0`). Changing these rules after data is labeled requires an ADR and a new dataset version. |
| **Last updated** | 2026-10-09 |

Viewer clips are our labels. They're free and plentiful, but **noisy**. This doc defines exactly how clips become labels so every model trains and is evaluated on the same thing.

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
4. **Cap moment length** at `MAX_MOMENT_S`. If a cluster grows beyond it (chained overlaps), split it at the largest internal gap.
5. A moment's interval is the union of its clips. Aggregate stats are defined in [data model § moments](../architecture/data-model.md#moments).

| Parameter | Proposed value | Rationale |
|---|---|---|
| `GAP_S` | 5 s | Clips of the same moment usually overlap heavily |
| `MAX_MOMENT_S` | 90 s | Long enough for a story beat, short enough for a short-form clip |

**Moment strength** (graded relevance), proposed: `n_clips` (number of independent clippers). Secondary: `log1p(total_views)` normalized per broadcaster (§6).

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

**Confident negatives (proposal):** to reduce PU noise, the default training set for window models uses as negatives only windows **at least `NEG_BUFFER_S` (60 s) from any moment**. Windows inside that buffer and outside any moment are excluded from training but **kept for evaluation**.

## 5. Chat lag

Chat reacts **after** the event: viewers see the stream with a delay, then type. Expect a lag of several seconds up to roughly 15 s. ⚠️ Estimate it from data by cross-correlating chat rate with moment starts.

- **Features handle lag, labels don't.** Labels stay in true VOD time (where the clip is). Chat features for window *t* include **lead** features from windows *t+1…t+k* (see [features](../ml/features.md)). For offline VOD analysis it's legitimate to look slightly ahead in chat.
- **Live mode** (stretch) can't look ahead, so it must use only past chat. Keep lead features in a separate, clearly named group so they can be dropped.
- **Export** shifts clip boundaries using the estimated lag plus padding (see [architecture § export](../architecture/overview.md#46-export)).

## 6. Normalization across channels

Raw view counts and chat rates differ by orders of magnitude between channels:

- **Views:** `views_norm = log1p(views) - median(log1p(views) for that broadcaster's clips in the training period)`
- **Chat/audio features:** z-scored against the same VOD's own distribution, or a rolling baseline (see [features](../ml/features.md)).
- Never normalize using statistics that include test-split data.

## 7. View-count maturity

Clip views are fetched at different ages. To compare like with like:

- The collector re-fetches a VOD's clips at a fixed age, proposed **7 days after the VOD's `created_at`**. This snapshot is the labeling source.
- If the 7-day snapshot is missing, use the nearest snapshot and record its age. Exclude it from view-based labels if it's younger than 3 days.

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
- Distribution of moment lengths. Count how many hit `MAX_MOMENT_S`.
- Chat coverage: % of windows with ≥ 1 message
- Positive rate per split (should be similar across splits)
- Manual spot-check: watch 10 random positives and 10 random confident negatives per dataset version, and record the findings in the dataset's README.
