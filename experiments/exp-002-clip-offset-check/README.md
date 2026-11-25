# exp-002: are viewer clips where Twitch says they are?

- **Issue:** #36 (follow-up to [exp-001](../exp-001-chat-lag-alignment/README.md))
- **Question:** exp-001 found chat and loudness peaking at or before viewer clips' reported start (Helix `vod_offset`). Is the reported position wrong, and by how much? Measured directly, by finding each clip's own audio in its VOD.
- **Primary metric + success criterion (set in advance):** the shift, true minus reported start, over strong matches. A match is strong at envelope correlation ≥ 0.6, a margin ≥ 0.1 over the best position more than 1 s away, and not at the search edge. A group's shift counts as consistent when its interquartile range is ≤ 10 s, and only then are clips moved to rerun exp-001.
- **Data:** 50 of the most-viewed clips of the 25 active pilot VODs, 10 per channel, 5 live and 5 post-stream each, at most 3 per VOD. Each clip was downloaded with TwitchDownloaderCLI `clipdownload` (360p) and its audio extracted to 16 kHz mono. Clip media and the per-clip table stay in the git-ignored `artifacts/`; `summary.json` holds aggregates only.
- **Git SHA:** `f440efd` (in `summary.json`)
- **Revision:** first run at `e645b56`; rerun at `f440efd` during review of #127 (downloads cached, same clips). The end residual now uses each downloaded clip's own audio length instead of the rounded metadata `duration_s`, which moved it from +0.66 s to +0.73 s; the metadata version is kept beside it and drives the correction, so the rerun of exp-001 below is unchanged. Min/max values were added so every number here is in `summary.json`.
- **Seeds:** 42 (resampling)
- **Command:** `uv run python scripts/clip_offset_check.py --config experiments/exp-002-clip-offset-check/config.yaml`. Downloads are cached; a rerun only matches.

## Method

Code: `autoclipper.labels.clip_offsets` and `scripts/clip_offset_check.py`.

- **Matching.** Clip and VOD audio as 10 ms log-energy envelopes. The two are differently encoded copies of the same stream, so their loudness shape matches even where waveforms don't. The clip's envelope is correlated (Pearson) with every position within 120 s of the reported start; the best position is the clip's true start.
- **Skipped clips.** Clips overlapping a muted segment Helix reports (there are none in the pilot), or whose VOD audio is more than half silent over the reported span (2 clips, replaced by the next most-viewed).
- **True end.** The true start plus the downloaded clip's own audio length (the metadata `duration_s` is rounded, and cut at the VOD's end when placed).
- **Live and post-stream.** Created before or after the VOD's end.
- **Intervals.** 95 %, resampling VODs.

## Results

All 50 clips matched strongly (10th-percentile correlation 0.98; no weak match, no match at the search edge).

| Shift (true minus reported start) | Median (95 % CI) | IQR | Consistent? |
|---|---|---|---|
| All 50 clips | −29.4 s (−32.0 to −25.6) | 28.2 s | no |
| Live (25) | −29.5 s (−30.0 to −26.3) | 12.0 s | no |
| Post-stream (25) | −28.0 s (−52.1 to −17.5) | 35.1 s | no |

By the criterion set in advance, the shift is **not** consistent: it is large everywhere (every clip's true start is 10–62 s before the reported one) but spread out. Looking at the clips (post hoc) shows why: the shift is almost exactly minus the clip's own length. A 60 s clip shifts by about −60 s, a 30 s clip by −30 s, a 14 s clip by −14 s (line fit: slope −0.93, r = −0.97). **The reported position marks where the clip ends.**

| End residual (true end minus reported position) | Median (95 % CI) | IQR | Within 2 s of the median |
|---|---|---|---|
| All 50 clips | **+0.73 s** (0.43 to 0.96) | 1.2 s | 80 % |
| Live (25) | +0.48 s (0.13 to 0.62) | 0.7 s | 88 % |
| Post-stream (25) | +1.00 s (0.80 to 1.79) | 1.3 s | 76 % |
| Per channel | +0.18 to +1.07 s | 0.4 to 5.8 s | 60 to 90 % |
| *With metadata lengths (used for the correction):* all / live / post-stream | +0.66 s (0.36 to 0.93) / +0.42 s / +0.94 s | 1.1 / 0.7 / 1.5 s | 78 / 88 / 72 % |

This reading is consistent by the same 10 s criterion in every group. End residuals range from −4.0 s to +18.1 s (−12.9 s to +18.1 s with metadata lengths); the widest spread is IShowSpeed's (IQR 5.8 s). With true positions, live clips are created after their content ends (median 55 s later), as they must be; with the reported positions most of them weren't (exp-001).

### exp-001 rerun with corrected positions

Every clip moved back by its metadata length plus its class's median metadata end residual (+0.42 s live, +0.94 s post-stream), then re-placed and re-merged (2,268 moments instead of 2,169). Same settings and cached series as exp-001. Lifts average the VODs with at least one spike of the kind (13 for chat, 15 for audio).

| Measure | Reported positions | Corrected positions |
|---|---|---|
| Top moments with **neither** a chat nor an audio spike (25 VODs) | 0.77 (0.66 to 0.87) | **0.76** (0.66 to 0.86) |
| Top moments with a chat spike / audio spike | 0.18 / 0.08 | 0.19 / 0.08 |
| Chat-spike lift, shifted 12 s / unshifted (13 VODs) | 1.33 / 1.67 | **2.00** (1.31 to 2.83) / 2.00: ×1.5 / ×1.2 |
| Audio-spike lift (15 VODs) | 0.89 | 0.92 |
| Chat peak around moment starts (all 2,169 / 2,268 moments) | −1 s (−7 to +1) | **+26 s** (+22 to +34) |
| Loudness peak around moment starts (all moments) | −3 s | +16 s (+14 to +46) |
| Loudness peak around top-clip starts (all 125 top clips; exp-001 also shows −22 s for the 101 live ones) | −9 s | **+10 s** (+7 to +22) |
| Loudest second of a top clip (125): before / inside / after (chance 34 / 32 / 34 %) | 47 / 31 / 22 % | 29 / **42** / 29 % |

## Conclusion

Supported: Helix `vod_offset` is the clip's **end** (to about a second), not its start, for live and post-stream clips on every pilot channel. True start ≈ `vod_offset − duration` (+0.7 s). With that correction the clips line up with the signals as labeling § 1 expected: the streamer's loud moment is inside the clip, chat peaks about 12 s after it, and chat spikes are twice as likely near a moment as chance (lift 2.00 over 13 VODs, up ×1.5 from 1.33). The 77 % of top moments without a spike does **not** change: at a VOD z-score of 3, spikes are simply rare, whatever the clip positions.

## Notes and caveats

- The end-anchored reading was found after looking at the per-clip shifts; the criterion set in advance (a consistent constant shift) failed. It was then checked with the same consistency rule and applied only because every group passed it.
- 50 clips, the most-viewed ones. Less-viewed clips aren't checked; nothing here suggests they differ, but that's untested.
- The match finds where the clip's *audio* is; a clip whose audio differs from the VOD's (muted later, music removed) would match weakly, and none did.
- A little residual spread is expected: the envelope resolution is 10 ms, and a clip's audio can run a little past its video. Metadata lengths add more (they are rounded), which is why the downloaded length is used for the end residual.
