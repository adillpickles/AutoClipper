# Product Requirements Document: AutoClipper

| | |
|---|---|
| **Status** | Current. Updated 2026-10-31: two outputs (vertical clips and a highlights video), local-first delivery. |
| **Last updated** | 2026-10-31 |
| **Related** | [Roadmap](roadmap.md) · [Architecture](../architecture/overview.md) · [API contract](../architecture/api-contract.md) · [Modeling plan](../ml/modeling-plan.md) · [Evaluation](../ml/evaluation.md) |

## Scope at a glance

**The MVP, end to end by demo night:** give the app a Twitch VOD → it pulls the chat and audio → it scores every moment of the stream → it comes back with the **top 10 moments as ready-made vertical clips** (9:16, captioned), ranked, each with a one-line reason → the user previews, nudges start/end if needed, and downloads them one by one or all at once, **or turns the top moments into one highlights video** to watch.

| Priority | What's in it |
|---|---|
| **Must (MVP)** | VOD in (Twitch link, or an already-downloaded VOD plus its link) · download chat and audio with TwitchDownloader · viewer-clip list from the Twitch API · chat and audio features for every few seconds of the VOD · baselines (random, biggest chat spike, loudest moment, chat + loudness) and a trained model (logistic regression, then LightGBM) tested on a streamer it never saw · top 10 clips auto-rendered as 9:16 MP4s with burned-in captions · results screen: ranked clip cards with preview, score and a one-line reason · trim and re-export · download · **highlights video**: the top moments joined into one 16:9 video |
| **Should (polish)** | Laughter/scream/cheer detection with a pretrained audio model · speech (Whisper) signals for the shortlist · an LLM rerank of the top ~30 moments that also writes the reason and a title · clip start/end snapped to natural speech pauses · a whole-VOD timeline/heatmap view · signal-by-signal comparison · blind taste test with classmates · caching analyzed VODs |
| **Could (stretch)** | Facecam stacked over gameplay (smart crop) · track channels and auto-analyze each new VOD · desktop app download plus a project website · video signals (scene cuts, motion) · thumbs up/down feedback · Docker |
| **Won't (this semester)** | Clipping live streams in real time · auto-posting to TikTok, Reels or Shorts · training video models from scratch · predicting TikTok/Reels views · YouTube or Kick input · accounts and payments · mobile app |

**Editing stays small:** trim, a 9:16 crop, captions on/off, and picking which moments go into the highlights video. No timeline editor or effects.

**The model goal** is to beat the baselines on a streamer it never trained on. If it doesn't, we say so; the app still ships with the best scorer we have.

---

## 1. Summary

AutoClipper turns a long Twitch VOD into its best moments. It reads the two cheapest, strongest signals of a great moment, **chat going wild** and **the streamer getting loud**, scores every few seconds of the stream, and hands back two things:

- **Vertical clips:** the top 10 moments as ranked, captioned 9:16 clips (20–45 s each by default), ready to post on TikTok, Reels or Shorts.
- **A highlights video:** the same top moments, in stream order, joined into one 16:9 video for someone who wants to watch the best of the stream.

Viewer-made clips on Twitch tell us which moments people already cared about. We use them as training examples, not as the answer: the app also finds moments nobody clipped.

**One-line pitch:** *Someone had to scrub an 8-hour stream to find those 30 seconds. AutoClipper finds them for you.*

## 2. Problem

**Clipping is a real side hustle, and watching a whole VOD is a big ask.** Popular streamers' highlights are reposted by clip accounts, and fans who missed a stream want the best parts. The bottleneck is finding and formatting the moments:

1. **Scrubbing is slow.** Streams run 4–10+ hours.
2. **Viewer clips are noisy.** Big streams produce hundreds of duplicate, inside-joke or badly trimmed clips.
3. **Small or fresh streams have few clips,** and small streamers rarely have a community of clippers.
4. **Formatting is busywork.** Turning 16:9 gameplay+facecam into a captioned 9:16 short, or stitching moments into a recap, is repetitive.

## 3. Users

| User | Wants | Main output |
|---|---|---|
| **Clipper/editor** | Ready-to-post clips from a stream without watching it | Vertical clips |
| **Small streamer** | Clips of their own broadcasts without hiring an editor or having clippers | Vertical clips (and a highlights video for YouTube) |
| **Viewer/fan** | The best parts of a stream they missed, in one video | Highlights video |

All three use the same app and the same analysis; only the export differs.

## 4. The app experience

Inspired by tools like Opus Clip: the analysis is a black box to the user; the output is finished video.

