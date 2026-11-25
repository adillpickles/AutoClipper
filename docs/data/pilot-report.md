# Pilot Data Report

| | |
|---|---|
| **Status** | Pilot collection complete (#35). Exploration findings (#36) still to come. |
| **Last updated** | 2026-11-24 |
| **Data** | 25 active VODs in [`configs/pilot_vods.txt`](../../configs/pilot_vods.txt), collected 2026-11-06 |
| **How to check** | `uv run python scripts/pilot_report.py --check docs/data/pilot-report.md` |

The pilot collected chat, audio and a viewer-clip snapshot for five VODs of each channel in [`configs/channels.yaml`](../../configs/channels.yaml), so the dataset, the baselines and the models can work on real data. It also measured what collection costs before anyone scales it up ([collection plan](collection-plan.md#targets)).

**The tables between `<!-- BEGIN pilot-report:… -->` and `<!-- END … -->` markers are generated** by [`scripts/pilot_report.py`](../../scripts/pilot_report.py) from the data directory: the catalog, the clip snapshots, the chat tables, the audio files and the runner's records. Nobody types those numbers in. `--check` recomputes every table and exits 1 if one differs, which is how to confirm that the report matches the data ([§ 13](#13-reproducing-this-report)). Numbers in the prose come from the same run (the tables or the script's `--json` output) unless a sentence names another source.

## 1. Summary

- **25 of 25 VODs collected** (5 per channel, 103.44 VOD-hours), every one complete on its first attempt: metadata, clip snapshot, chat and audio all `ok`. Every integrity check passes ([§ 12](#12-integrity-checks)).
- **12,777 viewer clips**, all with a VOD offset, none in a clip-search range that hit Twitch's limit, every snapshot complete and taken at least 7 days after the stream.
- **1,703,046 chat messages**, chat download success 25/25, authors pseudonymized, no unredacted @mentions.
- **Cost:** about 2.1 minutes of ingest and 220 MB of disk per VOD-hour (104 MB if the WAVs aren't kept). The audio download takes two thirds of the time.
- **For labeling (#38, #40):** under the `v0` label rules, **more than half of all windows would be positive** and some VODs would have no training negatives ([§ 10](#10-implications-for-36-and-38)). #38 (merged 2026-11-07) kept the binary rules at `v0` and added view-relative weights for positives (`one-plus-log1p-relviews-v1`, [labeling § 4](labeling.md#4-window-labels)). The weights make low-view moments count less in training, but they don't change which windows are positive, so the positive rule is still open. Dataset v1 (#40) pins the rule version, so the decision is needed before it is built.

## 2. Purpose and method

**Selection.** [`scripts/select_pilot_vods.py`](../../scripts/select_pilot_vods.py) listed each channel's past broadcasts through Helix (metadata only) and saved each listing as a timestamped selection file that is never overwritten. It applies hard rules to every VOD: a past broadcast of the channel, longer than zero, and **at least 7 days old** (the snapshot maturity rule, [labeling § 7](labeling.md#7-view-count-maturity)). Preferences only rank the eligible VODs: 7–30 days old, 2–12 h long, under 20 % muted, English, low expiry risk. Unknown values count as misses. The picks are spread over time. Re-running `show` on a saved file recomputes the same picks.

**Channels.** `flight23white` (FlightReacts) had no viewer clips at all in the pilot month, so its VODs would give no labels. It was replaced by `loltyler1` before collection continued ([collection plan](collection-plan.md#channels-and-vods)). The one `flight23white` VOD already ingested is kept as a historical zero-clip case ([§ 11](#11-historical-vod)). Tyler1's backups were adjusted by hand (one promoted, one demoted) and recorded as overrides in its selection file. They don't affect the 25 primaries.

**Collection.** [`scripts/run_pilot.py`](../../scripts/run_pilot.py) ran `autoclipper ingest` once per VOD, one VOD at a time, oldest first, in a child process tree with a timeout and disk and memory guards ([collection plan § Ingest](collection-plan.md#ingest)). Every run used zero retries, so every failure would have been visible, and none happened. From the third run on, a VOD was started only when the machine had at least 7 GiB of memory available. That was an operating rule for this machine, not something the runner checks.

**Environment.** One Windows 10 laptop on a home connection. TwitchDownloaderCLI 1.56.5 (chat), yt-dlp 2026.08.19 (audio) and FFmpeg 9.0.2 (the build CI pins). Helix requests used a Twitch developer app registered for the pilot, not the team app described in [setup](../development/setup.md#twitch-credentials).

## 3. Issue #35 metrics

<!-- BEGIN pilot-report:issue-metrics -->
| Metric (issue #35) | Value |
|---|---|
| Download time (ingest wall time, all steps) | 13,243 s (3.68 h) for 103.44 VOD-hours: 2.13 min per VOD-hour |
| Audio download alone | 8,811 s (67 % of the wall time) |
| Disk use (active pilot outputs) | 22.69 GB: raw audio 10.71 GB, WAV 11.92 GB, chat 55.0 MB, clips 1.4 MB |
| Disk use per VOD-hour | 219.3 MB (raw audio only: 103.6 MB) |
| Chat success rate | 25 / 25 VODs (100 %) |
| Clips per VOD-hour | 123.5 overall (12,777 clips); per VOD 6.6–338.2 |
| Clips without a VOD offset | 0 of 12,777 (0.0 %) |
| Clip-search ranges that hit the clip limit | 0 of 1,864 ranges queried |
<!-- END pilot-report:issue-metrics -->

"Download time" is the whole `autoclipper ingest` run per VOD: metadata, clip search, chat download and parse, audio download and normalization. The audio download alone is shown separately. Disk use counts the outputs of the 25 active VODs; [§ 8](#8-storage) has the whole data directory.

## 4. Dataset composition

<!-- BEGIN pilot-report:channels -->
| Channel | VODs | Hours | Clips | Clips/h | 1-view clips | Chat messages | Messages/h | Unique chatters | Ingest s |
|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| caseoh_ | 5 | 20.27 | 2,494 | 123.0 | 53 % | 553,860 | 27,325 | 55,687 | 2,846 |
| ishowspeed | 5 | 15.90 | 3,039 | 191.2 | 51 % | 206,342 | 12,980 | 33,370 | 1,765 |
| kaicenat | 5 | 21.55 | 4,615 | 214.2 | 48 % | 454,650 | 21,099 | 64,745 | 2,466 |
| jynxzi | 5 | 25.93 | 2,443 | 94.2 | 74 % | 419,127 | 16,165 | 53,429 | 3,791 |
| loltyler1 | 5 | 19.80 | 186 | 9.4 | 34 % | 69,067 | 3,488 | 7,824 | 2,376 |
| **all** | 25 | 103.44 | 12,777 | 123.5 | 55 % | 1,703,046 | 16,464 | 202,710 | 13,243 |
<!-- END pilot-report:channels -->

Unique chatters are counted per channel and across the pilot on the pseudonyms only. Summed per VOD they come to more, because many chatters appear in several VODs of the same channel. Per VOD (oldest first, as listed):

<!-- BEGIN pilot-report:vods -->
| # | Channel | VOD | Recorded (UTC) | Hours | Age at capture (d) | Clips | Clips/h | Chat messages | Ingest s | Audio download s | MiB/s | Peak MiB | Est. expiry |
|---|---|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| 1 | loltyler1 | `2894699771` | 2026-10-07 | 4.66 | 29.8 | 43 | 9.2 | 17,281 | 440 | 365 | 1.97 | 667 | 2026-12-06 |
| 2 | caseoh_ | `2894828714` | 2026-10-08 | 4.30 | 29.6 | 463 | 107.5 | 114,783 | 475 | 189 | 2.13 | 1,155 | 2026-12-07 |
| 3 | kaicenat | `2895594319` | 2026-10-09 | 4.31 | 28.7 | 901 | 208.9 | 128,492 | 605 | 308 | 1.28 | 1,187 | 2026-12-08 |
| 4 | jynxzi | `2896312034` | 2026-10-09 | 5.35 | 27.9 | 436 | 81.6 | 84,626 | 670 | 382 | 1.29 | 949 | 2026-12-08 |
| 5 | ishowspeed | `2898041380` | 2026-10-11 | 2.83 | 26.1 | 505 | 178.4 | 39,394 | 315 | 204 | 1.22 | 753 | 2026-12-10 |
| 6 | kaicenat | `2899188846` | 2026-10-12 | 5.32 | 24.8 | 308 | 57.9 | 50,921 | 555 | 373 | 1.32 | 809 | 2026-12-11 |
| 7 | loltyler1 | `2899670860` | 2026-10-13 | 4.88 | 24.2 | 34 | 7.0 | 14,591 | 1,010 | 941 | 0.48 | 664 | 2026-12-12 |
| 8 | jynxzi | `2899844366` | 2026-10-13 | 6.53 | 24.0 | 557 | 85.2 | 95,561 | 785 | 470 | 1.30 | 1,001 | 2026-12-12 |
| 9 | caseoh_ | `2900227157` | 2026-10-14 | 3.87 | 23.7 | 456 | 117.7 | 99,346 | 560 | 279 | 1.27 | 1,095 | 2026-12-13 |
| 10 | ishowspeed | `2900661913` | 2026-10-14 | 4.24 | 23.1 | 1,060 | 249.8 | 53,068 | 440 | 307 | 1.21 | 816 | 2026-12-13 |
| 11 | kaicenat | `2901714894` | 2026-10-15 | 3.04 | 21.9 | 764 | 251.5 | 56,940 | 305 | 148 | 1.86 | 842 | 2026-12-14 |
| 12 | loltyler1 | `2904713185` | 2026-10-19 | 2.14 | 18.8 | 33 | 15.4 | 8,404 | 205 | 163 | 2.09 | 638 | 2026-12-18 |
| 13 | caseoh_ | `2904758941` | 2026-10-19 | 4.08 | 18.7 | 529 | 129.7 | 116,716 | 395 | 164 | 2.31 | 1,155 | 2026-12-18 |
| 14 | jynxzi | `2905263958` | 2026-10-19 | 5.16 | 18.1 | 423 | 81.9 | 96,416 | 1,155 | 882 | 0.55 | 1,013 | 2026-12-18 |
| 15 | kaicenat | `2907224886` | 2026-10-22 | 5.60 | 15.9 | 1,533 | 273.8 | 131,631 | 650 | 367 | 1.40 | 1,186 | 2026-12-21 |
| 16 | ishowspeed | `2908755591` | 2026-10-23 | 2.59 | 14.1 | 716 | 276.4 | 32,130 | 210 | 102 | 2.28 | 730 | 2026-12-22 |
| 17 | loltyler1 | `2909976321` | 2026-10-25 | 4.26 | 12.9 | 28 | 6.6 | 13,821 | 390 | 327 | 2.07 | 661 | 2026-12-24 |
| 18 | kaicenat | `2910392998` | 2026-10-25 | 3.28 | 12.4 | 1,109 | 338.2 | 86,666 | 350 | 170 | 1.71 | 981 | 2026-12-24 |
| 19 | jynxzi | `2910768161` | 2026-10-25 | 3.27 | 12.0 | 531 | 162.6 | 73,341 | 430 | 240 | 1.26 | 904 | 2026-12-24 |
| 20 | caseoh_ | `2911013863` | 2026-10-26 | 4.10 | 11.8 | 528 | 128.8 | 113,208 | 940 | 690 | 0.55 | 1,142 | 2026-12-25 |
| 21 | ishowspeed | `2911712860` | 2026-10-26 | 2.38 | 11.0 | 268 | 112.6 | 33,284 | 255 | 165 | 1.33 | 724 | 2026-12-25 |
| 22 | ishowspeed | `2913223522` | 2026-10-28 | 3.85 | 9.2 | 490 | 127.2 | 48,466 | 545 | 431 | 0.83 | 794 | 2026-12-27 |
| 23 | jynxzi | `2914109607` | 2026-10-29 | 5.62 | 8.2 | 496 | 88.3 | 69,183 | 750 | 583 | 0.89 | 887 | 2026-12-28 |
| 24 | loltyler1 | `2914268139` | 2026-10-29 | 3.86 | 8.1 | 48 | 12.4 | 14,970 | 330 | 282 | 1.27 | 665 | 2026-12-28 |
| 25 | caseoh_ | `2914503283` | 2026-10-30 | 3.91 | 7.9 | 518 | 132.5 | 109,807 | 475 | 279 | 1.28 | 1,154 | 2026-12-29 |
<!-- END pilot-report:vods -->

"Age at capture" is the VOD's age when its clip snapshot was taken. "Est. expiry" adds the 60-day Partner retention to the recording date. It is an estimate: streamers can delete VODs earlier, and some channels keep them longer ([data sources § VOD retention](data-sources.md#vod-retention)).

## 5. Viewer clips

<!-- BEGIN pilot-report:clips -->
| Clip measure (active pilot) | Value |
|---|---|
| Clips | 12,777 |
| Views: median / p90 / p99 / max (nearest rank) | 1 / 14 / 498 / 80,472 |
| Clips with 1 view | 6,966 (55 %) |
| 1-view share per VOD | 17–80 % |
| Clips with ≥ 10 views | 1,730 |
| Clips with ≥ 100 views | 236 |
| Made after the stream ended | 5,129 (40 %) |
| Share of a VOD's clips in its densest 20 minutes | 11–41 % (median 21 %) |
| Clips starting at or after the reported VOD end | 9 in 7 VODs |
| Clips ending after the reported VOD end | 28 in 16 VODs |
| Clip snapshots complete / mature | 25 / 25 of 25 |
| VOD age at capture | 7.9–29.8 days |
<!-- END pilot-report:clips -->

- **Maturity.** Every snapshot was taken 7.9–29.8 days after its stream, so all 25 count as mature. Clips keep being made after a stream: 40 % were made after their stream ended, so from the VOD. The maturity rule fixes the VOD's age, not the clips' age ([labeling § 7](labeling.md#7-view-count-maturity)), so late clips have had less time to collect views.
- **Views are mostly tiny.** The median clip has 1 view. The 1-view share ranges from 17 % to 80 % per VOD.
- **Clips come in bursts.** A VOD's busiest 20 minutes hold 11–41 % of its clips.
- **Coverage.** Each snapshot searched Twitch's clip list in date ranges from the broadcast start to the capture time. No range reached the per-range limit, so nothing was cut off ([data model § clips](../architecture/data-model.md#clips)). The search also returned 138,190 clips of the channels' other VODs and 1,561 duplicates, which were dropped.

## 6. Chat

<!-- BEGIN pilot-report:chat -->
| Chat measure (active pilot) | Value |
|---|---|
| Messages | 1,703,046 |
| Messages per VOD | 8,404–131,631 |
| Messages per VOD-hour | 16,464 overall; per VOD 2,991–29,797 |
| Unique chatter pseudonyms | 202,710 across the pilot; 288,829 summed per VOD |
| Messages after the reported VOD end | 2,151 in 24 VODs |
| Malformed comments skipped by the parser | 0 (logged for 25 VODs) |
| Pseudonyms that aren't 64-hex / unredacted @mentions | 0 / 0 |
| Chat tool | TwitchDownloaderCLI@1.56.5 |
<!-- END pilot-report:chat -->

Chat was downloaded with TwitchDownloaderCLI and parsed into the `chat_messages` table. Authors became salted pseudonyms, @mentions and known names were redacted, and the raw replay file was deleted after parsing ([data handling § 2](data-governance.md#2-chat)). Third-party emotes (7TV, BTTV, FFZ) aren't counted in `n_emotes`, because ingest doesn't download their emote lists. The message text is kept, so they can be counted later from those services' public lists without downloading chat again ([data sources § 5](data-sources.md#5-third-party-emotes-optional)).

## 7. Audio

<!-- BEGIN pilot-report:audio -->
| Audio measure (active pilot) | Value |
|---|---|
| WAV format | 16000 Hz, 1 ch, 16-bit |
| WAV length minus Helix length | 0.00–0.98 s |
| Raw audio container bitrate | 203–371 kb/s |
| Raw audio per VOD-hour | 103.6 MB (WAV 115.2 MB) |
| Audio tool | yt-dlp@2026.08.19 |
<!-- END pilot-report:audio -->

The raw audio is Twitch's audio-only AAC stream as downloaded. Its bitrate varies by VOD: 22 VODs are at 203–218 kb/s and three Tyler1 VODs at 360–371 kb/s. The source sample rate also varies (44.1 or 48 kHz, checked with ffprobe during collection). None of this matters after normalization, because every WAV is 16 kHz mono 16-bit and lasts as long as the VOD to within a second.

## 8. Storage

<!-- BEGIN pilot-report:storage -->
| Category | Files | Size | Active VODs | Historical VODs |
|---|--:|--:|--:|--:|
| Raw audio (compressed source) | 26 | 10.93 GB | 10.71 GB | 216.7 MB |
| Normalized WAV (16 kHz mono) | 26 | 12.18 GB | 11.92 GB | 258.1 MB |
| Pseudonymized chat tables | 26 | 55.2 MB | 55.0 MB | 237.6 kB |
| Clip snapshots and manifests | 52 | 1.4 MB | 1.4 MB | 2.8 kB |
| Shared tables (catalog, VODs, channels) | 3 | 12.0 kB | - | - |
| Selection records | 5 | 565.0 kB | - | - |
| Earlier pilot lists | 1 | 1.0 kB | - | - |
| Run records and logs | 52 | 112.9 kB | - | - |
| **Total (dataset)** | 191 | 23.16 GB | 22.69 GB | 475.1 MB |
<!-- END pilot-report:storage -->

The table covers every file in the data directory except the runner's empty lock file and the collector's local notes, which aren't part of the dataset. An independent `find`/`stat` count of the directory matches it to the byte (23,163,142,568 B). Every VOD's files are still the size the runner recorded when it ingested that VOD. Raw chat takes no space: the replay files are deleted after parsing. Before deletion the largest replay seen was 90 MiB. The runner only samples the file every 5 s, so many replays were never seen, including some busy ones ([§ 14](#14-limitations)).

**For scaling:** 100 more VOD-hours would need about 22 GB (10 GB without WAVs) and about 3.6 hours of ingest at this pilot's rates. The WAVs can be regenerated from the raw audio.

## 9. Ingestion performance and memory

<!-- BEGIN pilot-report:performance -->
| Run measure (active pilot) | Value |
|---|---|
| Attempts | 25 for 25 VODs (25 on the first attempt) |
| Ingest wall time per VOD | 205–1,155 s (median 475 s) |
| Ingest minutes per VOD-hour | 1.35–3.82 |
| Phase: metadata and token | 39 s (0.3 %) |
| Phase: clip snapshot | 784 s (5.9 %) |
| Phase: chat download and parse | 2,999 s (22.6 %) |
| Phase: audio download | 8,811 s (66.5 %) |
| Phase: WAV normalization | 539 s (4.1 %) |
| Audio download speed | 0.48–2.31 MiB/s (median 1.29) |
| Peak process-tree memory | 638–1,187 MiB |
| Lowest free system memory during a run | 4.00–8.00 GiB |
| Peak memory model (least squares) | 577 MiB + 4.90 KiB per chat message |
<!-- END pilot-report:performance -->

Phase times come from the ingest log lines of each run, and memory comes from the runner's measurement of the whole process tree. Peak memory grows with chat size, because the parser reads the whole replay into memory: the fitted model predicts about 5.2 GiB for a one-million-message chat. The busiest pilot chat had 131,631 messages. The lowest free memory (4.00 GiB) was during the second run, before the 7 GiB start rule; with the rule, every run kept at least 6.3 GiB free. The audio download speed varied about 5× between VODs and dominates the run time.

## 10. Implications for #36 and #38

<!-- BEGIN pilot-report:labels-preview -->
| Channel | 10 s windows | Moments | Positive | Negative (≥ 60 s away) | Buffer | Negative share per VOD | Positive / negative, clips ≥ 10 views only |
|---|--:|--:|--:|--:|--:|--:|--:|
| caseoh_ | 7,297 | 536 | 56.4 % | 9.8 % | 33.8 % | 6–16 % | 10.2 % / 69.6 % |
| ishowspeed | 5,724 | 347 | 71.9 % | 4.9 % | 23.1 % | 0–12 % | 22.9 % / 49.8 % |
| kaicenat | 7,758 | 411 | 71.0 % | 6.3 % | 22.6 % | 2–14 % | 29.1 % / 46.3 % |
| jynxzi | 9,333 | 712 | 58.6 % | 9.6 % | 31.8 % | 0–19 % | 7.0 % / 77.3 % |
| loltyler1 | 7,128 | 164 | 11.7 % | 65.9 % | 22.4 % | 51–74 % | 1.5 % / 93.8 % |
| **all** | 37,240 | 2,170 | 53.8 % | 19.0 % | 27.1 % | 0–74 % | 13.6 % / 68.3 % |
<!-- END pilot-report:labels-preview -->

This is a **preview, not the labels**. It applies the documented defaults with the existing code: 10 s windows (`make_windows`), and clips merged into moments when they overlap or are at most 5 s apart (`merge_clips_into_moments`). A window counts as positive when it overlaps a moment and as a training negative when it is at least 60 s from every moment ([labeling § 4](labeling.md#4-window-labels)). It ignores view weights, which #38 adds.

What the pilot data means for the next issues:

1. **Clips cover most of a dense VOD (#38).** Busy channels have so many overlapping clips that 70 %+ of KaiCenat and IShowSpeed windows overlap a clip, and some VODs have no negatives at all. Merging isn't the cause: the positive windows are the union of the clip intervals, so a smaller merge gap changes the number of moments, not which windows are positive. "Overlaps any moment" is too broad as the positive rule here. Counting only clips with ≥ 10 views brings positives down to 13.6 % of windows and negatives up to 68.3 %, but a fixed view threshold removes clips unevenly by channel and drops more clips made after the stream (they had less time to collect views). #38 kept `v0`, so choosing the rule, recording it as a new `label_rules_version` and comparing it with `v0` is still to do before dataset v1 (#40).
2. **Streamers differ by 20×.** From about 9 clips per VOD-hour (Tyler1) to over 200 (KaiCenat), and 6.6–338 per VOD. Normalize weights per streamer (as [labeling § 6](labeling.md#6-normalization-across-channels) plans), and expect the Tyler1 test fold to be small (186 clips) with wide intervals.
3. **Most clips have 1 view.** 55 % of clips have a single view. With equal weights they would dominate the positives, so the view-relative weights of [labeling § 6](labeling.md#6-normalization-across-channels), or a view threshold, matter here.
4. **Clip and chat times past the VOD end.** Helix reports whole seconds, and the audio runs up to a second longer. Nine clips start 0–1 s past the reported end, 28 run past it (by up to about a minute), and chat continues for up to 35 s. Clamp moments to the VOD and treat chat after the end as lead context only.
5. **No muted segments reported.** Helix gave no muted segments for any of the 26 VODs, so `is_muted` will be false everywhere. Muted stretches can only show up in the audio (silence features).
6. **For #36.** The chat delay and clip alignment questions can use all 25 VODs. Chat is dense enough (2,991–29,797 messages per VOD-hour) to estimate lag per channel. Clips made after the stream (40 %) were made from the VOD, not from the live stream. Whether they line up with chat and audio peaks differently from live clips is worth checking.

## 11. Historical VOD

<!-- BEGIN pilot-report:historical -->
| Channel | VOD | Hours | Catalog | Clips | Chat messages | Audio |
|---|---|--:|---|--:|--:|--:|
| flight23white | `2894413562` | 2.24 | vod_meta ok, clips ok, chat ok, audio ok | 0 | 8,306 | 474.9 MB |
<!-- END pilot-report:historical -->

Kept as a diagnostic: a real VOD whose channel had no viewer clips. It is not in the active list and is left out of every table above, and it must stay out of clip-supervised labels, because a VOD without clips has no positives and every window labeled negative would be wrong ([collection plan](collection-plan.md#channels-and-vods)).

## 12. Integrity checks

<!-- BEGIN pilot-report:checks -->
| Check | Result | Detail |
|---|---|---|
| list: unique IDs, all primaries of the selections, channels in channels.yaml | pass | 25 IDs; per channel caseoh_ 5, ishowspeed 5, kaicenat 5, jynxzi 5, loltyler1 5 |
| catalog: every VOD has vod_meta, clips, chat and audio ok | pass | all 25 active and 1 historical VODs |
| metadata: every VOD has a row in processed/vods.parquet, of its selection's channel | pass | all 25 active and 1 historical VODs |
| clips: one snapshot per VOD, schema, complete, mature, no saturated range, counts match the manifest, unique clip IDs of this VOD and channel | pass | all 25 active and 1 historical VODs |
| chat: schema, 64-hex author pseudonyms, no unredacted @mentions, sorted non-negative offsets | pass | all 25 active and 1 historical VODs |
| audio: one raw file, WAV 16000 Hz mono 16-bit within 5 s of the Helix length | pass | all 25 active and 1 historical VODs |
| runs: last attempt complete, no temporary files left | pass | all 25 active and 1 historical VODs |
| privacy: no raw chat replay left on disk | pass | raw/chat/ empty |
| storage: every file in a known category and of a cataloged VOD | pass | none unclassified |
<!-- END pilot-report:checks -->

A failing check makes `pilot_report.py` exit 1. Beyond these checks, the runner's records show no errors, retries, timeouts or guard stops in any of the 26 runs, and the catalog holds no error text.

## 13. Reproducing this report

You need the complete pilot data directory (the collection machine's, or a full copy with the same layout) and the repository at the same `configs/`. With `AUTOCLIPPER_DATA_DIR` pointing at it:

```bash
uv run python scripts/pilot_report.py --check docs/data/pilot-report.md   # every table matches the data
uv run python scripts/pilot_report.py                                     # print the tables
uv run python scripts/pilot_report.py --json report.json                  # every measurement per VOD
uv run python scripts/pilot_report.py --update docs/data/pilot-report.md  # regenerate the tables
```

The script reads only. It makes no network request and changes no file in the data directory. The inputs behind these numbers:

<!-- BEGIN pilot-report:provenance -->
| Input | SHA-256 / count |
|---|---|
| `configs/pilot_vods.txt` | `ba5476a03f45cf5bcbbce6afb9f6a7c6cac9d92a71667d54a317e6fed39776b0` |
| Collection window (UTC, all runs) | 2026-11-06T15:49:53 to 2026-11-06T23:09:27 |
| Runner settings (26 of 26 runs) | retries 0, timeout 60 min + 30 min per VOD-hour, keep 25 GiB disk free, tree memory cap 8 GiB, stop below 1 GiB free memory, disk estimate x1.5, checked every 5 s |
| `pilot/selections/selection-20261106T145041Z.json` | `ee6b15cfedb4399c5334f6a535dd204c02f11ac8b487a5ae7ba8de7c34e2fd63` |
| `pilot/selections/selection-20261106T150046Z.json` | `8a8f187bc20703751ae73f9c7067e5c9e5fed88eea948c707f0a344cb6f6cbfd` |
| `pilot/selections/selection-20261106T161125Z.json` | `1de8a571735075f0ed4cba9ea1e67c66fc40fa59c6e0e400479362f5a338cf7f` |
| Tool `TwitchDownloaderCLI@1.56.5` | 25 catalog rows of active VODs |
| Tool `helix` | 50 catalog rows of active VODs |
| Tool `yt-dlp@2026.08.19` | 25 catalog rows of active VODs |
| VODs picked in `selection-20261106T145041Z.json` | 20 |
| VODs picked in `selection-20261106T161125Z.json` | 5 |
<!-- END pilot-report:provenance -->

## 14. Limitations

- **One machine, one connection, one day.** Download speeds and run times reflect a home connection on 2026-11-06; another machine or a worker will differ.
- **Five large channels.** Clip density, chat volume and moment coverage are for very large channels. Small channels would look very different ([collection plan](collection-plan.md#channels-and-vods)).
- **One snapshot per VOD.** Clip counts and views are as of capture (7.9–29.8 days after the stream). Clips made later are not in the data.
- **Raw chat size was sampled.** The temporary replay file was seen, at least partly, for 11 of 25 VODs. The others were written and parsed between two of the runner's 5 s checks, so the 90 MiB maximum is a lower bound.
- **Expiry is estimated.** The expiry dates follow the channel type's documented retention, not anything Twitch confirms.
- **No video.** Only audio was collected. Previews and exports of pilot VODs need the video, which can only be downloaded while the VOD is still on Twitch. Several pilot VODs are estimated to expire before the 2026-12-11 demo.

## 15. Privacy, permissions and retention

- **Pseudonymized at parse time.** Chat authors and clip creators are stored only as salted HMAC-SHA256 pseudonyms (64 hex characters, checked in § 12). The salt is held by the collector. It is not in the repository and doesn't go with any copy of the data. @mentions and known names are redacted in message text. Names typed without an @ can remain ([data sources § 2](data-sources.md#2-chat-replay-twitchdownloader-unofficial)).
- **No raw chat kept.** Every replay file was deleted after parsing (`raw/chat/` is empty, § 12).
- **Audio only.** The raw audio and WAVs are kept for analysis. No video was downloaded.
- **Shared with the team in Google Drive.** The AutoClipper team and CUNY Tech Prep approved keeping the pilot data in a private Google Drive folder for the project, and the folder is shared with the team. That folder is the team's chosen storage ([data handling § 1](data-governance.md#1-where-data-lives)): the data was collected once, and teammates reuse it without running the downloaders. It holds the data directory in the layout this report measures, the active list, `channels.yaml` and a copy of this report, with a manifest and SHA-256 checksums for every file. Point `AUTOCLIPPER_DATA_DIR` at a downloaded copy of its `data/` folder. It doesn't hold video, raw chat replays, the chat-hashing salt, credentials or the collector's notes.
- **Twitch's terms still apply.** The team's approval is internal, not a grant from Twitch or the broadcasters. Twitch's Terms of Service and Developer Services Agreement restrict downloading, storing and redistributing Twitch content and API data, and this project's decisions ([ADR-0011](../decisions/0011-data-sources-and-local-delivery.md)) don't override them. Use the data for AutoClipper only, don't copy it to other storage or people, and keep media and chat out of git, issues and PRs ([data handling § 1](data-governance.md#1-where-data-lives)). This report and its tables contain counts only, no media or chat.
- **Retention.** Nothing has been deleted, and no retention period is set yet. The shared copy includes the WAVs so teammates don't have to regenerate them; by the [data handling](data-governance.md#4-disk-space) rules they are transient and can be regenerated from the raw audio.

## 16. Exploration findings (#36)

*Added by #36.*
