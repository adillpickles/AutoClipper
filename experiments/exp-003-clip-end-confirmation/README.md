# exp-003: confirming that `vod_offset` is the clip's end

- **Issue:** #36 (confirmation of [exp-002](../exp-002-clip-offset-check/README.md))
- **Question:** exp-002 found, after looking at its clips, that Helix `vod_offset` marks where a clip ends. Does that hold on clips it didn't see, including ordinary low-view ones?
- **Rule and pass criteria (set in advance):** rule: true clip end = `vod_offset_s`. **PASS** when, over strong matches, the median |true end − `vod_offset_s`| is ≤ 2 s **and** at least 70 % of clips are within 2 s; otherwise FAIL. Strong match as in exp-002. The config holding them was committed (`dce2c70`) before any exp-003 clip was selected or downloaded; the code came after (`372b48c`) and the config is unchanged.
- **Data:** 50 clips of the 25 active pilot VODs, none used in exp-002 (its 50 clips were excluded by ID; overlap checked: 0). 10 per channel in four cells: top clips (the most-viewed remaining; median 477 views) and ordinary low-view clips (at or below the channel's median views, drawn at random; median 1 view, at most 2), each live and post-stream, 25 of each kind overall. 7 candidates were skipped for silent VOD audio at either reading of their position; Helix reports no muted segments. Matching code and settings from exp-002. Clip media and the per-clip table stay in the git-ignored `artifacts/`; `summary.json` holds aggregates only.
- **Git SHA:** `372b48c` (in `summary.json`)
- **Pre-registration evidence:** after the rebase, the config commit is ce9b881 and the code commit is 0a92253 (previously dce2c70 and 372b48c). Main gets one squashed commit, so the ordering is preserved in PR #127's commit list (refs/pull/127/head).
- **Seeds:** 42 (low-view draw, resampling)
- **Command:** `uv run python scripts/clip_end_confirmation.py --config experiments/exp-003-clip-end-confirmation/config.yaml`

## Result: PASS

All 50 clips matched strongly (no weak match).

| Criterion | Required | Measured (95 % CI, VODs resampled) | |
|---|---|---|---|
| Median \|true end − `vod_offset_s`\| | ≤ 2 s | **0.69 s** (0.34 to 1.18) | met |
| Share within 2 s | ≥ 70 % | **80 %** (67 to 91) | met |

Descriptive breakdowns (no criteria):

| Group | Clips | Median signed (true end − `vod_offset_s`) | Median \|error\| | Within 2 s |
|---|---|---|---|---|
| Top clips | 25 | +0.34 s | 0.58 s | 80 % |
| Low-view clips | 25 | +0.42 s | 0.75 s | 80 % |
| Live | 25 | +0.08 s | 0.37 s | 80 % |
| Post-stream | 25 | +0.80 s | 0.93 s | 80 % |
| Per channel | 10 each | +0.17 to +0.97 s | 0.26 to 1.22 s | 70 to 90 % |

Shift against clip length: slope −0.93, r = −0.95, as in exp-002.

The 10 clips more than 2 s off range from −34 s to +11 s; four of them are short (7–8 s) and one, a 7 s Tyler1 clip made after the stream, is 34 s off. The 90th percentile of the error is 5.4 s.

## Conclusion

The rule passes as written on 50 new clips, top and low-view, live and post-stream alike: Helix `vod_offset` is the clip's end. A clip's content is `[vod_offset_s − duration_s, vod_offset_s]`, to within about a second for most clips and a few seconds for one in five.

## Notes and caveats

- The lower end of the 95 % interval for the share within 2 s (67 %) dips below 70 %; the criterion was set on the point estimate, which is how it's reported.
- One in five clips is more than 2 s off, often short ones; edited or re-trimmed clips may explain some. A label rule built on this reading should tolerate a few seconds (the 10 s windows and the ±15 s evaluation match do).
- 50 clips from five large channels and 25 VODs; other channels and later Twitch changes aren't covered.
