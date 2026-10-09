# Product Requirements Document: AutoClipper

| | |
|---|---|
| **Status** | Draft v0.1. Initial idea agreed. Details open for team refinement. |
| **Last updated** | 2026-10-09 |
| **Related** | [Roadmap](roadmap.md) · [Architecture](../architecture/overview.md) · [Evaluation](../ml/evaluation.md) · [Open questions](../project/open-questions.md) |

> This PRD is a living document. Sections marked **Proposal** reflect the current best plan but haven't been ratified by the team.

---

## 1. Summary

AutoClipper takes a Twitch VOD (past broadcast), finds the moments most worth turning into short-form videos, ranks them, and exports them as vertical, captioned clips ready to post on TikTok, Instagram Reels or YouTube Shorts.

It learns what makes a moment clip-worthy from **clips real viewers made**, which the Twitch API reports along with where each one sits in the VOD. That gives the project a large, naturally labeled dataset at no labeling cost. The goal is a model whose picks **measurably beat simple baselines**.

**One-line pitch:** *Someone had to scrub an 8-hour stream to find those 30 seconds. AutoClipper finds them for you.*

## 2. Problem

**Stream clipping is a real side hustle.** Popular streamers' highlights are reposted to short-form platforms by fan "clip accounts". Many streamers allow this, and some actively pay clippers. The bottleneck is **finding and choosing** moments:

1. **Scrubbing is slow.** Streams often run 4–10+ hours. Finding the best moments by hand takes hours per stream.
2. **Viewer clips are noisy.** A big stream can produce hundreds of viewer-made clips. Many duplicate the same moment, many are inside jokes, and their Twitch view counts reflect live context rather than how they'd perform as a standalone short.
3. **Small streams have no clips.** For smaller channels, or streams that just ended, few or no viewer clips exist, so there's nothing to choose from.
4. **Formatting is busywork.** Turning a 16:9 gameplay+facecam clip into a 9:16 captioned short is repetitive manual editing.

### Why existing tools don't solve it

Commercial auto-clippers exist (e.g. Eklipse, Medal, Opus Clip). From the outside, they appear to be general-purpose and opaque. Tools aimed at talking-head content lean heavily on transcripts. AutoClipper's angle:

- **It learns from what real viewers clipped** in this content category, not from a generic notion of "interesting".
- **It's measured.** Every claim about quality is backed by an offline evaluation against baselines. The project's value is the evidence as much as the tool.

## 3. Users

**Primary: the clipper.** Runs (or wants to start) a short-form clip account for one or more streamers. Wants the best few moments from a long stream fast, formatted and ready to post. Comfortable pasting a link, not interested in editing timelines.

**Secondary: the streamer or their editor.** Wants a highlight list of their own stream to post or to hand to an editor.

**Not a target user: the viewer.** People watch long streams because they enjoy the whole thing. AutoClipper is not a "watch the highlights instead" product.

## 4. Goals and non-goals

### Goals

| # | Goal | How we'll know |
|---|---|---|
| G1 | Rank a VOD's viewer clips so the top few are the best to post | Beats baselines on ranking metrics (see §8) |
| G2 | Detect clip-worthy moments in VOD time that has no clips | Recovers held-out human clips better than baselines |
| G3 | Export ready-to-post clips | Vertical 9:16, burned-in captions, correct trim, uploadable as-is |
| G4 | Make it demoable in under a minute | Paste link → timeline + ranked clips → play one |
| G5 | Produce a credible data-science result | Reproducible evaluation, ablations ("which signals matter?") |

### Non-goals (for the course project)

- **Auto-publishing to social platforms.** Out of scope: platform API approval and scope creep. A clipper uploads the exported files themselves.
- **Platforms other than Twitch** as an input source.
- **Training on raw video end to end.** Video is optional and comes later (see [ADR-0004](../decisions/0004-staged-scope-chat-and-audio-first.md)).
- **Real-time / live-stream clipping** for v1 or v2. It's a stretch goal.
- **Monetization** or revenue targets as a success metric.
- **Multi-user accounts, auth, billing.** The app is a single-user tool for the demo.

## 5. User stories

| ID | As a… | I want to… | So that… | Stage |
|---|---|---|---|---|
| US-1 | clipper | paste a Twitch VOD URL | I don't have to download anything myself | v1 |
| US-2 | clipper | see the VOD's viewer clips ranked by predicted quality, with duplicates merged | I can pick the best few in seconds | v1 |
| US-3 | clipper | see a highlight timeline across the whole VOD | I can see where the action is at a glance | v1 (clip markers), v2 (model scores) |
| US-4 | clipper | preview a candidate before exporting | I don't export duds | v1 |
| US-5 | clipper | export a candidate as a vertical, captioned MP4 | I can upload it directly | v1 (basic), v2 (smart crop) |
| US-6 | clipper | get candidates from parts of the stream nobody clipped | I find moments other clippers missed | v2 |
| US-7 | clipper of a small streamer | get candidates for a VOD with zero viewer clips | the tool still works when there are no clips | v2 |
| US-8 | clipper | adjust clip start/end before export | I can fix a bad trim | v2 |
| US-9 | team member | see why a moment scored high (chat spike, loudness, …) | we can explain and debug the model | v2 |
| US-10 | clipper | get candidates while a stream is still live | I can post before other clippers | Stretch |

