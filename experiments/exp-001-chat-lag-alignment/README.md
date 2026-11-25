# exp-001: chat delay and clip alignment on the pilot VODs

- **Issue:** #36
- **Question:** How late does chat react to what happens on stream, and how often do chat and audio spikes line up with viewer clips? What does that mean for the window size, how far ahead to look at chat, and clip padding?
- **Primary metric + success criterion (set in advance):** none. This is an exploratory spike: every number is an estimate with a 95 % interval from resampling VODs, and nothing passes or fails.
- **Deviation from #36:** the issue asks for the chat delay *from chat peaks vs. clip starts*. The delay is measured around the streamer's loud moments instead, which needs no clips: clip positions turned out to be unreliable as event times (this run's clip timing, then exp-002: they are clip ends). Chat around clip starts is reported too, under "Where clips sit".
- **Data:** the 25 active pilot VODs in `configs/pilot_vods.txt` (5 channels × 5, 103.44 VOD-hours, SHA-256 in `summary.json`), one clip snapshot per VOD, pseudonymized chat, 16 kHz audio. The historical zero-clip VOD is excluded by the config. No dataset version yet (#40).
- **Git SHA:** `f440efd` (in `summary.json`, with the config, list and lexicon hashes)
- **Revision:** first run at `44ad906`; rerun at `f440efd` during review of #127 to record every number this README cites in `summary.json` (the sensitivity table and the per-VOD peak-correlation range were only in `artifacts/` before). No existing value changed.
- **Seeds:** 42 (resampling and random anchors)
- **Command:** `uv run python scripts/pilot_alignment.py --config experiments/exp-001-chat-lag-alignment/config.yaml`, with `AUTOCLIPPER_DATA_DIR` pointing at the pilot data. A rerun gives a byte-identical `summary.json`. Plots: [`notebooks/2026-11-25-hm-chat-lag-alignment.ipynb`](../../notebooks/2026-11-25-hm-chat-lag-alignment.ipynb).

## Method

Code: `autoclipper.labels.alignment` (estimators) and `autoclipper.labels.alignment_study` (the study). Everything is in VOD time.

- **Signals.** Per second: chat messages (clip-link messages removed, as in the chat features) and the loudest 64 ms audio frame, built with the feature code itself. Chat is `log1p`, loudness is dBFS; both have a 5-minute moving average removed and are z-scored per VOD.
- **Profiles.** The average signal from 60 s before to 90 s after an anchor, averaged per VOD and then over VODs, smoothed over 5 s. The peak is searched from −30 to +60 s. The interval comes from resampling VODs (1,000 times); the chance level is the 95th percentile of the peak for random anchors (as many per VOD, 200 draws).
- **Anchors.** *Loud events*: each VOD's loudest 0.5 % of seconds, at least 60 s apart (369 events). They need no clips. *Moment starts* (2,169) and the starts and ends of each VOD's 5 most-viewed clips (125), split into live clips and clips made after the stream.
- **Spikes.** 10 s windows with the features' VOD z-scores (`chat_msg_rate_z`, `audio_rms_max_z`) at or above 3, at least 60 s apart. A spike matches a moment within 15 s (as hit rate@10 does). *Lift* = the share of spikes near a moment divided by the share of all windows near one. Chat spikes are shifted back by the pooled chat delay unless marked unshifted. Means over VODs.
- **Clip timing.** For live clips (created before the VOD ended): creation time on the VOD timeline (`created_at` minus the VOD's `created_at`) against the reported position (`vod_offset`).

Clips starting at or after the VOD's end (9) are dropped and clips running past it (19) are cut at the end.

## Results

### Chat delay

| Estimate | Value (95 % CI) | Chance level | n |
|---|---|---|---|
| Chat peak after a loud event, all VODs | **+12 s** (3 to 16 s), height 0.26 z | 0.12 z | 369 events, 25 VODs |
| Chat above half its peak height | −1 to +24 s after the loud event | | |
| Tyler1 alone | +6 s (2 to 13 s), height 0.51 z | 0.20 z | 127 events |
| Other channels alone | peaks from +4 to +24 s, intervals wide; KaiCenat and IShowSpeed don't clear chance | | 39–91 events each |
| Whole-VOD cross-correlation (chat after loudness), median of VOD peaks | +4 s (3 to 14 s) | | 25 VODs, 2 peaks at the search edge |

The whole-VOD correlation is weak (peak correlations of 0.01–0.10). CaseOh's VODs give a median of −20 s, which isn't physical: on talk-heavy streams loudness carries little event signal. The loud-event profile is the main estimate.

### Where clips sit (around the reported position)

| Signal | Anchor | Peak (95 % CI) | Above half height | n |
|---|---|---|---|---|
| Chat | moment start | **−1 s** (−7 to +1), 0.25 z (chance 0.05) | −16 to +15 s | 2,169 |
| Chat | top-clip start | −2 s (−10 to +6), 0.42 z (chance 0.16) | −17 to +11 s | 125 |
| Loudness | moment start | **−3 s** (−23 to −2), 0.09 z | −25 to +2 s | 2,169 |
| Loudness | top-clip start, live clips | −22 s (−23 to +48), 0.22 z | −23 to −19 s | 101 |

Post-stream clips (182 moments, 24 top clips) are too few for a clear profile; their intervals span most of the window.

### Clip timing (live clips, no audio or chat needed)

| | Value |
|---|---|
| Live clips (created before the VOD ended) | 7,647 |
| Post-stream clips (left out of this table) | 5,121 |
| Created after the reported **start**, median | 16 s (p25 2 s, p75 52 s) |
| Created after the reported **end**, median | −18 s (p25 −35 s, p75 20 s) |
| 30 s clips (4,197): created after start / after end, medians | 6 s / −24 s |
| 60 s clips (1,262): created after start / after end, medians | 26 s / −34 s |
| Created **before** the reported end | 65 % |
| Created 0–10 s after the reported start | 19 % |

A clip made during a stream can only hold what had already happened, so its content should end no later than its creation (plus the viewer's stream delay of a few seconds). For two thirds of live clips the reported end is after the creation time.

### Spike alignment (10 s windows, z ≥ 3)

| Metric | Mean over VODs (95 % CI) | VODs averaged |
|---|---|---|
| Chat spikes / audio spikes per VOD-hour | 1.7 (0.9 to 2.4) / 1.2 (0.6 to 1.8) | 25 |
| Windows within 15 s of a moment (chance) | 0.67 (0.56 to 0.76) | 25 |
| Chat-spike lift, shifted by 12 s / unshifted | 1.33 (1.00 to 1.75) / **1.67** (1.14 to 2.31) | 13 (with a chat spike) |
| Audio-spike lift | 0.89 (0.64 to 1.11) | 15 (with an audio spike) |
| Chat spikes within 15 s of an audio spike, lift | 4.2 (0.93 to 7.7): **inconclusive** | 7 (with both) |
| Top moments with a chat spike / audio spike / both / **neither** | 0.18 / 0.08 / 0.04 / **0.77** | 25 |

A lift is defined only for a VOD with at least one spike of the kind, so the lifts average fewer VODs than the shares. The chat-near-audio lift rests on 7 VODs and its interval includes 1: chat and audio spikes may coincide more than chance, but this run can't show it.

CaseOh has no chat window at z ≥ 3 (busy, steady chat); Tyler1, where moments cover only 20 % of windows, has the clearest chat lift (1.85 shifted, 2.70 unshifted).

Sensitivity (all VODs; every setting is under `sensitivity` in `summary.json`):

| Window | z | Chat lift (unshifted) | Audio lift | Top moments with neither |
|---|---|---|---|---|
| 5 s | 3 | 1.16 (1.60) | 1.09 | 0.71 |
| 10 s | 2 | 1.18 (1.34) | 1.18 | 0.45 |
| 10 s | 3 | 1.33 (1.67) | 0.89 | 0.77 |
| 15 s | 3 | 1.50 (1.86) | 0.99 | 0.77 |
| 20 s | 3 | 1.42 (1.58) | 1.00 | 0.78 |

### Padding inputs (5 most-viewed clips per VOD, 125 clips)

| Peak | Before the start | Inside | After the end | Distance outside (median, p90) |
|---|---|---|---|---|
| Loudest second (searched ±30 s) | **47 %** (chance 34 %) | 31 % (32 %) | 22 % (34 %) | before: 20 s, 29 s (capped at 30); after: 15 s, 25 s |
| Chat peak shifted back 12 s (−30/+60 s) | 58 % (26 %) | 23 % (25 %) | 18 % (50 %) | before: 23 s, 39 s; after: 29 s, 44 s |

## Conclusion

The documented expectation for chat lag (labeling § 5: several seconds up to roughly 15 s) is supported, with one surprise. Chat follows the streamer's loudest moments by about 12 s (95 % CI 3–16 s) and stays up for about 25 s, which the current `lead{1..3}` chat features (30 s) cover. The surprise: chat and loudness peak at or before the clips' **reported** start, and two thirds of live clips were created before their reported end. Helix's `vod_offset` therefore looks 10–25 s later than the clip's content, or the VOD's `created_at` is later than its first second (superseded by exp-002: it is the clip's end, one clip length late). Either way, viewer-clip intervals used as labels sit after the event they mark. Spikes at z ≥ 3 are rare and line up with clips only weakly: three quarters of top moments have neither spike, and audio spikes are no likelier near a moment than any window.

## Notes and caveats

- **Follow-up: [exp-002](../exp-002-clip-offset-check/README.md)** matched 50 clips' own audio against their VODs: `vod_offset` is the clip's *end* (+0.7 s), not its start. With corrected positions the clip-position results above reverse (the loud moment falls inside the clip) and the shifted chat-spike lift rises ×1.5 (1.33 to 2.00, 13 VODs); the share of top moments without a spike doesn't change.
- **Not verified on video.** The pilot downloaded audio only. Watching a sample of top clips next to the VOD at `vod_offset` (the #40 spot check) is the way to tell an offset in Twitch's positions from clips that start after the event.
- The clip-timing check takes the VOD's `created_at` as its first second; if Twitch records it late, the shift belongs to the VOD timeline, not the clips.
- Loud events stand in for "something happened"; they miss quiet highlights and include loud non-events (music, shouting at nothing).
- Moments cover 67 % of windows on average (20 % for Tyler1), so near-moment shares mean little without the chance level.
- Per-channel estimates rest on 5 VODs each, and smoothing limits peak positions to about ±2 s.
- Live and post-stream clips are told apart by the VOD's end time; a clip made from the VOD while the stream was still live counts as live.
