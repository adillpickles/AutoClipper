# Product Requirements Document: AutoClipper

| | |
|---|---|
| **Status** | Product scope agreed (2026-10-16), based on the team's project design doc. The exact training target and detailed label/evaluation rules are still open (see below). |
| **Last updated** | 2026-10-16 |
| **Related** | [Roadmap](roadmap.md) · [Architecture](../architecture/overview.md) · [Evaluation](../ml/evaluation.md) · [Open questions](../project/open-questions.md) |

## Scope at a glance

**The MVP, end to end by demo night:** paste a Twitch VOD link → the pipeline pulls viewer clips, chat and audio → chat and audio signals plus a trained model rank moments across the whole VOD → the web app shows the top moments with scores, a timeline and a preview → the user adjusts start/end → one click exports a vertical (9:16), captioned MP4.

| Priority | What's in it |
|---|---|
| **Must (MVP)** | Paste a VOD link · collect VOD info, viewer clips, chat replay and audio · find and rank moments across the whole VOD, **including VODs with no viewer clips** · show viewer clips too, with duplicates merged · web app with ranked shortlist, timeline and preview · adjust start/end · export a 9:16 MP4 with basic crop and burned-in captions · 3 simple baselines (random, biggest chat spike, loudest audio) and at least one trained model (logistic regression, then LightGBM), evaluated on VODs and streamers it never trained on |
| **Should (polish)** | Speech (Whisper) signals on the shortlist · signal-by-signal comparison (ablations) · blind taste test with classmates · smart crop (facecam stacked over gameplay) · "why this moment" explanations · caching processed VODs · progress bar and clear errors (bad link, expired VOD, missing chat) · evaluation script in CI |
| **Could (stretch)** | Video signal experiment · learning the chat delay automatically · thumbs up/down feedback · interactive heatmap dashboard · AI-suggested titles and hooks · Docker · hosted deployment (OQ-18) |
| **Won't (this semester)** | Clipping live streams in real time · auto-posting to TikTok, Reels or Shorts · training video models from scratch · predicting TikTok/Reels views · YouTube or Kick input · accounts and payments · mobile app |

**Editing is deliberately small:** trim (start/end), a basic 9:16 crop and captions. No timeline editor, effects or multi-clip compilations. Nothing from Should or Could starts until the MVP runs end to end.

**The model goal** is to beat the baselines on streamers it never trained on. If it doesn't, we report that honestly and the app still ships; app and export work never wait on the model.

### Decided vs. still open

