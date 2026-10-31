# Data Collection Plan

| | |
|---|---|
| **Status** | Current. Pilot scope from [data sources and delivery defaults](../decisions/0011-data-sources-and-local-delivery.md); snapshot rules in [labeling](labeling.md). |
| **Last updated** | 2026-10-31 |

## Why collection is urgent

Twitch VODs expire ([data sources](data-sources.md#vod-retention)), and pilot VODs must also be at least 7 days old before they're ingested ([labeling § 7](labeling.md#7-view-count-maturity)). That leaves a limited window per VOD, so start the pilot promptly. App, export and evaluation work proceeds alongside it.

## What we collect per VOD

Everything is collected in one `autoclipper ingest` run per VOD (#34). Run it on a suitable team machine or worker and share the completed media and tables. Teammates can reuse those outputs without downloading the same VOD again.

| Item | Source | When | Approx. size (6 h VOD) |
|---|---|---|---|
| VOD metadata | Get Videos | At ingest | KB |
| Viewer-clip snapshot | Get Clips | At ingest, once; the VOD must be at least 7 days old ([labeling § 7](labeling.md#7-view-count-maturity)); record the capture time | KB–MB |
| Chat replay | TwitchDownloader | At ingest | ~10–200 MB JSON, depending on chat volume (deleted after parsing) |
| Audio | yt-dlp or TwitchDownloader (audio only) | At ingest | ~100–400 MB |
| Video | yt-dlp or TwitchDownloader | On demand for preview/export; retain the demo and evaluation set in the chosen storage | Measure in pilot |

These are rough planning estimates; measure them in the pilot. Each clip snapshot is immutable and records capture times, query ranges, coverage and dataset lineage ([data model](../architecture/data-model.md#clips)). Later-created clips and pagination limits affect which clips a snapshot contains. A VOD younger than 7 days at capture is flagged and left out of view-weighted labels and metrics.

## Channels and VODs

The pilot channels are listed in [`configs/channels.yaml`](../../configs/channels.yaml): five large channels, each with a `login` and a short `reason`. Collect **~5 VODs per channel** that are at least 7 days old and still available, then scale up if the pilot shows it's worth it (resource use, label density, coverage of held-out-streamer evaluation). The chosen VODs are collected in `configs/pilot_vods.txt` (public VOD links only), created by the pilot download (#35). Changes to either list go through PRs. Channels were picked because they:

1. **Have an active clip culture:** many viewer clips per stream, so labels are dense.
2. **Stream in English**, to keep captions and text features simple.
3. **Mix content types:** "Just Chatting"/IRL, FPS, variety. The model shouldn't learn one game's quirks only.
4. **Are large:** dense viewer clips give dense labels. Evaluation hides or subsamples a held-out VOD's clips to measure how much the scorer depends on clips ([evaluation § 6](../ml/evaluation.md#small-streamer-simulation)). That covers the "few or no clips" half of the small-streamer case, not a small channel's quieter chat; small channels can be added after the pilot if time allows.
5. **Have public VOD history** with retention long enough to collect. Subscriber-only VODs are skipped ([data sources](data-sources.md#subscriber-only-vods)).

## Targets

| Milestone | VODs with clips + chat + audio | Notes |
|---|---|---|
| Pipeline smoke test | 5 | From 2–3 channels |
| Pilot dataset (baselines and first models) | ~25 | ~5 per channel in `configs/channels.yaml` |
| Scale-up | Based on the pilot | Add VODs or channels if results and disk/download costs justify it |

Measure disk, download and feature costs before scaling. Keep compressed audio in `data/raw/`; WAV is transient. Features alone can't support preview or export after a VOD expires, so retain the media needed for the demo and evaluation in the chosen storage.

## Ingest

The pilot runs `autoclipper ingest` (#34) once for each VOD in `configs/pilot_vods.txt` (#35). For one VOD it:

1. Fetches the VOD metadata (Helix Get Videos) and checks the VOD's age.
2. Takes the viewer-clip snapshot (Helix Get Clips, chunked by time range) and records its capture time and coverage.
3. Downloads and parses the chat replay (TwitchDownloader), then deletes the raw file.
4. Downloads the audio (yt-dlp or TwitchDownloader).
5. Records each step's status and tool versions in the catalog.

- **Idempotent and resumable:** the catalog records what's done. Re-runs pick up failures. Deleted and subscriber-only VODs are recorded as `unavailable`.
- **Polite:** respect rate limits, run downloads sequentially (or with low concurrency), and add a random delay between media downloads.

There is no scheduled collector in the MVP. Watching channels for new VODs automatically is the stretch channel-tracking issue (#37).

## Data sharing within the team

Raw data **never goes in git**. Use a shared directory, direct copy or hosted storage to make completed media and versioned tables available to the team. Point `AUTOCLIPPER_DATA_DIR` at a shared directory when possible; for object storage, synchronize the same versioned layout to the machine running a stage. Use one writer per shared catalog or dataset output to avoid collisions. Chat is shared as the pseudonymized message tables and per-window features; the raw replay file is deleted after parsing ([data handling](data-governance.md)).
