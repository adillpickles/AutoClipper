# Feature Catalog

| | |
|---|---|
| **Status** | Adopted. Build the **Phase 1** features first (everything in the chat, audio and context tables below); Phase 2 and 3 come after the MVP works. |
| **Last updated** | 2026-11-28 |

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
| `chat_greeting_rate` | Messages per second with a greeting or check-in ("hi", "hello chat", "LATE", "first", HeyGuys) | chat-v2 (#192). Participation, not a reaction: stream starts fill with these. Lexicon category `greeting` |
| `chat_farewell_rate` | Messages per second with a goodbye ("bye", "gn", "cya", peepoBye) | chat-v2. Stream ends fill with these. Lexicon category `farewell` |
| `chat_distinct_frac` | Distinct messages (lowercased, whitespace-collapsed) / messages in the window | chat-v2. Low = chat repeats a shared reaction; high = everyone says something different |
| `chat_answer_frac` | Fraction of the window's messages that are **answers**: short (≤ 3 tokens, ≤ 25 characters), with a letter or digit, no native emote, no reaction/greeting/farewell/event match, and sent by nobody else in the window | chat-v2. Answer spam (chat-guessing games such as GeoGuessr) looks like hype by volume; this tells it from a shared reaction |

**Implementation:** `autoclipper.features.chat.compute_chat_features(windows, messages, lexicon=...)` returns `features_chat` (one Float64 row per window, never null, plus `feature_version`, now `chat-v2+lexicon-v2`). The reaction lists, event notices and clip-link patterns are in [`configs/lexicon.yaml`](../../configs/lexicon.yaml), loaded with `load_lexicon`; its `version` is part of `feature_version`. Choices the table leaves open:

- A message counts in the window whose `[start_s, end_s)` holds its offset; rates divide by the window's real length (a short final window too). Messages after the last window aren't counted.
- `chat_emote_frac` divides `n_emotes` by whitespace tokens (capped at 1); `chat_caps_frac` counts Unicode letters; `chat_repeat_frac` compares lowercased, whitespace-collapsed text with any chatter's message up to 10 s earlier, across window boundaries.
- Lexicon `emotes` match whole tokens in exact case, optionally followed by punctuation (`W` and `W!` are hype, `w` isn't); emoji match anywhere (`😂😂😂`). `words` match in any case on word boundaries, `patterns` are regexes. A VOD with no messages in any window is logged as a warning (quiet stream or missing chat).
- `chat_event_count` counts messages with bits plus subscription, gift and raid notices.
- `_rz` needs at least two earlier windows of the VOD, otherwise 0. Lead/lag values past the VOD's edges are 0.
- **chat-v2 (#192)** adds the last four rows of the table and changes no chat-v1 column: the new columns are appended to `FEATURE_COLUMNS`, so a model saved with chat-v1 features (its `features.json`) still finds every column it needs, and a dataset built with chat-v1 simply lacks the new ones. A dataset manifest lists the `feature_version`s it holds, so check it before comparing models across the bump.
- `greeting` and `farewell` are optional lexicon categories (a lexicon without them counts 0); `chat_greeting_rate` and `chat_farewell_rate` match like the reaction categories. Whole-message check-ins ("late", "im late", "early", "first", "here") count as greetings. The answer limits (3 tokens, 25 characters) are the lexicon's optional `answers: {max_tokens, max_chars}` section, so changing them changes the lexicon `version`.
- "Sent by nobody else in the window" compares the same normalized text as `chat_distinct_frac`. A message that is both short and a reaction ("lol") or an emote is never an answer, so a wave of laughter scores near 0 and a guessing game near 1. Ordinary busy chat on the demo VOD (2941757172) has `chat_answer_frac` around 0.4 (0.54 at the 90th percentile of windows with `chat_msg_rate_z > 2`), so on its own the column is a soft signal; the [ranking rules](modeling-plan.md#3-candidate-generation) use it only above 0.7.

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
| `audio_silence_frac` | Fraction of frames below an energy threshold | Near 1 = digital silence. Helix reports no muted segments for the pilot VODs, so music muted out of a VOD shows up only here ([pilot report § 10](../data/pilot-report.md#10-implications-for-36-and-38)); the default ranking down-weights such windows (#192) |
| `audio_rms_rz` | `_rz` of `audio_rms_max` (vs. the last 5 minutes) | A sudden jump matters more than being loud all stream |
| `audio_is_muted` | 1 if the window overlaps a Twitch muted segment | Audio features are unreliable when 1 |
| `audio_rms_max_lead{1..2}` / `_lag{1..2}` | Neighbor context | |

**Implementation:** `autoclipper.features.audio.compute_audio_features(windows, wav_path)` returns `features_audio` for one VOD (one Float64 row per window, never null, `feature_version` `audio-v1`). It uses NumPy only, not librosa: the features need RMS, zero crossings, an FFT and an onset envelope, and NumPy keeps the install light. Choices the table leaves open:

- The WAV is read in blocks (never whole) and cut into 64 ms frames every 32 ms; a frame belongs to the window holding its centre. Windows past the end of the audio are 0 and logged.
- `audio_spectral_centroid_mean` is in Hz (Hann-windowed magnitude spectrum). Onsets are peaks of the spectral-flux envelope (log spectrum clamped 80 dB below full scale; envelope scaled by its 99th percentile so muted stretches and silence edges don't suppress onsets elsewhere; local maximum over ±3 frames, at least 0.07 above the ±10-frame mean, at most one per 3 frames), following librosa's `onset_detect` peak picking. Silence is RMS below −60 dBFS. WAVs over 4 GiB (RF64, about 37 hours) aren't supported.
- `audio_rms_max_z` and `audio_rms_rz` divide by the standard deviation floored at −60 dBFS (`STD_FLOOR_RMS`) instead of zeroing below a tiny epsilon: near-constant stretches (muted or paused audio) would otherwise turn the next sound into a z-score in the thousands. A VOD where every window is alike still scores 0. Lead/lag columns are neighbouring windows' `audio_rms_max_z`, 0 past the edges.
- Runtime: about 2 s per 30 minutes of audio here (peak memory under 400 MB), so well under a minute for a 6-hour VOD.

## Phase 2 features (after the MVP works)

| Name | Definition | Notes |
|---|---|---|
| `audio_evt_laughter` | Max probability per window across YAMNet AudioSet laughter classes | Opt-in (`uv sync --extra audio-events`); off by default, not wired into the default scorer |
| `audio_evt_scream` | Max probability per window across YAMNet AudioSet scream/shout/yell classes | Same; runs on CPU |
| `audio_evt_cheer` | Max probability per window across YAMNet AudioSet cheer/applause/whoop classes | Same; runs on CPU |
| `audio_evt_music` | Max probability per window for YAMNet AudioSet `Music` | Same; runs on CPU |
| `speech_word_rate` | Spoken words per second inside the candidate span | Shortlist only (top N, default 50; #64). Opt-in (`uv sync --extra captions`) |
| `speech_exclaim_density` | Fraction of spoken words/tokens carrying `!` (clamped to `[0, 1]`) | Same; 0 when no words |
| `speech_question_density` | Fraction of spoken words/tokens carrying `?` (clamped to `[0, 1]`) | Same; 0 when no words |
| `speech_keyword_rate` | Spoken reaction keyword/phrase matches per second from `configs/speech_keywords.yaml` | Same; case-insensitive word-boundary match |
| `speech_frac` | Fraction of the candidate duration covered by merged spoken word intervals (`[0, 1]`) | Share of speech vs. silence (`1 - speech_frac` is the silence share) |
| `speech_peak_loudness_align` | `exp(-d / 2.0)` where `d` is the distance (s) from the loudest 64 ms audio frame to the nearest spoken word (`[0, 1]`) | `1.0` when peak loudness occurs during speech; `0.0` when silent or speechless |

**Implementation (`audio_evt_*`, #63):** `autoclipper.features.audio_events.compute_audio_event_features(windows, wav_path, ...)` runs Google's YAMNet (Apache-2.0, AudioSet-521) exported to ONNX via `onnxruntime` on CPU (optional extra `audio-events`, never a core dependency). Weights (`yamnet.onnx`, ~16 MB) are downloaded at runtime to `<artifacts_dir>/cache/audio_events/` and verified against a pinned SHA-256 digest. Feature version is `audio-events-v1`.

- **Class label mapping (`YAMNET_LABEL_MAP`):** each column takes the maximum probability across all 0.48 s YAMNet frames in the window and across all mapped AudioSet-521 classes:
  - `audio_evt_laughter`: `13` (`Laughter`), `14` (`Baby laughter`), `15` (`Giggle`), `16` (`Snicker`), `17` (`Belly laugh`), `18` (`Chuckle, chortle`).
  - `audio_evt_scream`: `6` (`Shout`), `7` (`Bellow`), `9` (`Yell`), `11` (`Screaming`).
  - `audio_evt_cheer`: `8` (`Whoop`), `58` (`Clapping`), `61` (`Cheering`), `62` (`Applause`).
  - `audio_evt_music`: `132` (`Music`).
- **Subset mode:** pass `window_ids` or `scores` + `top_k` (default `100`, configurable via `AUTOCLIPPER_AUDIO_EVENTS_TOP_K`) so a laptop CPU only reads and scores the top windows of a multi-hour VOD. Unselected windows remain in the output table with `null` values (preserving a 1-to-1 join on `window_id`; `logreg` imputes the training mean and LightGBM handles nulls natively). Selected windows past the end of the WAV get `0.0` and log a warning.
- **Opt-in only:** invoked explicitly through the standalone `autoclipper audio-events` CLI command or `compute_audio_event_features` / `extract_audio_event_features` (`AUTOCLIPPER_AUDIO_EVENTS_TOP_K` configures the default subset size when `--scores` is passed). Off by default and not wired into the default scorer or ranking; impact vs. Phase 1 baselines remains TBD pending dataset v1 (#40–#43).

**Implementation (`speech_*`, #64):** `autoclipper.features.speech.extract_speech_features(candidates, wav_path, ...)` transcribes only the top `top_n` shortlisted candidates (default `50`, configurable via `AUTOCLIPPER_SPEECH_TOP_N` or `--top-n`) — never the whole VOD — with a small padding window on each side (default `2.0 s`) to give Whisper clean boundary context. Model loading reuses `autoclipper.export.captions.transcriber_for` (`AUTOCLIPPER_WHISPER_MODEL`, default `base`, int8 on CPU) behind the optional `captions` extra (`uv sync --extra captions`). Without the `captions` extra, speech features are skipped with a clear warning instead of crashing. Feature version is `speech-v1+keywords-v<n>`, incorporating the `version` of [`configs/speech_keywords.yaml`](../../configs/speech_keywords.yaml).

- **Transcript cache (`autoclipper.features.transcript_cache`):** word-timed transcripts per candidate span are cached atomically under `data/interim/transcripts/<video_id>/<model>_<source_hash>.json`, keyed by the Whisper model name and the VOD's audio source identity (`<video_id>.source.json` for local media files from #138, or a size/mtime/sampled-SHA-256 fingerprint of the normalized WAV otherwise). Switching audio sources, modifying the local file, or changing the Whisper model invalidates the cache. Caption export (`autoclipper.export.captions.caption_lines_for` and `export_clip_with_captions`) reuses cached words whenever a cached span covers the exported clip interval, saving a second transcription.
- **Per-candidate exposure:** `Candidate.speech_features` (`autoclipper.analysis.SpeechFeatures`, optional and omitted from JSON when unset for backward compatibility) exposes the six computed features on `AnalysisResult` candidates so downstream explanations (#65) and rerankers can consume them. Candidates outside the top `top_n` shortlist stay in the Parquet output (`<artifacts_dir>/features/<video_id>_speech.parquet`) with `null` values and keep `speech_features=None` on `Candidate`.
- **Opt-in only:** invoked explicitly via `autoclipper speech-features <result.json>` (with `--update-result` to write `speech_features` back into the result JSON) or `autoclipper analyze <vod> --speech-features`. Off by default so default analysis is unaffected; evaluation impact vs. Phase 1 baselines remains TBD pending dataset v1 (#40–#43).

## Phase 3: Explanations and titles (#65)

`autoclipper.models.explain.explain_candidates` derives a deterministic short `title`, one-line `reason` and structured `signal_details` for every ranked candidate from the aggregate numeric signals already computed by the pipeline (chat rate spikes and lexicon reaction categories, audio loudness relative to the stream and recent minutes, viewer-clip counts, optional `audio_evt_*` probabilities when present, and speech-pause alignment). Raw chat text and usernames are never read or emitted. The scorer's ranking is final: explanations never reorder, drop or rescore candidates. An optional, off-by-default title polisher may polish the title text from the deterministic reason and aggregate stats only.

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