## 6. Functional requirements

Requirement IDs are stable. Reference them in issues and PRs (e.g. "implements FR-3").

### Ingestion

- **FR-1** Accept a Twitch VOD URL or video ID and resolve its metadata (channel, duration, created time, muted segments) via the Twitch Helix API.
- **FR-2** Fetch every viewer clip linked to that VOD with its `vod_offset`, duration, view count and creation time.
- **FR-3** Obtain the VOD's **chat replay** with per-message offsets. Pseudonymize usernames at ingestion.
- **FR-4** Obtain the VOD's **audio** (audio-only download) for feature extraction.
- **FR-5** Cache everything locally so re-running analysis never re-downloads.

### Analysis

- **FR-6** Merge overlapping viewer clips into **moments** (deduplication).
- **FR-7** Compute per-window chat and audio features (see [features](../ml/features.md)).
- **FR-8** (v1) Score and rank moments derived from viewer clips.
- **FR-9** (v2) Score every window of the VOD and propose candidate moments, including un-clipped ones.
- **FR-10** Return, for each candidate, the score, start/end in VOD seconds, and the top contributing signals.

### Export

- **FR-11** Cut a candidate from the source video at the right start/end. Correct for chat lag where relevant (see [labeling](../data/labeling.md)).
- **FR-12** Reframe to 9:16 (v1: center crop or a fixed facecam layout; v2: detection-based smart crop).
- **FR-13** Burn in captions generated by speech-to-text.
- **FR-14** Output an MP4 that meets common short-form upload constraints (H.264/AAC, ≤ 60–90 s, 1080×1920).

### Interface

- **FR-15** A CLI covering every pipeline step, so it's scriptable and testable.
- **FR-16** A web UI for the demo: paste URL → progress → timeline + ranked list → preview → export.

## 7. Non-functional requirements

| ID | Requirement | Target (Proposal) |
|---|---|---|
| NFR-1 | Runs on a typical student laptop (no GPU required) for v1/v2 | Chat + audio features for a 6-hour VOD in ≤ 10 minutes on CPU, excluding download time |
| NFR-2 | Reproducibility | Any reported metric can be regenerated from a commit + dataset version + seed |
| NFR-3 | Cross-platform | Windows, macOS, Linux |
| NFR-4 | Privacy | No raw chat usernames stored. No media or chat in git. |
| NFR-5 | Platform etiquette | Respects Twitch API rate limits and backs off on 429 |
| NFR-6 | Robustness | Handles missing chat, deleted VODs, muted segments, clips with null `vod_offset` |

## 8. Success metrics

**Primary (offline, required):** see [evaluation](../ml/evaluation.md) for exact definitions.

| Stage | Metric | Must beat |
|---|---|---|
| v1 clip ranker | NDCG@10 / Precision@5 against the chosen relevance target | Random order; sort by Twitch views (where applicable, see open question OQ-1) |
| v2 moment detector | Recall@k of held-out human-clipped moments; PR-AUC per window | Chat-rate spike heuristic; audio-loudness heuristic |
| Both | Ablation: contribution of each signal family | n/a (reported as a finding) |

**Secondary (online, stretch, needs streamer permission):** post model-picked clips vs. randomly picked clips on a clip account and compare normalized views. See [evaluation § online experiment](../ml/evaluation.md#5-online-experiment-stretch).

**Demo:** a judge can follow the whole flow in under a minute without explanation.

> **No results exist yet.** This section defines how success will be measured, not what has been achieved.

## 9. Constraints and assumptions

- **Team:** four students working part time over the course term, with heavy use of AI coding agents.
- **Compute:** laptops. Free tiers only, unless the team decides otherwise.
- **Data access:** Twitch Helix API (official, free developer app). Chat replay and VOD download rely on **unofficial** tools (see [data sources](../data/data-sources.md)). This is a known risk.
- **VOD retention:** Twitch deletes past broadcasts after a retention period, so **collection must start early and run continuously**.
- **Course requirements:** the project must be a data-science project with a model trained on real data that's essential to the product, not an API wrapper.

## 10. Risks

The full register is in [`../project/risks.md`](../project/risks.md). The top three:

1. **Unofficial chat/VOD tooling breaks** → keep a live-chat logger as a backup source. Collect early.
2. **Label noise** (clips reflect live context, duplicates, clip-start bias) → moment merging, careful label definitions, ranking metrics robust to noise.
3. **Scope creep into video ML** → chat + audio first. Video only as a measured add-on.

## 11. Open questions

Tracked in [`../project/open-questions.md`](../project/open-questions.md). The most important one for this PRD is **OQ-1: what exactly is the v1 ranking target?** Twitch view count is available for existing clips, so predicting it for old clips isn't useful. The candidate framings are listed there.

## 12. Milestones

See [roadmap](roadmap.md).