| Decided (follow it) | Still open (don't decide alone; see [open questions](../project/open-questions.md)) |
|---|---|
| Users: viewers, streamers, clippers/editors, all in one app | **OQ-1:** the exact training target (what counts as a positive, how views are used) |
| Web app: FastAPI backend + Next.js frontend ([ADR-0007](../decisions/0007-web-application-stack.md)) | **OQ-9 / OQ-17:** chat retention and source permissions. These block *real* data collection; synthetic data is fine meanwhile. |
| Python 3.12 + uv, single repo ([ADR-0002](../decisions/0002-language-and-tooling.md), [ADR-0003](../decisions/0003-repository-layout.md)) | **OQ-18:** hosting and the app's database (local demo first) |
| Chat + audio first; video optional for ranking, required for export ([ADR-0004](../decisions/0004-staged-scope-chat-and-audio-first.md)) | **ADR-0009 details:** window size, moment grouping, matching rules for evaluation |
| Parquet + DuckDB for pipeline data ([ADR-0005](../decisions/0005-local-storage-parquet-duckdb.md)) | **OQ-4:** which channels to collect |
| Baselines → logistic regression → LightGBM; splits grouped by streamer ([modeling plan](../ml/modeling-plan.md)) | |

---

## 1. Summary

AutoClipper helps users discover, review and export highlights from a supported Twitch VOD (past broadcast). Viewers can catch up on interesting moments; streamers and editors can turn selected moments into useful clips, including vertical captioned videos for short-form destinations.

Existing viewer clips, available VOD offsets and view counts are useful candidates, signals and weak labels. They do not supply exhaustive ground truth or prove standalone highlight quality. Combine available information with heuristics and useful trained ML, particularly where clips are incomplete or unavailable. The research goal is measurable improvement over simple baselines; honest negative results are still findings.

**One-line pitch:** *Someone had to scrub an 8-hour stream to find those 30 seconds. AutoClipper finds them for you.*

## 2. Problem

**Stream clipping is a real side hustle.** Popular streamers' highlights are reposted to short-form platforms by fan "clip accounts". Many streamers allow this, and some actively pay clippers. The bottleneck is **finding and choosing** moments:

1. **Scrubbing is slow.** Streams often run 4–10+ hours. Finding the best moments by hand takes hours per stream.
2. **Viewer clips are noisy.** A big stream can produce hundreds of viewer-made clips. Many duplicate the same moment, many are inside jokes, and their Twitch view counts reflect live context rather than how they'd perform as a standalone short.
3. **Small streams have no clips.** For smaller channels, or streams that just ended, few or no viewer clips exist, so there's nothing to choose from.
4. **Formatting is busywork.** Turning a 16:9 gameplay+facecam clip into a 9:16 captioned short is repetitive manual editing.

### Project focus

AutoClipper's focus is Twitch VODs and their available audience reactions:

- **Use what is already available:** viewer clips can seed discovery and support experiments, while signal-based analysis covers time that has no clips.
- **It's measured.** Every claim about quality is backed by an offline evaluation against baselines. The project's value is the evidence as much as the tool.

## 3. Users

- **Viewer:** missed a stream or lacks time to watch the whole broadcast; wants to browse and watch interesting highlights.
- **Streamer:** wants highlights from their own broadcasts to review, export or hand to an editor. Longer compilations are a possible later extension.
- **Clipper/editor:** wants to discover, preview, trim, caption and export selected moments for short-form destinations.

These audiences share one analysis and review workflow. Separate applications are unnecessary.

## 4. Goals and non-goals

### Goals

| # | Goal | How we'll know |
|---|---|---|
| G1 | Use available viewer clips intelligently | Useful candidates and duplicate handling, compared with a views baseline when applicable |
| G2 | Discover interesting moments when clips are sparse or absent | A discovery path independent of clip metadata, evaluated honestly against observed clips and human judgments |
| G3 | Export useful clips | Correct source-time trim, basic framing/aspect-ratio options and captions |
| G4 | Provide an appealing, understandable app | Enter VOD → understandable shortlist/timeline → preview → trim → export |
| G5 | Produce a credible data-science result | Reproducible evaluation, ablations ("which signals matter?") |

### Non-goals (for the course project)

- **Auto-publishing to social platforms.** Out of scope: platform API approval and scope creep. A clipper uploads the exported files themselves.
- **Platforms other than Twitch** as an input source.
- **Training on raw video end to end.** Video is optional and comes later (see [ADR-0004](../decisions/0004-staged-scope-chat-and-audio-first.md)).
- **Real-time / live-stream clipping.** Possible future work after the semester.
- **Predicting TikTok/Reels views** or any other platform's engagement.
- **Monetization** or revenue targets as a success metric.
- **Multi-user accounts, auth, billing.** The app is a single-user tool for the demo.

## 5. User stories

| ID | As a… | I want to… | So that… | Stage |
|---|---|---|---|---|
| US-1 | user | paste a supported Twitch VOD URL | I can start analysis easily | MVP |
| US-2 | streamer/editor | see available viewer clips alongside discovered moments, with duplicates handled | I can choose useful highlights | MVP |
| US-3 | viewer | browse an understandable shortlist or timeline | I can catch up on the stream | MVP; richer timeline optional |
| US-4 | user | preview a candidate | I can judge it before watching or exporting | MVP |
| US-5 | clipper/editor | export a vertical, captioned MP4 | I can prepare a short-form clip | MVP; smart crop optional |
| US-6 | user | get candidates from parts of the stream nobody clipped | I can discover missed moments | MVP |
| US-7 | user | analyze a supported VOD with zero viewer clips | the tool still offers signal-derived candidates | MVP |
| US-8 | streamer/editor | adjust start/end before export | I can fix a bad trim | MVP |
| US-9 | team member | inspect contributing signals where supported | we can debug and explain the analysis | Optional polish |
| US-10 | clipper | get candidates while a stream is still live | I can post before other clippers | Won't (this semester) |

## 6. Functional requirements

Requirement IDs are stable. Reference them in issues and PRs (e.g. "implements FR-3").

### Ingestion

- **FR-1** Accept a Twitch VOD URL or video ID and resolve its metadata (channel, duration, created time, muted segments) via the Twitch Helix API.
- **FR-2** Fetch retrievable viewer clips linked to that VOD with `vod_offset`, duration, view count and creation time. Handle query limits and report incomplete discovery coverage ([data sources](../data/data-sources.md)).
- **FR-3** Obtain authorized **chat replay** or an agreed substitute with per-message offsets; apply the approved retention/privacy policy before sharing.
- **FR-4** Obtain authorized **audio** for feature extraction; report missing or muted audio.
- **FR-5** Cache permitted artifacts with versions and expiry/deletion rules; report source/media availability separately from analytical availability.

### Analysis

- **FR-6** Merge overlapping viewer clips into **moments** (deduplication).
- **FR-7** Compute per-window chat and audio features (see [features](../ml/features.md)).
- **FR-8** Score and rank candidates, using API-derived information where useful. An existing-clip ranker is a possible intermediate prototype.
- **FR-9** Analyze across a supported VOD and propose moments independently of existing clips. Whole-VOD scoring or heuristic shortlisting with learned reranking are possible approaches; a second trained model is not required.
- **FR-10** Return, for each candidate, the score and start/end in VOD seconds. Top contributing signals ("why this moment") are optional polish (US-9), not an MVP requirement.

### Export

- **FR-11** Preview and cut the selected source-video interval; allow basic trim adjustment. Consume already aligned VOD timestamps without another chat-lag shift (see [labeling](../data/labeling.md)).
- **FR-12** Support basic framing, including 9:16 via center crop or a configured layout. Smart crop and further aspect ratios are optional polish.
- **FR-13** Burn in captions generated by speech-to-text.
- **FR-14** Output a playable MP4. Proposed vertical preset: H.264/AAC, 1080×1920, configurable duration; confirm destination constraints during export implementation.

### Interface

- **FR-15** Internal CLI tools for reproducible pipeline and experiment work.
- **FR-16** A web app for the semester demo (FastAPI + Next.js, [ADR-0007](../decisions/0007-web-application-stack.md)): VOD input → progress/errors → understandable highlights → preview → trim → export. UI development starts early with synthetic candidates and proceeds alongside analysis.

## 7. Non-functional requirements

| ID | Requirement | Target (Proposal) |
|---|---|---|
| NFR-1 | Runs on a typical student laptop (no GPU required) for v1/v2 | Chat + audio features for a 6-hour VOD in ≤ 10 minutes on CPU, excluding download time |
| NFR-2 | Reproducibility | Any reported metric can be regenerated from a commit + dataset version + seed |
| NFR-3 | Cross-platform | Windows, macOS, Linux |
| NFR-4 | Privacy | No raw chat identities in shared/processed outputs or logs; downloads may contain them transiently on the collector. Retention choice is OQ-9. No media or chat in git. |
| NFR-5 | Platform etiquette | Respects Twitch API rate limits and backs off on 429 |
| NFR-6 | Robustness | Handles missing chat, deleted VODs, muted segments, clips with null `vod_offset` |

## 8. Success metrics

**Offline evaluation is required; exact targets, budgets and metrics need team agreement** under OQ-1/OQ-15 and ADR-0009. The alternatives below are proposals, not two mandatory models. See [evaluation](../ml/evaluation.md).

| Stage | Metric | Must beat |
|---|---|---|
| Optional clip-ranking experiment | NDCG@10 / Precision@5 against an agreed relevance target | Random; Twitch views at the same observation time where applicable |
| Independent discovery | Recall and agreement with observed clips; window PR-AUC only as a weak-label diagnostic | Random, chat spike, loudness and combined heuristic at the same candidate budget |
| Both | Ablation: contribution of each signal family | n/a (reported as a finding) |

**Secondary (online, stretch, needs streamer permission):** post model-picked clips vs. randomly picked clips on a clip account and compare normalized views. See [evaluation § online experiment](../ml/evaluation.md#5-online-experiment-stretch).

**Product completion:** a non-team member can follow the review/export flow unassisted. A cached demo may be brief; fresh analysis time is reported separately. Research success means improvement on the agreed protocol, but an unfavorable result does not block UI/export completion.

> **No results exist yet.** This section defines how success will be measured, not what has been achieved.

## 9. Constraints and assumptions

- **Team:** four students working part time over the course term, with heavy use of AI coding agents.
- **Compute:** laptops. Free tiers only, unless the team decides otherwise.
- **Data access:** Twitch Helix API (official, free developer app). Chat replay and VOD download rely on **unofficial** tools (see [data sources](../data/data-sources.md)). This is a known risk.
- **VOD retention:** Twitch deletes past broadcasts after a retention period, so **collection must start early and run continuously**.
- **Course aim:** useful ML trained on real, authorized data, sensible experimental evaluation and a polished product. Neither a neural network nor TikTok-view prediction is a requirement.

## 10. Risks

The full register is in [`../project/risks.md`](../project/risks.md). The top three:

1. **Unofficial chat/VOD tooling breaks** → keep a live-chat logger as a backup source. Collect early.
2. **Label noise** (clips reflect live context, duplicates, clip-start bias) → moment merging, careful label definitions, ranking metrics robust to noise.
3. **Scope creep into video ML** → chat + audio first. Video only as a measured add-on.

## 11. Open questions

Tracked in [`../project/open-questions.md`](../project/open-questions.md). The most important one for this PRD is **OQ-1: what exactly is the v1 ranking target?** Twitch view count is available for existing clips, so predicting it for old clips isn't useful. The candidate framings are listed there.

## 12. Milestones

See [roadmap](roadmap.md).
