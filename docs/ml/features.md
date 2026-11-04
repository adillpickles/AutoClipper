# Feature Catalog

| | |
|---|---|
| **Status** | Adopted. Build the **Phase 1** features first (everything in the chat, audio and context tables below); Phase 2 and 3 come after the MVP works. |
| **Last updated** | 2026-10-31 |

## Naming

`<family>_<signal>_<stat>`, all lowercase snake_case.

- **family:** `chat`, `audio`, `video`, `ctx` (context/metadata)
- **stat suffixes:** `_mean`, `_max`, `_std`, `_sum`, `_rate` (per second), `_z` (z-score vs. the VOD's baseline), `_rz` (z-score vs. a rolling baseline), `_lead{k}` (value from window *t+k*), `_lag{k}` (value from window *t−k*)

Adding a feature means adding a row to this file in the same PR. Changing a feature's definition means bumping `feature_version` for that family.

## Normalization

Channels differ hugely in chat volume and audio levels, so raw values rarely transfer:

- **`_z`:** `(x - mean_vod) / std_vod` over all windows of the same VOD. Fine offline, where the whole VOD is known.
- **`_rz`:** `(x - rolling_mean) / rolling_std` over the previous `R` windows (default `R = 30`, i.e. 5 minutes). Usable live.
- Guard against zero variance: if `std < eps`, set the z-score to 0.

Fit cross-dataset preprocessing (scalers, imputers, learned thresholds) on training data only. Per-input unsupervised VOD statistics are allowed offline even for a held-out VOD; do not pool held-out VODs or use their labels. Outcome transformations are evaluation/label operations, not inference inputs. Live experiments use only observations available at their stated prediction time.

## Chat preprocessing and leakage

Before **all** chat aggregates, filtering removes whole messages containing Twitch clip URLs (`clips.twitch.tv/...` or `twitch.tv/<channel>/clip/...`) and recognized automated clip-created announcements, including any bot metadata linked to clip creation. Removing only URL text would leave giveaway message counts. Use the same versioned URL/announcement rules at training and inference, including lead/lag windows; record filtered counts without logging raw messages.

Ordinary reactions such as "clip it" remain eligible signals. Do not remove all bot messages or all occurrences of "clip". The URL and announcement patterns live in a versioned file under `configs/` and are covered by synthetic tests; ablations can test ordinary reaction keywords separately. Clip-derived game/category information also stays out of independent-discovery inputs.

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
| `chat_laugh_rate` | Messages per second with a laugh emote or word (KEKW, OMEGALUL, LUL, LMAO, "LOL", 😂) | Funny moments. Lexicon in `configs/lexicon.yaml` |
| `chat_hype_rate` | Messages per second with a hype emote or word (POG, PogChamp, "W", "LETS GO", "!!!") | Hype moments |
| `chat_shock_rate` | Messages per second with a shock/confusion reaction ("?", monkaS, "WTF", "NO WAY", "D:") | Shocking moments |
| `chat_unique_rate_z` | `_z` of `chat_unique_rate` | Many people reacting, not a few spammers |
| `chat_event_count` | Subs, bits and raids announced in the window | Context; donations can trigger loud text-to-speech |
| `chat_msg_rate_lead{1..3}` | `chat_msg_rate_z` of windows t+1…t+3 | **Offline only** (chat lag). Excluded in live experiments. |
| `chat_msg_rate_lag{1..3}` | Same for t−1…t−3 | Build-up context |

**Implementation:** `autoclipper.features.chat.compute_chat_features(windows, messages, lexicon=...)` returns `features_chat` (one Float64 row per window, never null, plus `feature_version`, e.g. `chat-v1+lexicon-v1`). The reaction lists, event notices and clip-link patterns are in [`configs/lexicon.yaml`](../../configs/lexicon.yaml), loaded with `load_lexicon`; its `version` is part of `feature_version`. Choices the table leaves open:

- A message counts in the window whose `[start_s, end_s)` holds its offset; rates divide by the window's real length (a short final window too). Messages after the last window aren't counted.
- `chat_emote_frac` divides `n_emotes` by whitespace tokens (capped at 1); `chat_caps_frac` counts Unicode letters; `chat_repeat_frac` compares lowercased, whitespace-collapsed text with any chatter's message up to 10 s earlier, across window boundaries.
- Lexicon `emotes` match whole tokens in exact case, optionally followed by punctuation (`W` and `W!` are hype, `w` isn't); emoji match anywhere (`😂😂😂`). `words` match in any case on word boundaries, `patterns` are regexes. A VOD with no messages in any window is logged as a warning (quiet stream or missing chat).
- `chat_event_count` counts messages with bits plus subscription, gift and raid notices.
- `_rz` needs at least two earlier windows of the VOD, otherwise 0. Lead/lag values past the VOD's edges are 0.

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
| `audio_rms_rz` | `_rz` of `audio_rms_max` (vs. the last 5 minutes) | A sudden jump matters more than being loud all stream |
| `audio_is_muted` | 1 if the window overlaps a Twitch muted segment | Audio features are unreliable when 1 |
| `audio_rms_max_lead{1..2}` / `_lag{1..2}` | Neighbor context | |

**Implementation:** `autoclipper.features.audio.compute_audio_features(windows, wav_path)` returns `features_audio` for one VOD (one Float64 row per window, never null, `feature_version` `audio-v1`). It uses NumPy only, not librosa: the features need RMS, zero crossings, an FFT and an onset envelope, and NumPy keeps the install light. Choices the table leaves open:

- The WAV is read in blocks (never whole) and cut into 64 ms frames every 32 ms; a frame belongs to the window holding its centre. Windows past the end of the audio are 0 and logged.
- `audio_spectral_centroid_mean` is in Hz (Hann-windowed magnitude spectrum). Onsets are peaks of the spectral-flux envelope (local maximum over ±3 frames, at least 0.07 above the ±10-frame mean, at most one per 3 frames), as in librosa's `onset_detect`. Silence is RMS below −60 dBFS.
- `audio_rms_max_z` and `audio_rms_rz` divide by the standard deviation floored at −60 dBFS (`STD_FLOOR_RMS`) instead of zeroing below a tiny epsilon: near-constant stretches (muted or paused audio) would otherwise turn the next sound into a z-score in the thousands. A VOD where every window is alike still scores 0. Lead/lag columns are neighbouring windows' `audio_rms_max_z`, 0 past the edges.
- Runtime: about 5 s per 30 minutes of audio here, so roughly a minute for a 6-hour VOD.

## Phase 2 features (after the MVP works)

| Name | Definition | Notes |
|---|---|---|
| `audio_evt_laughter`, `audio_evt_scream`, `audio_evt_cheer`, `audio_evt_music` | Max probability per window from a pretrained audio-event model (PANNs or YAMNet) | Runs on CPU; if too slow for a whole VOD, run it on the windows of the shortlist only |
| `speech_rate`, `speech_exclaim_count`, `speech_keyword_count` | From a Whisper transcript of the shortlist (the top 50 candidates, [modeling plan § 3](modeling-plan.md#3-candidate-generation); #64) | Words per second, exclamations, reaction keywords |

## Phase 3: LLM rerank (after Phase 2)

An LLM (#65) reads the transcript and a short chat summary for each of the top ~30 candidates of the shortlist, picks the best 10 for a standalone short, and writes a one-line reason and a title for each. It reranks; it doesn't replace the scorer, and the evaluation reports the scorer with and without it.

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

**Not inputs to independent discovery:** anything derived from clips (counts, titles, views or clip-inferred category). Product ranking may use available API information, but must be evaluated as that setup.

The scorer works on windows only. A candidate's score is the scorer's window score at its peak, and a viewer-clip moment is scored by the maximum window score over its span ([modeling plan § 3](modeling-plan.md#3-candidate-generation)); build-up before a moment is covered by the `_lag` features.
