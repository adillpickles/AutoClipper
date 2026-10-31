# ADR-0011: Data sources and local-first delivery

| | |
|---|---|
| **Status** | Accepted |
| **Date** | 2026-10-30 (revised 2026-10-31) |
| **Related** | [Data handling](../data/data-governance.md) · [Collection plan](../data/collection-plan.md) · [Data sources](../data/data-sources.md) · [PRD § 5 Delivery](../product/prd.md#5-delivery) |

## Context

AutoClipper is a student data-science project for the CUNY Tech Prep fellowship. To build and evaluate it we need real VODs, their chat and the viewer clips made from them, and the app has to run somewhere without a budget for servers or storage. Earlier drafts kept data collection, hosting and the training target open; this ADR settles them so the build can proceed.

## Decision

1. **Sources.** The official Twitch Helix API (one shared team app) provides VOD metadata and the viewer-clip list. [TwitchDownloader](https://github.com/lay295/TwitchDownloader) (CLI pinned in the repo) provides chat replay. TwitchDownloader or yt-dlp provides VOD audio, and video for previews and exports. Everyone runs the tools locally; the pilot's VOD links are collected in `configs/pilot_vods.txt` (#35). Browser extensions and download websites aren't used: they can't be scripted.
2. **Pilot.** Five large channels listed in [`configs/channels.yaml`](../../configs/channels.yaml), about 5 recent VODs each, mixing Just Chatting and gaming. Large channels give many viewer clips to learn from. Evaluation hides or subsamples a held-out VOD's clips to measure how much the scorer depends on clips; that doesn't test a small channel's quieter chat ([evaluation](../ml/evaluation.md#small-streamer-simulation)). Scale up if the pilot shows it's worth it.
3. **Storage.** Downloads and derived data live on team machines under the git-ignored `data/` and `artifacts/` folders. Teammates may copy files to each other (shared drive or direct copy). Nothing heavy goes in git.
4. **Chat.** Pseudonymize usernames and user IDs at parse time, redact @mentions, and delete the raw replay file after parsing. Keep the pseudonymized messages (`data/interim/chat/`) so features can be recomputed after a VOD expires. Models only use per-window aggregates, and this keeps names out of every file we write.
5. **Training target.** Learn which moments are clip-worthy from chat and audio signals, with viewer clips (weighted by views relative to the channel's usual clips) as noisy positive labels. The app also ranks moments nobody clipped.
6. **Analysis rules.** [ADR-0009](0009-analysis-and-evaluation-contract-repair.md) is accepted as written; its numeric parameters stay tunable in experiments.
7. **Delivery: local first.** `autoclipper serve` runs the backend on the user's machine and the app opens in the browser. All downloading, analysis and rendering is local, so there's nothing to pay for. A laptop is the demo machine.
8. **Distribution.** A static project website (GitHub Pages or Vercel) explains the project, shows a demo video, links the repo and offers a packaged download of the local app (#70, stretch). Users analyze their own VODs on their own machine. Hosted analysis (cloud jobs, storage, accounts) is out of scope this semester; because the app talks to the backend only through the [API contract](../architecture/api-contract.md), it can be added later without changing the frontend.

## Consequences

- Collection can start as soon as the ingest command (#34) exists; nothing else blocks it.
- The downloader tools are unofficial and can break when Twitch changes things: pin versions, fail loudly, and download the pilot early because VODs expire ([risks](../project/risks.md)).
- The demo depends on local media, so the demo VODs are analyzed and rendered in advance (#50).
