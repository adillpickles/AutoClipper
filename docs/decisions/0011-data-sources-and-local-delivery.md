# ADR-0011: Data sources and delivery defaults

| | |
|---|---|
| **Status** | Accepted |
| **Date** | 2026-10-30 (revised 2026-10-31) |
| **Related** | [Data handling](../data/data-governance.md) · [Collection plan](../data/collection-plan.md) · [Data sources](../data/data-sources.md) · [PRD § 5 Delivery](../product/prd.md#5-delivery) |

## Context

AutoClipper is a student data-science project for the CUNY Tech Prep fellowship. Building and evaluating it needs VODs, chat and viewer clips, plus a working app flow. This record gives the first implementation concrete defaults. Collection, storage and deployment arrangements can evolve as the team builds the product.

## Decision

1. **Sources.** The official Twitch Helix API (one shared team app) provides VOD metadata and the viewer-clip list. [TwitchDownloader](https://github.com/lay295/TwitchDownloader) (CLI pinned in the repo) provides chat replay. TwitchDownloader or yt-dlp provides VOD audio, and video for previews and exports. The pilot's VOD links are collected in `configs/pilot_vods.txt` (#35). Run the scripted collection on a suitable team machine or worker and reuse completed downloads and tables.
2. **Pilot.** Five large channels listed in [`configs/channels.yaml`](../../configs/channels.yaml), about 5 recent VODs each, mixing Just Chatting and gaming. Large channels give many viewer clips to learn from. Evaluation hides or subsamples a held-out VOD's clips to measure how much the scorer depends on clips; that doesn't test a small channel's quieter chat ([evaluation](../ml/evaluation.md#small-streamer-simulation)). Scale up if the pilot shows it's worth it.
3. **Storage.** Downloads and derived data use the git-ignored `data/` and `artifacts/` layout. Configure the roots for a local directory, shared drive or worker's storage. Hosted storage can be integrated when useful. Collect each input once and share its versioned outputs; nothing heavy goes in git.
4. **Chat.** Pseudonymize usernames and user IDs at parse time, redact @mentions, and delete the raw replay file after parsing. Keep the pseudonymized messages (`data/interim/chat/`) so features can be recomputed after a VOD expires. Models use per-window aggregates; the parser's handling of mentions and known names is documented in [data sources](../data/data-sources.md#2-chat-replay-twitchdownloader-unofficial).
5. **Training target.** Learn which moments are clip-worthy from chat and audio signals, with viewer clips (weighted by views relative to the channel's usual clips) as noisy positive labels. The app also ranks moments nobody clipped.
6. **Analysis defaults.** Follow [labeling](../data/labeling.md), [candidate generation](../ml/modeling-plan.md#3-candidate-generation) and [evaluation](../ml/evaluation.md). Numeric parameters stay tunable in experiments; version changes that affect labels or results.
7. **Initial setup.** `autoclipper serve` starts the backend and the app opens in a browser. A laptop is the first development and demo target. This avoids a hosting prerequisite while the product is being built.
8. **Distribution and deployment.** A project website explains the project, shows a demo video, links the repo and offers an app download (#70, stretch). A hosted app is also an option when useful and feasible. Adapt worker execution, persistence and media inputs as needed, keeping the [API contract](../architecture/api-contract.md) current.

## Consequences

- Collection can start as soon as the ingest command (#34) exists; nothing else blocks it.
- The downloader tools can break when Twitch changes things: pin versions, report failures clearly, and download the pilot early because VODs expire ([data sources](../data/data-sources.md)).
- Analyze and render the demo VODs in advance (#50), with an offline copy as a fallback for the chosen deployment.
