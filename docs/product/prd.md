# Product Requirements Document: AutoClipper

| | |
|---|---|
| **Status** | Clarified product direction; technical proposals and detailed acceptance parameters await team agreement. |
| **Last updated** | 2026-10-13 |
| **Related** | [Roadmap](roadmap.md) · [Architecture](../architecture/overview.md) · [Evaluation](../ml/evaluation.md) · [Open questions](../project/open-questions.md) |

> This PRD is a living document. Sections marked **Proposal** reflect the current best plan but haven't been ratified by the team.

The original course design establishes a whole-VOD discovery, preview and export experience for viewers, streamers and editors. The owner's 2026-10-13 clarification preserves that experience while keeping application form, detection strategy and model architecture open. Technical departures remain proposals under OQ-15 and [ADR-0009](../decisions/0009-analysis-and-evaluation-contract-repair.md).

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
- **Real-time / live-stream clipping** for v1 or v2. It's a stretch goal.
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
| US-10 | clipper | get candidates while a stream is still live | I can post before other clippers | Stretch |

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
- **FR-10** Return, for each candidate, the score, start/end in VOD seconds, and the top contributing signals.

### Export

- **FR-11** Preview and cut the selected source-video interval; allow basic trim adjustment. Consume already aligned VOD timestamps without another chat-lag shift (see [labeling](../data/labeling.md)).
- **FR-12** Support basic framing, including 9:16 via center crop or a configured layout. Smart crop and further aspect ratios are optional polish.
- **FR-13** Burn in captions generated by speech-to-text.
- **FR-14** Output a playable MP4. Proposed vertical preset: H.264/AAC, 1080×1920, configurable duration; confirm destination constraints during export implementation.

### Interface

- **FR-15** Internal CLI tools for reproducible pipeline and experiment work.
- **FR-16** An appealing application for the semester demo: VOD input → progress/errors → understandable highlights → preview → trim → export. Web versus desktop and detailed screens remain open (ADR-0007); UI development proceeds alongside analysis.

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
- **Course aim:** useful ML trained on real, authorized data, sensible experimental evaluation and a polished product. Confirm exact staff requirements under OQ-13; neither a neural network nor TikTok-view prediction is an established requirement.

## 10. Risks

The full register is in [`../project/risks.md`](../project/risks.md). The top three:

1. **Unofficial chat/VOD tooling breaks** → keep a live-chat logger as a backup source. Collect early.
2. **Label noise** (clips reflect live context, duplicates, clip-start bias) → moment merging, careful label definitions, ranking metrics robust to noise.
3. **Scope creep into video ML** → chat + audio first. Video only as a measured add-on.

## 11. Open questions

Tracked in [`../project/open-questions.md`](../project/open-questions.md). The most important one for this PRD is **OQ-1: what exactly is the v1 ranking target?** Twitch view count is available for existing clips, so predicting it for old clips isn't useful. The candidate framings are listed there.

## 12. Milestones

See [roadmap](roadmap.md).
