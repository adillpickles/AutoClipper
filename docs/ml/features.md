# Feature Catalog

| | |
|---|---|
| **Status** | Proposal (feature set `v0`, ADR-0009). Candidate names/definitions become a contract only when adopted; implement the task's agreed subset. |
| **Last updated** | 2026-10-13 |

## Naming

`<family>_<signal>_<stat>`, all lowercase snake_case.

- **family:** `chat`, `audio`, `video`, `ctx` (context/metadata)
- **stat suffixes:** `_mean`, `_max`, `_std`, `_sum`, `_rate` (per second), `_z` (z-score vs. the VOD's baseline), `_rz` (z-score vs. a rolling baseline), `_lead{k}` (value from window *t+k*), `_lag{k}` (value from window *t−k*)

Adding a feature means adding a row to this file in the same PR. Changing a feature's definition means bumping `feature_version` for that family.

## Normalization

Channels differ hugely in chat volume and audio levels, so raw values rarely transfer:

- **`_z`:** `(x - mean_vod) / std_vod` over all windows of the same VOD. Fine offline, where the whole VOD is known.
- **`_rz`:** `(x - rolling_mean) / rolling_std` over the previous `R` windows (proposed `R = 30`, i.e. 5 minutes). Usable live.
- Guard against zero variance: if `std < eps`, set the z-score to 0.

Fit cross-dataset preprocessing (scalers, imputers, learned thresholds) on training data only. Per-input unsupervised VOD statistics are allowed offline even for a held-out VOD; do not pool held-out VODs or use their labels. Outcome transformations are evaluation/label operations, not inference inputs. Live experiments use only observations available at their stated prediction time.

## Chat preprocessing and leakage

Before **all** chat aggregates, proposed filtering removes whole messages containing Twitch clip URLs (`clips.twitch.tv/...` or `twitch.tv/<channel>/clip/...`) and recognized automated clip-created announcements, including any bot metadata linked to clip creation. Removing only URL text would leave giveaway message counts. Use the same versioned URL/announcement rules at training and inference, including lead/lag windows; record filtered counts without logging raw messages.

Ordinary reactions such as "clip it" remain eligible signals. Do not remove all bot messages or all occurrences of "clip". The initial announcement patterns must be explicit, synthetic-tested and ratified with the feature subset (OQ-15/ADR-0009); ablations can test ordinary reaction keywords separately. Clip-derived game/category information also stays out of independent-discovery inputs.

## Chat features (`features_chat`)

| Name | Definition | Notes |
|---|---|---|
| `chat_msg_rate` | Messages per second in the window | The strongest expected single signal |
| `chat_msg_rate_z` | `_z` of the above | |
| `chat_msg_rate_rz` | `_rz` of the above | Live-safe |
| `chat_unique_rate` | Unique chatters (by hash) per second | Robust to a few spammers |
| `chat_emote_rate` | Emotes per second (native + third-party where detected) | |
| `chat_emote_frac` | Emotes / total tokens | Reaction-heavy chat is mostly emotes |
| `chat_caps_frac` | Fraction of letters that are uppercase | Excitement |
| `chat_repeat_frac` | Fraction of messages that duplicate a message seen in the last 10 s | "Copypasta" / spam waves |
| `chat_mean_len` | Mean message length in characters | Reactions are short |
| `chat_keyword_rate` | Messages per second matching a small reaction lexicon (e.g. "clip it", "LOL", "W", "OMEGALUL") | Lexicon lives in `configs/`. It's language and community specific. |
| `chat_msg_rate_lead{1..3}` | `chat_msg_rate_z` of windows t+1…t+3 | **Offline only** (chat lag). Excluded in live experiments. |
| `chat_msg_rate_lag{1..3}` | Same for t−1…t−3 | Build-up context |

## Audio features (`features_audio`)

Computed from 16 kHz mono audio with librosa/NumPy. Frame-level values are aggregated per window.

| Name | Definition | Notes |
|---|---|---|
| `audio_rms_mean` | Mean RMS energy | Loudness |
| `audio_rms_max` | Max RMS energy | Spikes (yelling, explosions) |
| `audio_rms_max_z` | `_z` of `audio_rms_max` | |
| `audio_rms_std` | Std of RMS | Dynamics |
| `audio_onset_rate` | Onsets per second (librosa onset detection) | Busy-ness |
| `audio_spectral_centroid_mean` | Mean spectral centroid | Brightness (screams and laughter are brighter) |
| `audio_zcr_mean` | Mean zero-crossing rate | Noisiness |
| `audio_silence_frac` | Fraction of frames below an energy threshold | |
| `audio_is_muted` | 1 if the window overlaps a Twitch muted segment | Audio features are unreliable when 1 |
| `audio_rms_max_lead{1..2}` / `_lag{1..2}` | Neighbor context | |

**Stretch:** pretrained audio embeddings (e.g. a general audio-event model) for laughter/cheering detection, mean-pooled per window. These go in a separate family (`audio_emb_*`) so ablations stay clean.

## Video features (`features_video`), stretch only

| Name | Definition |
|---|---|
| `video_scene_cuts` | Scene changes in the window (PySceneDetect / FFmpeg scene score) |
| `video_motion_mean` | Mean frame-difference magnitude (low-res sampled frames) |
| `video_face_present` | Facecam face detected (for crop logic, and possibly as a signal) |

Video features require video download, which is far more expensive than audio. Sample at low resolution and low FPS.

## Context features (`ctx_*`)

| Name | Definition | Notes |
|---|---|---|
| `ctx_position_frac` | `start_s / duration_s` | Streams have arcs (start, mid, end) |
| `ctx_minutes_since_start` | `start_s / 60` | |
| `ctx_game_id` | Category at that time (categorical) | Only if derivable. Watch for leakage via clips. |

**Not inputs to independent discovery:** anything derived from clips (counts, titles, views or clip-inferred category). Product ranking may use available API information, but must be evaluated as that setup. Time-bounded early ranking needs a pinned inference snapshot under OQ-1; mature outcomes never feed its candidate formation or inputs.

## Moment-level features (v1)

For a moment spanning windows `[a, b]`: aggregate window features with `max`, `mean` and `argmax-position`, plus `moment_duration_s` and the build-up (mean of the 3 windows before `a`).
