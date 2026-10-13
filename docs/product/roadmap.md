# Roadmap

| | |
|---|---|
| **Status** | Proposal. Implementation staging and dates await team agreement; semester product goals are in the PRD. |
| **Last updated** | 2026-10-13 |

The roadmap stages internal work while building the application alongside it. A clip-ranking prototype can exercise ingestion and export early, but M3 alone is not the finished semester product. The demo must include discovery independent of existing clips, understandable results, preview, basic trim and useful export ([PRD](prd.md)).

The original course design includes a tentative September-December 2026 schedule. Replan from the actual M0 state under OQ-13. Ranker-first remains an option ([ADR-0004](../decisions/0004-staged-scope-chat-and-audio-first.md)); one learned scorer may serve both discovery and ranking, and shortlisted analysis is also possible. Neither two models nor a neural-network sequence is a completion requirement.

```mermaid
flowchart LR
    M0[M0 Foundations] --> M1[M1 Data collection]
    M1 --> M2[M2 Dataset + baselines]
    M2 --> M3[M3 learned scoring + basic export]
    M2 --> M4[M4 independent discovery]
    M0 --> APP[App with synthetic candidates]
    APP --> M5[M5 integrated demo and polish]
    M3 --> M5
    M4 --> M5
    M5 -.-> S[Stretch goals]
    M1 -. runs continuously .-> M1
```

> **Start a small authorized data pilot promptly**, then collect at an agreed scale while app/export work proceeds. VODs expire, but urgency does not establish permission. Synthetic development can proceed while collection decisions are resolved.

---

## M0: Foundations

**Goal:** a repo where anyone, human or agent, can pick up a task and ship it consistently.

- [ ] Documentation, conventions, ADRs, issue/PR templates merged *(included in this combined documentation PR)*
- [x] Labels and `main` protection ruleset configured *(remote base established; temporary admin PR-only bypass for this PR must be removed after merge)*
- [ ] Ratify or amend proposed ADRs (tooling, layout, storage, license, web stack)
- [ ] Python project scaffold: `pyproject.toml`, `uv`, Ruff, Pyright, pytest, pre-commit
- [ ] CI on GitHub Actions: lint + type check + tests on every PR. Make these required checks.
- [ ] Config module and `.env` loading
- [ ] Twitch developer application registered (team-owned credentials, shared privately)
- [ ] Choose application form and initial review flow (ADR-0007); start UI/job-state work with synthetic candidates

**Exit criteria:** `uv sync && uv run pytest` passes on a fresh clone on Windows and macOS/Linux, and CI is green on `main`.

## M1: Data collection

**Goal:** a growing, versioned local corpus of VOD metadata, clips, chat and audio for a fixed set of channels.

- [ ] Choose target channels and categories ([collection plan](../data/collection-plan.md))
- [ ] Record source authorization, retention and privacy choice (OQ-9/OQ-17) before real collection
- [ ] Twitch API client: app token, Get Users, Get Videos, Get Clips with pagination and rate-limit handling
- [ ] Chat replay downloader wrapper (TwitchDownloader CLI) with username pseudonymization
- [ ] Audio-only VOD downloader (yt-dlp) with resumable downloads
- [ ] Scheduled collector: new VODs per channel, plus a clip refresh N days later so clips can accumulate
- [ ] Investigate live-chat backup only if replay access/reliability requires it
- [ ] Manifest/catalog of what's been collected, with dataset versioning

**Exit criteria:** an agreed pilot ingested with a reproducible catalog, immutable observations, coverage records and explicit missing-data status. Scale targets follow measured cost and channel coverage; arbitrary corpus counts do not block baselines.

## M2: Dataset and baselines

**Goal:** a clean, versioned training dataset and reproducible baseline comparisons.

- [ ] Moment merging (dedupe overlapping clips) per [labeling](../data/labeling.md)
- [ ] Windowing and window labels, with chat-lag handling
- [ ] Chat features and audio features per [features](../ml/features.md)
- [ ] Train/validation/test split by broadcaster and time, saved to disk
- [ ] Evaluation harness implementing [evaluation](../ml/evaluation.md) metrics, with bootstrap CIs
- [ ] Applicable baselines B0–B4 evaluated on the same population and candidate budget
- [ ] Agree the first learning target and evaluation protocol (OQ-1/OQ-15, ADR-0009)

**Exit criteria:** `autoclipper eval --baseline all` reproduces the baseline table from a clean checkout plus the dataset.

## M3: v1 clip ranker and basic export

**Goal:** useful learned scoring and basic export; an existing-clip prototype is one possible first integration.

- [ ] Train a simple model justified by the agreed target; compare with heuristics before adding complexity
- [ ] Ablations: chat only, audio only, both
- [ ] Export: cut, center/fixed-layout 9:16 crop, burned-in captions (faster-whisper)
- [ ] CLI: `autoclipper analyze <vod>` → ranked candidates JSON. `autoclipper export <candidate>` → MP4.
- [ ] Results write-up in `experiments/`

**Exit criteria:** a reproducible learned-model comparison and a supported VOD → exported MP4 flow work on a laptop. Baseline improvement is the research goal, reported separately from delivery readiness. Negative results prompt analysis, not suspension of app work.

## M4: v2 moment detector

**Goal:** independent discovery in VOD time nobody clipped. This may reuse the M3 scorer or combine heuristics and learned reranking.

- [ ] Whole-VOD signal analysis with an agreed discovery strategy; a separate classifier/sequence model is optional
- [ ] Candidate generation: peak picking, non-max suppression, boundary refinement
- [ ] Evaluation: recall of held-out human moments, precision@k, comparison against heuristics
- [ ] "Zero-clip VOD" mode: run on a VOD while hiding its clips, and measure how many held-out clips we recover
- [ ] Explanations: top contributing signals per candidate

**Exit criteria:** signal-derived candidates work without clip metadata; baseline comparisons and limitations are recorded. Hidden clips test independence from clip availability, not generalization to small audiences by itself.

## M5: Demo-ready app

**Goal:** integrate and polish the semester app. UI development starts in M0/M1, rather than after ML research finishes.

- [ ] Application/job integration using the agreed form (FastAPI + Next.js remains a proposal)
- [ ] VOD input → progress/errors → understandable highlights → preview → basic trim → export
- [ ] Smart 9:16 crop (face/facecam detection) if time allows
- [ ] Small permitted demo/evaluation media set retained before expiry; processed features alone cannot support preview/export
- [ ] Final results write-up and slides

**Exit criteria:** a non-team member can run the demo flow unassisted.

## Stretch goals (unordered, each needs its own ADR)

- **Video features:** scene cuts, motion, game-specific CV. Measured as an ablation ("does video add anything beyond chat + audio?").
- **Short-form performance model:** predict TikTok/Reels performance from external clip-account data (views normalized per account).
- **Online posting experiment:** model picks vs. random picks on a clip account, with streamer permission.
- **Live mode:** stream ingestion, low-latency scoring, candidates while the stream is live.
- **Clip refinement:** learned start/end boundaries instead of fixed padding.

## Out of scope

Auto-publishing to social platforms, non-Twitch sources, multi-user accounts. See [PRD § Non-goals](prd.md#non-goals-for-the-course-project).