1. **Start.** Paste a Twitch VOD link. If you already downloaded the VOD, also paste the file's path on your computer (browsers don't expose dropped files' paths to a local server), so it isn't downloaded again; the link is still needed for chat and viewer clips.
2. **Wait.** A progress screen: "Downloading chat… Listening to audio… Finding moments… Rendering clips (4 of 10)".
3. **Results.** A grid of 10 clip cards, ranked 1–10. Each card shows the vertical clip (playable), its rank and score bar, the time in the stream, and a one-line reason, e.g. "Chat exploded to 9× normal and the streamer screamed".
4. **Adjust.** Open a card to nudge start/end and re-render, or toggle captions.
5. **Download** one clip or all of them.
6. **Highlights video.** A "Make highlights video" button joins the top moments (all 10 by default; untick any you don't want; uses your trims) in stream order into one 16:9 video, then offers it for download.

A whole-stream timeline with the top moments highlighted is a Should, as a secondary view.

## 5. Delivery

- **Initial setup.** `autoclipper serve` starts the backend and the app opens in the browser. A team laptop is the first demo target; the core flow needs no hosting service.
- **Collection and storage.** Download a VOD once and reuse its media and processed tables. A team machine, shared drive, collection worker or hosted storage can supply the same versioned inputs. No teammate needs to repeat another person's completed downloads.
- **Distribution (Could):** a project website that explains the project, shows a demo video, links the repo and offers a packaged download (#70). It can also link to a hosted app when one is available.
- **Hosted deployment is an option.** Choose it when it improves access or the demo and fits available resources. Extend the [API contract](../architecture/api-contract.md), job persistence and media-input handling as the deployment needs; these are implementation choices within the project.

## 6. Goals and non-goals

| # | Goal | How we'll know |
|---|---|---|
| G1 | Find the moments worth clipping | Our top 10 overlap viewer clips more often than the baselines, on a streamer the model never saw |
| G2 | Find moments nobody clipped | Discovery works from chat + audio alone; checked in the taste test |
| G3 | Hand back finished video | Correct trim, 9:16 clips and a 16:9 highlights video, readable captions, plays everywhere |
| G4 | Be easy to use | A classmate goes from VOD to downloaded clip and highlights video without help |
| G5 | Credible data science | Reproducible evaluation; which signals matter |

**Non-goals:** auto-publishing, other platforms as input, end-to-end video models, live clipping, predicting short-form views, accounts and billing.

## 7. Functional requirements

### Ingestion

- Accept a Twitch VOD link or ID; resolve its metadata (channel, duration, muted segments) with the Twitch API.
- Accept an already-downloaded VOD file (plus its link for chat and clips) instead of downloading again.
- Fetch the VOD's viewer clips (start time in the VOD, duration, views) with the Twitch API.
- Download the chat replay with TwitchDownloader; hash usernames and redact mentions at parse time; keep the pseudonymized messages and compute per-window totals.
- Download audio-only with TwitchDownloader or yt-dlp; report missing or muted audio.
- Cache downloads and processed tables locally so re-running a VOD is fast.

### Analysis

- Merge overlapping viewer clips into one moment.
- Compute chat and audio features for every window (see [features](../ml/features.md)).
- Score every window with the selected scorer (a baseline or the trained model).
- Turn scores into candidate moments (20–45 s by default) across the whole VOD, including parts nobody clipped and VODs with zero clips.
- For each candidate return rank, score, start/end in VOD seconds and its strongest signals, which the app turns into a one-line reason.

### Export

The cutting rules are in [architecture § Export](../architecture/overview.md#46-export); the API is in the [API contract](../architecture/api-contract.md).

- **Vertical clips.** Cut the selected interval from the source video exactly as trimmed (times are already aligned; no extra chat-lag shift). Reframe to 9:16 by center crop or a configured region (smart crop is a Could). Burn in captions from speech-to-text (faster-whisper). Output H.264/AAC MP4, 1080×1920, at most 90 s.
- **Auto-render.** When analysis finishes, the app requests clip exports for the top 10 candidates (center crop, captions on) and fills in the cards as they finish.
- **Highlights video.** On request, join the chosen moments (default: the top 10 with their current trims) in stream order into one H.264/AAC MP4 at 1920×1080, original framing, with a 0.5 s fade between moments, captions optional (off by default), at most 15 minutes.

### Interface

- CLI commands for the pipeline, exports and experiments (`autoclipper analyze`, `export`, `highlights`, `train`, `evaluate`).
- A local web app (FastAPI + Next.js, [web stack](../decisions/0007-web-application-stack.md)) following § 4.

## 8. Non-functional requirements

| Requirement | Target |
|---|---|
| Runs on a student laptop without a GPU | Chat + audio features for a 6-hour VOD in ≤ 10 minutes, excluding download |
| Reproducibility | Any reported metric regenerates from a commit + dataset version + seed |
| Cross-platform | Windows, macOS, Linux |
| Privacy | No chatter names in processed data, logs or git |
| Polite API use | Respects Twitch API rate limits |
| Robustness | Handles missing chat, deleted VODs, muted audio, clips without a VOD timestamp |

## 9. Success metrics

See [evaluation](../ml/evaluation.md). In short, starting simple:

| Stage | Metric | Must beat |
|---|---|---|
| First | **Hit rate@10**: of our top 10 moments, how many overlap a real viewer clip, on a held-out streamer | Random, chat spike, loudness, chat + loudness |
| Next | Recall of the most-viewed clips; NDCG@10 weighted by clip views | Same |
| Polish | Blind taste test: classmates rate our clips vs. the chat-spike baseline's (1–5) | Chat-spike baseline |

> **No results exist yet.**

## 10. Constraints

- **Team:** four students, part time, building with AI coding agents.
- **Compute:** laptops; free tiers only.
- **Data:** Twitch API for metadata and viewer clips; TwitchDownloader and yt-dlp for chat, audio and video ([data sources and delivery](../decisions/0011-data-sources-and-local-delivery.md)). These tools are unofficial and can break when Twitch changes things.
- **VOD expiry:** Twitch deletes VODs after 7–60 days, so download the pilot VODs early.

## 11. Risks

Full register: [risks](../project/risks.md). Top three:

1. **Downloader tools break** → pin versions, download early, keep local copies.
2. **Noisy labels** (duplicate clips, clips that start late) → merge clips into moments, generous time matching.
3. **Scope creep** → nothing from Should/Could until the MVP runs end to end.

## 12. Milestones

See [roadmap](roadmap.md).
