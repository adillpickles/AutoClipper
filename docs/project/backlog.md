# Implementation Backlog

| | |
|---|---|
| **Status** | Living. Copy-ready drafts; **none of these exist as GitHub issues yet.** |
| **Last updated** | 2026-10-16 |
| **Related** | [Roadmap](../product/roadmap.md) (milestones) · [PRD scope](../product/prd.md#scope-at-a-glance) · [Open questions](open-questions.md) · [Labels](labels.md) · [Writing tasks](../agents/writing-tasks.md) |

This file is the **canonical list of issue drafts** for the semester MVP. The [roadmap](../product/roadmap.md) stays the high-level milestone view; detailed tasks live only here.

## How to use this file

- **Publishing:** create the issues in the order below, using each heading as the exact issue title. Use the **Task** template. For `experiment` drafts use the **Experiment** template and write the hypothesis and success criterion before running anything; for `spike` drafts use the **Research spike** template. Fill "Depends on" with the linked issue numbers (`#N`) once those issues exist. Once a draft is published, the GitHub issue becomes the live record; add its number to the index in this file and don't keep editing the draft.
- **Picking one up:** start only when everything under **Depends on** is merged (or decided). See [CONTRIBUTING § 2](../../CONTRIBUTING.md#2-pick-up-an-issue).
- **Decision tasks** (`decision` label) are team choices recorded in an ADR or [open questions](open-questions.md). Agents can draft options; they don't make the call.
- **Readiness labels:** `agent-ready` means fully specified with no open decision upstream. `needs-decision` means the task waits on a decision task, directly or through a dependency. When that decision closes, re-check the draft, then swap `needs-decision` for `agent-ready`. Later drafts list **Details to settle** that the person picking them up refines first.
- **Paths** are written as plain repo paths (e.g. `docs/ml/features.md` § Normalization) so they still make sense when pasted into GitHub.

## Two tracks, built in parallel

- **App and export track:** scaffolds → synthetic data → FastAPI mock jobs + Next.js screens + FFmpeg export → wired to the real pipeline in Wave 5. It never waits on model results.
- **Data and ML track:** decisions → mocked ingestion adapters → authorized pilot → collector → dataset v1 → baselines and trained models → plugged into analysis.

Waves group work by when it can start. Tasks within a wave can mostly run in parallel. **Depends on** lists genuine prerequisites only.

## Index

<!-- index:start -->
| Wave | Issue title | Labels | Can start |
|---|---|---|---|
| 0 | [Set up team access and issue tracking](#set-up-team-access-and-issue-tracking) | `tooling` | Now |
| 0 | [Register the team Twitch developer app](#register-the-team-twitch-developer-app) | `tooling` `ingestion` | Now |
| 1 | [Scaffold the Python package with tooling and CI](#scaffold-the-python-package-with-tooling-and-ci) | `tooling` `agent-ready` | Now |
| 1 | [Scaffold the Next.js app with lint and CI](#scaffold-the-nextjs-app-with-lint-and-ci) | `frontend` `tooling` `agent-ready` | Now |
| 1 | [Decide real-data permissions and chat retention (OQ-17 and OQ-9)](#decide-real-data-permissions-and-chat-retention-oq-17-and-oq-9) | `decision` `data` | Now |
| 1 | [Choose pilot channels and data storage (OQ-4 and OQ-5)](#choose-pilot-channels-and-data-storage-oq-4-and-oq-5) | `decision` `data` `needs-decision` | After its decisions and dependencies |
| 1 | [Decide the first training target and label rules (OQ-1)](#decide-the-first-training-target-and-label-rules-oq-1) | `decision` `modeling` | Now |
| 1 | [Adopt the dataset construction rules (ADR-0009)](#adopt-the-dataset-construction-rules-adr-0009) | `decision` `data` | Now |
| 1 | [Adopt the v0 chat and audio feature set (ADR-0009)](#adopt-the-v0-chat-and-audio-feature-set-adr-0009) | `decision` `modeling` | Now |
| 1 | [Adopt the evaluation protocol (ADR-0009)](#adopt-the-evaluation-protocol-adr-0009) | `decision` `modeling` `needs-decision` | After its decisions and dependencies |
| 1 | [Define the job and candidate API contract (ADR-0009)](#define-the-job-and-candidate-api-contract-adr-0009) | `decision` `backend` `frontend` | Now |
| 2 | [Add the config module and logging setup](#add-the-config-module-and-logging-setup) | `tooling` `agent-ready` | After its dependencies merge |
| 2 | [Generate a synthetic VOD dataset for development](#generate-a-synthetic-vod-dataset-for-development) | `data` `agent-ready` | After its dependencies merge |
| 2 | [Build the Helix client for auth and VOD metadata](#build-the-helix-client-for-auth-and-vod-metadata) | `ingestion` `agent-ready` | After its dependencies merge |
| 2 | [Fetch a VOD's viewer clips with chunked queries](#fetch-a-vods-viewer-clips-with-chunked-queries) | `ingestion` `agent-ready` | After its dependencies merge |
| 2 | [Parse chat replay with pseudonymization](#parse-chat-replay-with-pseudonymization) | `ingestion` `agent-ready` | After its dependencies merge |
| 2 | [Download VOD audio and normalize it to 16 kHz mono](#download-vod-audio-and-normalize-it-to-16-khz-mono) | `ingestion` `agent-ready` | After its dependencies merge |
| 2 | [Cut and reframe a source interval to a vertical MP4](#cut-and-reframe-a-source-interval-to-a-vertical-mp4) | `export` `agent-ready` | After its dependencies merge |
| 2 | [Burn in captions with faster-whisper](#burn-in-captions-with-faster-whisper) | `export` `agent-ready` | After its dependencies merge |
| 2 | [Build windows and moments from the adopted rules](#build-windows-and-moments-from-the-adopted-rules) | `data` `needs-decision` | After its decisions and dependencies |
| 2 | [Compute chat features per window](#compute-chat-features-per-window) | `modeling` `needs-decision` | After its decisions and dependencies |
| 2 | [Compute audio features per window](#compute-audio-features-per-window) | `modeling` `needs-decision` | After its decisions and dependencies |
| 2 | [Implement evaluation metrics and the report harness](#implement-evaluation-metrics-and-the-report-harness) | `modeling` `needs-decision` | After its decisions and dependencies |
| 2 | [Implement the B0–B4 baselines](#implement-the-b0b4-baselines) | `modeling` `needs-decision` | After its decisions and dependencies |
| 2 | [Generate candidates from window scores](#generate-candidates-from-window-scores) | `modeling` `needs-decision` | After its decisions and dependencies |
| 2 | [Build the FastAPI job API with a mock analysis job](#build-the-fastapi-job-api-with-a-mock-analysis-job) | `backend` `needs-decision` | After its decisions and dependencies |
| 2 | [Build the VOD input and job status screens](#build-the-vod-input-and-job-status-screens) | `frontend` `needs-decision` | After its decisions and dependencies |
| 2 | [Build the highlights shortlist and timeline](#build-the-highlights-shortlist-and-timeline) | `frontend` `needs-decision` | After its decisions and dependencies |
| 2 | [Build the preview player with trim and export controls](#build-the-preview-player-with-trim-and-export-controls) | `frontend` `needs-decision` | After its decisions and dependencies |
| 2 | [Add the export command and API endpoint](#add-the-export-command-and-api-endpoint) | `export` `backend` `needs-decision` | After its decisions and dependencies |
| 2 | [Connect the web app to the API](#connect-the-web-app-to-the-api) | `frontend` `backend` `needs-decision` | After its decisions and dependencies |
| 3 | [Add the ingest command and collection catalog](#add-the-ingest-command-and-collection-catalog) | `ingestion` `needs-decision` | After its decisions and dependencies |
| 3 | [Run a small authorized data pilot](#run-a-small-authorized-data-pilot) | `data` `ingestion` `needs-decision` | After its decisions and dependencies |
| 3 | [Explore pilot data for chat lag and clip alignment](#explore-pilot-data-for-chat-lag-and-clip-alignment) | `spike` `data` `modeling` `needs-decision` | After its decisions and dependencies |
| 3 | [Build the scheduled collector](#build-the-scheduled-collector) | `ingestion` `needs-decision` | After its decisions and dependencies |
| 3 | [Build window labels and saved splits](#build-window-labels-and-saved-splits) | `data` `needs-decision` | After its decisions and dependencies |
| 3 | [Add the model training command](#add-the-model-training-command) | `modeling` `needs-decision` | After its decisions and dependencies |
| 3 | [Build and quality-check dataset v1](#build-and-quality-check-dataset-v1) | `data` `needs-decision` | After its decisions and dependencies |
| 4 | [Run the baselines on dataset v1](#run-the-baselines-on-dataset-v1) | `experiment` `modeling` `needs-decision` | After its decisions and dependencies |
| 4 | [Train and evaluate a logistic regression model](#train-and-evaluate-a-logistic-regression-model) | `experiment` `modeling` `needs-decision` | After its decisions and dependencies |
| 4 | [Train and evaluate a LightGBM model](#train-and-evaluate-a-lightgbm-model) | `experiment` `modeling` `needs-decision` | After its decisions and dependencies |
| 5 | [Add the analyze command for a VOD](#add-the-analyze-command-for-a-vod) | `backend` `modeling` `needs-decision` | After its decisions and dependencies |
| 5 | [Run real analysis jobs in the API worker](#run-real-analysis-jobs-in-the-api-worker) | `backend` `needs-decision` | After its decisions and dependencies |
| 5 | [Preview and export real VOD media](#preview-and-export-real-vod-media) | `backend` `frontend` `export` `needs-decision` | After its decisions and dependencies |
| 5 | [Use the trained model in analysis](#use-the-trained-model-in-analysis) | `modeling` `backend` `needs-decision` | After its decisions and dependencies |
| 5 | [Add an end-to-end test on a synthetic VOD](#add-an-end-to-end-test-on-a-synthetic-vod) | `tooling` `needs-decision` | After its decisions and dependencies |
| 6 | [Write up results against the baselines](#write-up-results-against-the-baselines) | `modeling` `docs` `needs-decision` | After its decisions and dependencies |
| 6 | [Prepare and rehearse the demo](#prepare-and-rehearse-the-demo) | `tooling` `docs` `needs-decision` | After its decisions and dependencies |
| Optional | [Add speech signals for shortlisted candidates](#add-speech-signals-for-shortlisted-candidates) | `modeling` `needs-decision` | After its decisions and dependencies |
| Optional | [Run signal ablations for chat and audio](#run-signal-ablations-for-chat-and-audio) | `experiment` `modeling` `needs-decision` | After its decisions and dependencies |
| Optional | [Run the blind taste test](#run-the-blind-taste-test) | `experiment` `modeling` `needs-decision` | After its decisions and dependencies |
| Optional | [Stack the facecam over gameplay in vertical exports](#stack-the-facecam-over-gameplay-in-vertical-exports) | `export` `needs-decision` | After its decisions and dependencies |
| Optional | [Show why each moment ranked](#show-why-each-moment-ranked) | `frontend` `modeling` `needs-decision` | After its decisions and dependencies |
| Optional | [Cache processed VODs](#cache-processed-vods) | `backend` `needs-decision` | After its decisions and dependencies |
| Optional | [Show detailed job progress and clear errors](#show-detailed-job-progress-and-clear-errors) | `frontend` `backend` `needs-decision` | After its decisions and dependencies |
| Optional | [Run the evaluation harness in CI](#run-the-evaluation-harness-in-ci) | `tooling` `modeling` `needs-decision` | After its decisions and dependencies |
<!-- index:end -->

---

## Wave 0: Team setup (owner, start now)

### Set up team access and issue tracking

**Labels:** `tooling`
**Depends on:** None
**Can run in parallel with:** Register the team Twitch developer app; Scaffold the Python package with tooling and CI

**Goal:** Teammates can be assigned issues, review PRs and browse a labeled backlog. Owner-only: needs repo admin rights.

**Scope:** GitHub collaborators, labels, issues and project board. No repo file changes except recording issue numbers in this backlog's index.
**Out of scope:** Code. Changing `main` protections beyond what's listed here.
**Docs:** `docs/project/labels.md` § Changing live labels · `docs/project/team-process.md` · `docs/development/git-workflow.md`

**Acceptance criteria:**
- [ ] All three teammates have write access (OQ-11); invitations accepted.
- [ ] Live labels match `docs/project/labels.md`, applied with the mapping in that doc so existing assignments are preserved.
- [ ] Issues created from this backlog in index order, with dependencies linked as `#N`; issue numbers recorded in the index here (small docs PR).
- [ ] GitHub Project board created with columns Backlog → Ready → In progress → In review → Done.
- [ ] After "Scaffold the Python package with tooling and CI" merges: its CI checks are required on `main`.
- [ ] Once teammates are reviewing PRs: the setup-period admin bypass is removed and the ruleset re-checked.

**Verify:** A teammate can self-assign an issue and is selectable as a PR reviewer. `gh label list` matches `docs/project/labels.md`.

### Register the team Twitch developer app

**Labels:** `tooling` `ingestion`
**Depends on:** None
**Can run in parallel with:** Set up team access and issue tracking

**Goal:** One team-owned Twitch app with credentials shared privately, so ingestion work can run live smoke tests once data use is authorized.

**Scope:** Twitch developer console; private credential sharing; `docs/development/setup.md` if steps differ.
**Out of scope:** Any data collection. Committing credentials anywhere.
**Docs:** `docs/development/setup.md` § Twitch credentials · `docs/data/data-sources.md` § 1. Twitch Helix API (official)

**Acceptance criteria:**
- [ ] OQ-8 resolved (one shared app is the proposal) and moved to Resolved in `docs/project/open-questions.md`.
- [ ] App registered; Client ID and Secret shared privately (not in Slack/Discord history or git).
- [ ] Each teammate can put the values in their local `.env`.

**Verify:** A teammate requests an app access token locally with the documented client-credentials call and gets `200`.

---

## Wave 1: Scaffolds and decisions (start now, all in parallel)

The two scaffolds are the first coding tasks. The decision tasks can start the same day; each one unblocks a specific part of Wave 2+.

### Scaffold the Python package with tooling and CI

**Labels:** `tooling` `agent-ready`
**Depends on:** None
**Can run in parallel with:** Scaffold the Next.js app with lint and CI; all Wave 1 decision tasks

**Goal:** A Python package that installs with one command and runs lint, type checks and tests locally and in CI, so every later task has a working base.

**Scope:** `pyproject.toml`, `uv.lock`, `src/autoclipper/__init__.py`, `src/autoclipper/cli.py`, `tests/test_cli.py`, `.pre-commit-config.yaml`, `.github/workflows/ci.yml`, plus doc updates listed below.
**Out of scope:** Config module, any pipeline code, the `web/` frontend.
**Docs:** `docs/decisions/0002-language-and-tooling.md` · `docs/decisions/0003-repository-layout.md` · `docs/development/conventions.md` · `docs/development/testing.md` · `docs/development/setup.md`

**Acceptance criteria:**
- [ ] `uv` project for Python 3.12 with the `src/` layout; package `autoclipper`; `uv.lock` committed.
- [ ] Dev tools per ADR-0002: Ruff (line length 100), Pyright (`standard`), pytest + pytest-cov, pre-commit. Runtime: Typer for the CLI.
- [ ] `uv run autoclipper --help` works; a pytest smoke test covers it.
- [ ] Pytest registers a `live` marker and **excludes it by default**.
- [ ] Pre-commit runs Ruff (lint + format), strips notebook outputs, and blocks large files and private keys.
- [ ] GitHub Actions on every PR and on pushes to `main`: `uv sync --locked`, Ruff check, Ruff format check, Pyright, pytest. Runs on Ubuntu and Windows.
- [ ] `AGENTS.md` § 5, `docs/development/setup.md` (with tested PowerShell steps) and `docs/development/testing.md` commands match what actually works; `docs/project/status.md` updated.

**Verify:** Fresh clone → `uv sync && uv run pytest && uv run ruff check . && uv run pyright` passes on Windows and on macOS or Linux. CI is green on the PR.

### Scaffold the Next.js app with lint and CI

**Labels:** `frontend` `tooling` `agent-ready`
**Depends on:** None
**Can run in parallel with:** Scaffold the Python package with tooling and CI; all Wave 1 decision tasks

**Goal:** A Next.js + TypeScript + Tailwind app in `web/` that builds locally and in CI, so UI work can start right away.

**Scope:** `web/` (App Router, strict TypeScript, Tailwind, ESLint + Prettier, one placeholder home page), `web/README.md`, `.github/workflows/web.yml`, frontend notes in `docs/development/conventions.md` § 10 and `docs/development/setup.md`.
**Out of scope:** Real screens, API client, backend changes.
**Docs:** `docs/decisions/0007-web-application-stack.md` · `docs/development/conventions.md` § 10. Frontend (proposal)

**Acceptance criteria:**
- [ ] `web/` created with the standard Next.js tooling; lockfile committed. Use npm unless the team agrees otherwise in the PR.
- [ ] Node version pinned to the current Active LTS (check nodejs.org) via `engines` and `.nvmrc`.
- [ ] Scripts: `dev`, `build`, `lint`, `typecheck` (`tsc --noEmit`), `format`.
- [ ] Separate workflow `web.yml`, triggered for changes under `web/`: install, lint, typecheck, build.
- [ ] Frontend conventions in `docs/development/conventions.md` § 10 updated to what was set up (folder layout, one typed API client module).
- [ ] PR includes a screenshot of the placeholder page.

**Verify:** `cd web && npm ci && npm run lint && npm run typecheck && npm run build` passes. CI is green on the PR.

### Decide real-data permissions and chat retention (OQ-17 and OQ-9)

**Labels:** `decision` `data`
**Depends on:** None
**Can run in parallel with:** every other Wave 1 task

**Goal:** A written, source-by-source answer to "what real Twitch data may we collect, keep, show and export, and what chat data do we retain?" Real collection, the pilot and real preview/export wait on this.

**Scope:** `docs/data/data-governance.md`, `docs/data/data-sources.md`, `docs/project/open-questions.md`. Ask course staff if anything is unclear.
**Out of scope:** Building collectors. Treating public availability as permission.
**Docs:** `docs/data/data-governance.md` § Authorization boundary before real collection (OQ-17) · `docs/data/data-governance.md` § 2. Privacy: chat data · `docs/data/data-sources.md`

**Acceptance criteria:**
- [ ] For each source (Helix metadata and clips, chat replay, audio download, video segments for preview/export, clip downloads, creator-provided media), record: allowed use, storage location, retention and deletion, sharing, and evidence.
- [ ] Preview/display route chosen (Twitch player embed, served retained media, or creator-provided media).
- [ ] OQ-9 decided: aggregate-only chat statistics versus sanitized messages, with retained fields, hash linkage scope, mention redaction and deletion deadline.
- [ ] If a source is not permitted, the alternative is named and affected backlog drafts are updated.
- [ ] OQ-9 and OQ-17 moved to Resolved; `needs-decision` removed from drafts that waited only on this.

**Verify:** A teammate can answer "may I download and keep this VOD's audio for two weeks?" from the docs alone.

### Choose pilot channels and data storage (OQ-4 and OQ-5)

**Labels:** `decision` `data` `needs-decision`
**Depends on:**
- Decide real-data permissions and chat retention (OQ-17 and OQ-9)

**Can run in parallel with:** Decide the first training target and label rules (OQ-1); Adopt the dataset construction rules (ADR-0009)

**Goal:** Know which channels the pilot and dataset use, how much data to aim for, which machine runs the collector, and where shared data lives.

**Scope:** `configs/channels.yaml` (login + one-line reason per channel), `docs/data/collection-plan.md`, `docs/project/open-questions.md`.
**Out of scope:** Running collection.
**Docs:** `docs/data/collection-plan.md` § Channel selection (proposal) · `docs/data/collection-plan.md` § Data sharing within the team

**Acceptance criteria:**
- [ ] Pilot list: 2–3 channels, about 5 VODs total, consistent with the permissions decision.
- [ ] Candidate list for the dataset, mixing content types and channel sizes, with a target VOD count for dataset v1.
- [ ] Collector host and shared-storage location chosen; access limited to the team.
- [ ] OQ-4 and OQ-5 moved to Resolved.

**Verify:** `configs/channels.yaml` exists and every entry has a reason; the collection plan names the host and storage.

### Decide the first training target and label rules (OQ-1)

**Labels:** `decision` `modeling`
**Depends on:** None
**Can run in parallel with:** every other Wave 1 task

**Goal:** Agree what the first model learns to predict, so labels, splits and evaluation can be built. Use the documented proposal unless the team prefers another option; revisit with a new label-rules version after the pilot if needed.

**Scope:** `docs/ml/modeling-plan.md`, `docs/data/labeling.md`, an ADR (new, or an ADR-0009 update), `docs/project/open-questions.md`.
**Out of scope:** Training anything.
**Docs:** `docs/ml/modeling-plan.md` § OQ-1: the v1 target needs a decision · `docs/data/labeling.md` § 4. Window labels · `docs/data/labeling.md` § 6. Normalization across channels

**Acceptance criteria:**
- [ ] Target chosen (proposal: independent discovery, with windows overlapping observed viewer-clip moments as positives).
- [ ] Label rules fixed as `label_rules_version` v0: positive definition, graded relevance (if any), negative buffer, view normalization or "views not used".
- [ ] What the model may use at inference is stated (no clip-derived inputs for discovery).
- [ ] Whether a moment-level ranking of existing viewer clips is in the MVP (it's optional per ADR-0004).
- [ ] Recorded in an ADR; OQ-1 moved to Resolved.

**Verify:** Someone can hand-label a synthetic VOD with three overlapping clips using only the written rules, and two people get the same labels.

### Adopt the dataset construction rules (ADR-0009)

**Labels:** `decision` `data`
**Depends on:** None
**Can run in parallel with:** every other Wave 1 task

**Goal:** Fix how clips are snapshotted, how VODs are cut into windows and how overlapping clips merge into moments, so ingestion and dataset code have a contract.

**Scope:** `docs/decisions/0009-analysis-and-evaluation-contract-repair.md` (adopted subset), `docs/data/labeling.md`, `docs/architecture/data-model.md`.
**Out of scope:** Labels and splits (covered by the training-target decision).
**Docs:** `docs/data/labeling.md` § 2. From clips to moments · `docs/data/labeling.md` § 3. Windows · `docs/architecture/data-model.md` § `clips`

**Acceptance criteria:**
- [ ] Windows: `WINDOW_S` and `STRIDE_S` (proposal 10/10; the design doc used 20/10), last-partial-window rule, muted-segment flag.
- [ ] Clip snapshots: manifest fields, capture/discovery horizon for later-created clips, coverage record for saturated query ranges.
- [ ] Moments: `GAP_S`, half-open intervals, overlong-group handling, deterministic `moment_id` and tie-break.
- [ ] Each rule has a small synthetic worked example in the docs.
- [ ] Adopted parts marked as such in ADR-0009, `labeling.md` and `data-model.md`.

**Verify:** The worked examples give unambiguous expected outputs that a test can copy.

### Adopt the v0 chat and audio feature set (ADR-0009)

**Labels:** `decision` `modeling`
**Depends on:** None
**Can run in parallel with:** every other Wave 1 task

**Goal:** Pick the first, small set of chat and audio features and the clip-link filter, so feature code has a fixed contract.

**Scope:** `docs/ml/features.md`, ADR-0009 adoption note.
**Out of scope:** Speech, video and embedding features (optional polish/future).
**Docs:** `docs/ml/features.md` § Chat features (`features_chat`) · `docs/ml/features.md` § Audio features (`features_audio`) · `docs/ml/features.md` § Chat preprocessing and leakage

**Acceptance criteria:**
- [ ] `chat-v0` subset chosen (at least `chat_msg_rate`, `chat_msg_rate_z`, `chat_unique_rate`, `chat_emote_rate` and lead/lag columns).
- [ ] `audio-v0` subset chosen (at least `audio_rms_mean`, `audio_rms_max`, `audio_rms_max_z`, `audio_is_muted`).
- [ ] Clip-link filter patterns listed explicitly, with synthetic messages that must and must not be filtered.
- [ ] Empty-window and zero-variance behavior stated per column.
- [ ] Adopted subset marked in `features.md` with `feature_version` values.

**Verify:** Every adopted column has a one-line definition precise enough to write a unit test from.

### Adopt the evaluation protocol (ADR-0009)

**Labels:** `decision` `modeling` `needs-decision`
**Depends on:**
- Decide the first training target and label rules (OQ-1)

**Can run in parallel with:** Adopt the dataset construction rules (ADR-0009); Adopt the v0 chat and audio feature set (ADR-0009)

**Goal:** Fix how results are scored before anything is measured, so baselines and models are compared on the same rules.

**Scope:** `docs/ml/evaluation.md`, ADR-0009 adoption note.
**Out of scope:** The human taste test rubric (optional polish).
**Docs:** `docs/ml/evaluation.md` § 2. Baselines · `docs/ml/evaluation.md` § 4. v2 metrics: moment detector

**Acceptance criteria:**
- [ ] Primary metric chosen (proposal: Recall@k per VOD-hour; the design doc used Precision@10 and NDCG@10).
- [ ] Matching rule chosen (IoU ≥ 0.3 or peak ± 10 s, versus the design doc's ± 15 s) and candidate budget *k*.
- [ ] Evaluation population, VODs with no positives, short VODs and ties defined.
- [ ] Which of B0–B4 apply, and the bootstrap settings (resample VODs, seed).
- [ ] Recorded in ADR-0009 and `evaluation.md`.

**Verify:** A hand-worked toy example (2 VODs, a few candidates and moments) is written down with its expected metric values.

### Define the job and candidate API contract (ADR-0009)

**Labels:** `decision` `backend` `frontend`
**Depends on:** None
**Can run in parallel with:** every other Wave 1 task

**Goal:** A shared contract between the FastAPI backend and the Next.js app, so both sides can build against fake data in parallel.

**Scope:** `docs/architecture/data-model.md` § `candidates` (API/CLI output, JSON) plus a new API section; ADR-0009 adoption note. Docs only.
**Out of scope:** Implementing endpoints or screens.
**Docs:** `docs/architecture/overview.md` § 4.7 Interfaces · `docs/architecture/data-model.md` § `candidates` (API/CLI output, JSON)

**Acceptance criteria:**
- [ ] Job: create request (VOD URL or ID), states (queued, running, done, failed), progress fields, error codes (invalid link, VOD unavailable, chat missing, audio missing).
- [ ] Candidate fields: `candidate_id`, `rank`, `score`, `start_s`, `end_s`, `source` enum (viewer clip / discovered / both), `n_viewer_clips`, optional `top_signals`.
- [ ] Export: request (candidate, trimmed `start_s`/`end_s`, layout, captions on/off), status and download.
- [ ] Media: how the app gets a playable preview URL, with "media unavailable" as a valid state.
- [ ] Synthetic example payloads for each, in the docs.

**Verify:** A frontend and a backend developer can each build their side from the doc without asking the other.

---

## Wave 2: Build on synthetic data

Everything here works without real Twitch data. Ingestion adapters use mocked responses; features, evaluation, the app and export use the synthetic dataset.

### Add the config module and logging setup

**Labels:** `tooling` `agent-ready`
**Depends on:**
- Scaffold the Python package with tooling and CI

**Can run in parallel with:** Scaffold the Next.js app with lint and CI; Wave 1 decision tasks

**Goal:** One place that reads settings from env/`.env`, and one logging setup, so no other module touches `os.environ` or configures logging itself.

**Scope:** `src/autoclipper/config.py`, `src/autoclipper/logging.py`, `tests/test_config.py`. Adds pydantic-settings (ADR-0002).
**Out of scope:** Any pipeline stage.
**Docs:** `docs/development/conventions.md` § 4. Configuration · `docs/development/conventions.md` § 6. Errors and logging · `.env.example`

**Acceptance criteria:**
- [ ] Settings for every `.env.example` variable; data/artifact dirs as `Path` with documented defaults; secrets as secret types that never print.
- [ ] Twitch credentials optional at load, with a clear error when a Helix call needs them and they're missing.
- [ ] Cached `get_settings()`; logging configured once with key=value format and `AUTOCLIPPER_LOG_LEVEL`.
- [ ] Tests: env overrides, defaults, secrets absent from `repr` and logs, Windows-style paths.

**Verify:** `uv run pytest tests/test_config.py`

### Generate a synthetic VOD dataset for development

**Labels:** `data` `agent-ready`
**Depends on:**
- Add the config module and logging setup

**Can run in parallel with:** Build the Helix client for auth and VOD metadata; Cut and reframe a source interval to a vertical MP4

**Goal:** A deterministic generator for fake VODs (metadata, clips, chat, audio and a short video), so feature, evaluation, app and export work can proceed before real data exists.

**Scope:** `src/autoclipper/synthetic/`, an `autoclipper synth --out <dir> --seed <n>` command, tests. Output goes under the git-ignored data dir.
**Out of scope:** Real data. Committing generated media.
**Docs:** `docs/architecture/data-model.md` · `docs/development/testing.md` § Fixtures

**Acceptance criteria:**
- [ ] 2–3 fake VODs of a few minutes each, with fake broadcaster IDs and names like `user_001`.
- [ ] Clips include overlapping, near-touching and disjoint ones, one null `vod_offset_s` and one null `video_id`.
- [ ] Chat has bursts a few seconds after clip moments, plus quiet stretches, emotes and one fake clip URL message.
- [ ] 16 kHz mono WAV with loud bursts near moments, a short generated video (e.g. FFmpeg test pattern), and one muted segment.
- [ ] Columns follow the **proposed** `data-model.md` tables (prototype use, noted in the PR).
- [ ] Same seed gives byte-identical tabular output.

**Verify:** `uv run autoclipper synth --out <tmp> --seed 1` twice and compare; `uv run pytest tests/synthetic`

### Build the Helix client for auth and VOD metadata

**Labels:** `ingestion` `agent-ready`
**Depends on:**
- Add the config module and logging setup

**Can run in parallel with:** Parse chat replay with pseudonymization; Download VOD audio and normalize it to 16 kHz mono

**Goal:** A typed Twitch Helix client that gets an app token, resolves channels and fetches VOD metadata, tested entirely against recorded responses.

**Scope:** `src/autoclipper/ingest/twitch_api.py`, `tests/ingest/`, scrubbed JSON fixtures. Pick an HTTP client (httpx suggested) and justify it in the PR.
**Out of scope:** Get Clips (next task). Persisting anything.
**Docs:** `docs/data/data-sources.md` § 1. Twitch Helix API (official) · `docs/architecture/data-model.md` § `vods`

**Acceptance criteria:**
- [ ] Client-credentials token, cached until near expiry, refreshed once on `401`.
- [ ] Get Users (login → ID, `broadcaster_type`) and Get Videos (by ID; by user with `type=archive`, paginated).
- [ ] Rate limits read from `Ratelimit-*` headers; `429` waits until reset; capped retries on `5xx`.
- [ ] `duration` parsed to seconds (`"3h8m33s"`, `"45m"`, `"59s"`, `"1h0m0s"`); `muted_segments: null` → `[]`; missing/malformed fields raise a clear error.
- [ ] IDs stay strings; timestamps are UTC-aware.
- [ ] HTTP is injected; unit tests make no network calls. One `@pytest.mark.live` smoke test, excluded by default.

**Verify:** `uv run pytest tests/ingest`

### Fetch a VOD's viewer clips with chunked queries

**Labels:** `ingestion` `agent-ready`
**Depends on:**
- Build the Helix client for auth and VOD metadata

**Can run in parallel with:** Parse chat replay with pseudonymization; Download VOD audio and normalize it to 16 kHz mono

**Goal:** Retrieve the viewer clips linked to a VOD despite Get Clips' ~1,000-result cap, and report how complete the result is.

**Scope:** `src/autoclipper/ingest/twitch_api.py` (or `clips.py`), tests with scrubbed fixtures.
**Out of scope:** Saving snapshot files (the ingest command does that once the snapshot rules are adopted).
**Docs:** `docs/data/data-sources.md` § 1. Twitch Helix API (official) (Get Clips) · `docs/architecture/data-model.md` § `clips`

**Acceptance criteria:**
- [ ] Queries by `broadcaster_id` over short `started_at`/`ended_at` chunks, subdividing chunks that approach the cap; deduplicates by `clip_id`.
- [ ] Keeps rows whose `video_id` matches; empty `video_id` → null; null `vod_offset` kept (flagged for exclusion later).
- [ ] The discovery horizon is a parameter (no default horizon is decided here).
- [ ] Creator IDs are pseudonymized (HMAC with the configured salt); each row keeps its `fetched_at`.
- [ ] Returns a coverage record: ranges queried, ranges that hit the cap, totals.
- [ ] Tests: pagination, saturated-range subdivision, duplicates across chunks, null/empty edge cases.

**Verify:** `uv run pytest tests/ingest`

### Parse chat replay with pseudonymization

**Labels:** `ingestion` `agent-ready`
**Depends on:**
- Add the config module and logging setup

**Can run in parallel with:** Build the Helix client for auth and VOD metadata; Download VOD audio and normalize it to 16 kHz mono

**Goal:** Turn TwitchDownloader chat JSON into clean, pseudonymized message records in memory, ready for chat features.

**Scope:** `src/autoclipper/ingest/chat.py` (CLI wrapper + parser), synthetic JSON fixtures, tests.
**Out of scope:** Writing messages to disk (waits on the retention decision). Feature computation.
**Docs:** `docs/data/data-sources.md` § 2. Chat replay: TwitchDownloader (unofficial) · `docs/data/data-governance.md` § 2. Privacy: chat data · `docs/architecture/data-model.md` § `chat_messages`

**Acceptance criteria:**
- [ ] Wrapper runs `TwitchDownloaderCLI chatdownload` through an injectable runner and records the tool version.
- [ ] Field names confirmed from TwitchDownloader's own source or docs for the pinned version, cited in the PR (no guessed fields).
- [ ] Output columns follow the proposed `chat_messages` table; `user_hash` via HMAC; `@mentions` replaced with `@user`.
- [ ] Raw names never appear in output, logs or exceptions (tested).
- [ ] Malformed messages counted and logged; an unexpected file format raises `ChatDownloadError`.

**Verify:** `uv run pytest tests/ingest/test_chat.py`

### Download VOD audio and normalize it to 16 kHz mono

**Labels:** `ingestion` `agent-ready`
**Depends on:**
- Add the config module and logging setup

**Can run in parallel with:** Build the Helix client for auth and VOD metadata; Parse chat replay with pseudonymization

**Goal:** Download a VOD's audio track only and convert it to the 16 kHz mono WAV that audio features expect.

**Scope:** `src/autoclipper/ingest/media.py`, tests. Adds yt-dlp (ADR-0002). CI installs FFmpeg if not already present.
**Out of scope:** Video downloads. Running real downloads in CI.
**Docs:** `docs/data/data-sources.md` § 3. VOD media: yt-dlp (unofficial) · `docs/architecture/overview.md` § 4.1 Ingest

**Acceptance criteria:**
- [ ] Audio-only download to `data/raw/audio/<video_id>.<ext>`, resumable, with typed errors for unavailable or subscriber-only VODs.
- [ ] FFmpeg normalization to 16 kHz mono WAV; duration probed and returned.
- [ ] yt-dlp is mocked in unit tests; normalization is tested on a generated tone file.
- [ ] Real downloads only in a `live` test, and only once real-data use is authorized.

**Verify:** `uv run pytest tests/ingest/test_media.py`

### Cut and reframe a source interval to a vertical MP4

**Labels:** `export` `agent-ready`
**Depends on:**
- Add the config module and logging setup

**Can run in parallel with:** Generate a synthetic VOD dataset for development; Wave 2 ingestion tasks

**Goal:** Turn a source-time interval of a video into a 1080×1920 H.264/AAC MP4 with a basic 9:16 crop.

**Scope:** `src/autoclipper/export/` (`cut.py`, `reframe.py`, `encode.py`), tests.
**Out of scope:** Captions, smart crop, fetching media from Twitch.
**Docs:** `docs/architecture/overview.md` § 4.6 Export · `docs/data/labeling.md` § 5. Chat lag (export applies no lag shift)

**Acceptance criteria:**
- [ ] FFmpeg commands are built as data (argument lists) and run by a separate runner.
- [ ] Interval clamped to media bounds; optional context padding is explicit and separate from the trim; no chat-lag shift.
- [ ] Layouts: center crop, and one fixed layout (configurable region); output 1080×1920 H.264/AAC.
- [ ] Unit tests on command construction; one integration test on a generated 5-second video checks duration and resolution.

**Verify:** `uv run pytest tests/export`

### Burn in captions with faster-whisper

**Labels:** `export` `agent-ready`
**Depends on:**
- Cut and reframe a source interval to a vertical MP4

**Can run in parallel with:** Wave 2 data and app tasks

**Goal:** Add readable burned-in captions to exported clips using speech-to-text that runs on a CPU.

**Scope:** `src/autoclipper/export/captions.py`, tests. Adds faster-whisper (ADR-0002); model size configurable.
**Out of scope:** Speech-based ranking signals. Caption styling beyond one clean default.
**Docs:** `docs/architecture/overview.md` § 4.6 Export · `docs/product/prd.md` § Export

**Acceptance criteria:**
- [ ] Transcribe the clip's audio with word timestamps; group words into short caption lines sized for 9:16.
- [ ] Write a subtitle file and burn it in via FFmpeg; captions on/off option.
- [ ] Unit tests use a fake transcript (no model download); a slow, opt-in test runs the real model on a generated clip.
- [ ] PR includes a screenshot of a captioned frame.

**Verify:** `uv run pytest tests/export/test_captions.py`

### Build windows and moments from the adopted rules

**Labels:** `data` `needs-decision`
**Depends on:**
- Adopt the dataset construction rules (ADR-0009)
- Generate a synthetic VOD dataset for development

**Can run in parallel with:** Wave 2 ingestion, app and export tasks

**Goal:** Implement the adopted windowing and moment-merging rules so features, labels and candidates share one time grid.

**Scope:** `src/autoclipper/labels/windows.py`, `src/autoclipper/labels/moments.py`, tests.
**Out of scope:** Window labels and splits.
**Docs:** `docs/data/labeling.md` § 2. From clips to moments · `docs/data/labeling.md` § 3. Windows · `docs/architecture/data-model.md` § `windows`

**Acceptance criteria:**
- [ ] `windows` table exactly per the adopted rules, including the last partial window and `is_muted`.
- [ ] `moments` from one pinned clip set: null offsets dropped and counted, half-open intervals, deterministic IDs and ties.
- [ ] Tests reproduce every worked example from the adopted rules, plus the cases in `docs/development/testing.md`.

**Verify:** `uv run pytest tests/labels`

### Compute chat features per window

**Labels:** `modeling` `needs-decision`
**Depends on:**
- Adopt the v0 chat and audio feature set (ADR-0009)
- Build windows and moments from the adopted rules
- Parse chat replay with pseudonymization

**Can run in parallel with:** Compute audio features per window

**Goal:** Produce the adopted `chat-v0` features for every window.

**Scope:** `src/autoclipper/features/chat.py`, tests.
**Out of scope:** Features not in the adopted subset.
**Docs:** `docs/ml/features.md` § Chat features (`features_chat`) · `docs/ml/features.md` § Normalization · `docs/ml/features.md` § Chat preprocessing and leakage

**Acceptance criteria:**
- [ ] Clip-link filter applied before every aggregate, with filtered counts logged (no message text).
- [ ] Raw rates are 0 for empty windows; z-scores follow the formula, with 0 only for zero-variance VODs; lead/lag handle VOD edges.
- [ ] Output keyed by `window_id` with `feature_version`.
- [ ] Tests cover the adopted definitions and the synthetic filter examples.

**Verify:** `uv run pytest tests/features/test_chat.py`

### Compute audio features per window

**Labels:** `modeling` `needs-decision`
**Depends on:**
- Adopt the v0 chat and audio feature set (ADR-0009)
- Build windows and moments from the adopted rules

**Can run in parallel with:** Compute chat features per window

**Goal:** Produce the adopted `audio-v0` features for every window from 16 kHz mono audio.

**Scope:** `src/autoclipper/features/audio.py`, tests. Adds librosa/soundfile (ADR-0002).
**Out of scope:** Pretrained audio embeddings.
**Docs:** `docs/ml/features.md` § Audio features (`features_audio`) · `docs/ml/features.md` § Normalization

**Acceptance criteria:**
- [ ] Adopted columns computed per window; muted windows flagged.
- [ ] Tests on generated audio: a loud burst raises `audio_rms_max_z` in the right window; silence and zero variance handled.
- [ ] Runtime for a 6-hour VOD estimated from a scaled synthetic run and noted in the PR (target: well under 10 minutes on a laptop CPU).

**Verify:** `uv run pytest tests/features/test_audio.py`

### Implement evaluation metrics and the report harness

**Labels:** `modeling` `needs-decision`
**Depends on:**
- Adopt the evaluation protocol (ADR-0009)
- Add the config module and logging setup

**Can run in parallel with:** Compute chat features per window; Compute audio features per window

**Goal:** One harness that turns predictions plus ground truth into the adopted metrics with bootstrap confidence intervals.

**Scope:** `src/autoclipper/eval/`, an `autoclipper eval` command, tests.
**Out of scope:** Running real experiments.
**Docs:** `docs/ml/evaluation.md` · `docs/ml/experiments.md`

**Acceptance criteria:**
- [ ] Adopted primary and secondary metrics and matching rule, each tested against the hand-worked examples from the protocol decision.
- [ ] Bootstrap resamples VODs with a fixed seed.
- [ ] Writes `artifacts/reports/<run_id>/metrics.json` and a Markdown summary with dataset version, git SHA and seed.

**Verify:** `uv run pytest tests/eval`

### Implement the B0–B4 baselines

**Labels:** `modeling` `needs-decision`
**Depends on:**
- Implement evaluation metrics and the report harness
- Compute chat features per window
- Compute audio features per window

**Can run in parallel with:** Generate candidates from window scores

**Goal:** The simple reference scorers every model must be compared against, behind the same interface trained models will use.

**Scope:** `src/autoclipper/models/base.py` (fit/predict/save/load interface), `src/autoclipper/models/baselines.py`, tests.
**Out of scope:** Trained models.
**Docs:** `docs/ml/evaluation.md` § 2. Baselines · `docs/architecture/overview.md` § 4.4 Models

**Acceptance criteria:**
- [ ] B0 random (seeded), B2 chat spike, B3 loudness, B4 chat + loudness; B1 Twitch views where clips apply.
- [ ] All share one model interface and run through the harness on the synthetic dataset.
- [ ] Tests: each baseline ranks a planted synthetic peak as expected.

**Verify:** `uv run pytest tests/models` and one harness run on synthetic data.

### Generate candidates from window scores

**Labels:** `modeling` `needs-decision`
**Depends on:**
- Build windows and moments from the adopted rules
- Define the job and candidate API contract (ADR-0009)

**Can run in parallel with:** Implement the B0–B4 baselines

**Goal:** Turn any per-window score into a ranked, deduplicated candidate list, including viewer-clip moments, in the agreed output format.

**Scope:** `src/autoclipper/models/candidates.py` (or similar), tests.
**Out of scope:** Choosing the scorer.
**Docs:** `docs/ml/modeling-plan.md` § 3. v2 candidate generation · `docs/architecture/data-model.md` § `candidates` (API/CLI output, JSON)

**Acceptance criteria:**
- [ ] Smoothing, peak picking, non-max suppression and context padding, with parameters in config and documented defaults.
- [ ] Viewer-clip moments merged with discovered candidates; `source` set per the contract.
- [ ] A zero-clip mode hides all clip information through selection.
- [ ] Output validates against the adopted candidate contract.

**Verify:** `uv run pytest tests/models/test_candidates.py`

### Build the FastAPI job API with a mock analysis job

**Labels:** `backend` `needs-decision`
**Depends on:**
- Define the job and candidate API contract (ADR-0009)
- Generate a synthetic VOD dataset for development

**Can run in parallel with:** Build the VOD input and job status screens; Build the highlights shortlist and timeline

**Goal:** A running backend that implements the agreed job API with a fake job, so the frontend can integrate long before the real pipeline exists.

**Scope:** `src/autoclipper/api/`, an `autoclipper serve` command, tests. Adds FastAPI and a server (e.g. uvicorn), per ADR-0007.
**Out of scope:** Real analysis. Hosting.
**Docs:** `docs/decisions/0007-web-application-stack.md` · `docs/architecture/overview.md` § 4.7 Interfaces

**Acceptance criteria:**
- [ ] Create job, job status and job results endpoints exactly per the contract.
- [ ] Jobs run in an in-process background worker, never inside the request; the mock steps through states and returns synthetic candidates.
- [ ] Serves the synthetic preview video so the app can play something.
- [ ] Invalid links and failed jobs return the agreed error codes; CORS allows the local frontend.
- [ ] Tests with FastAPI's test client cover the full job lifecycle.

**Verify:** `uv run pytest tests/api`; `uv run autoclipper serve` and open the OpenAPI docs page.

### Build the VOD input and job status screens

**Labels:** `frontend` `needs-decision`
**Depends on:**
- Scaffold the Next.js app with lint and CI
- Define the job and candidate API contract (ADR-0009)

**Can run in parallel with:** Build the highlights shortlist and timeline; Build the preview player with trim and export controls

**Goal:** The first screens: paste a VOD link and watch the job progress, using fake API responses.

**Scope:** `web/` pages and components, the typed API client module with a mock mode backed by fixture JSON from the contract.
**Out of scope:** Calling the real backend (done in "Connect the web app to the API").
**Docs:** `docs/product/prd.md` § Scope at a glance · `docs/agents/writing-tasks.md` § Task types that need extra care

**Acceptance criteria:**
- [ ] URL input accepts `twitch.tv/videos/<id>` links and bare IDs; invalid input shows a clear message.
- [ ] Status view covers queued, running (with progress), done and failed states.
- [ ] Empty, loading and error states designed; layout works at laptop and phone widths.
- [ ] PR includes screenshots of each state.

**Verify:** `npm run lint && npm run typecheck && npm run build`; click through every state in mock mode.

### Build the highlights shortlist and timeline

**Labels:** `frontend` `needs-decision`
**Depends on:**
- Scaffold the Next.js app with lint and CI
- Define the job and candidate API contract (ADR-0009)

**Can run in parallel with:** Build the VOD input and job status screens; Build the preview player with trim and export controls

**Goal:** Show the ranked moments as a list and on a timeline across the whole VOD, so users can jump to the best parts.

**Scope:** `web/` results page and components (custom canvas/SVG timeline), mock data from the contract.
**Out of scope:** Video playback (next task), explanations ("why this moment" is optional polish).
**Docs:** `docs/decisions/0007-web-application-stack.md` · `docs/product/prd.md` § 5. User stories

**Acceptance criteria:**
- [ ] Ranked list with rank, time range, score and source badge (viewer clip / discovered / both).
- [ ] Timeline spanning the VOD duration with markers; selecting in either view selects in both.
- [ ] Handles zero candidates and a VOD with no viewer clips.
- [ ] PR includes screenshots.

**Verify:** `npm run lint && npm run typecheck && npm run build`; check against the contract's example payloads.

### Build the preview player with trim and export controls

**Labels:** `frontend` `needs-decision`
**Depends on:**
- Scaffold the Next.js app with lint and CI
- Define the job and candidate API contract (ADR-0009)

**Can run in parallel with:** Build the VOD input and job status screens; Build the highlights shortlist and timeline

**Goal:** Let the user watch a candidate, adjust its start and end, and request an export.

**Scope:** `web/` preview component, trim controls, export panel (layout + captions toggle) calling the mock client.
**Out of scope:** Real media routes (decided with data permissions); actual export processing.
**Docs:** `docs/product/prd.md` § Export · `docs/architecture/overview.md` § 4.6 Export

**Acceptance criteria:**
- [ ] Plays the selected interval from a media URL (the synthetic sample in mock mode); shows a clear "media unavailable" state.
- [ ] Start/end trim controls, clamped to valid bounds, with reset.
- [ ] Export panel sends trimmed bounds, layout and captions choice, and shows export status.
- [ ] PR includes screenshots (before/after trimming).

**Verify:** `npm run lint && npm run typecheck && npm run build`; trim and export a mock candidate.

### Add the export command and API endpoint

**Labels:** `export` `backend` `needs-decision`
**Depends on:**
- Cut and reframe a source interval to a vertical MP4
- Burn in captions with faster-whisper
- Build the FastAPI job API with a mock analysis job

**Can run in parallel with:** Build the VOD input and job status screens; Build the highlights shortlist and timeline; Build the preview player with trim and export controls

**Goal:** One export pipeline (cut → reframe → captions → encode) usable from the CLI and the API, ready for the app's Export button.

**Scope:** `autoclipper export` command and the export endpoints from the contract.
**Out of scope:** Real VOD media (see "Preview and export real VOD media"). Frontend wiring (done in "Connect the web app to the API").
**Docs:** `docs/architecture/overview.md` § 4.6 Export · `docs/architecture/data-model.md` § `candidates` (API/CLI output, JSON)

**Acceptance criteria:**
- [ ] `autoclipper export` takes a media file, `start_s`, `end_s`, layout and captions flag, and writes to `artifacts/exports/`.
- [ ] API export runs in the background worker with status and download per the contract, using the synthetic preview video.
- [ ] Trimmed bounds from the request are respected exactly (no chat-lag shift).
- [ ] PR includes a screenshot of an exported clip.

**Verify:** `uv run pytest tests/export tests/api`; call the export endpoint for a synthetic candidate and play the result.

### Connect the web app to the API

**Labels:** `frontend` `backend` `needs-decision`
**Depends on:**
- Build the VOD input and job status screens
- Build the highlights shortlist and timeline
- Build the preview player with trim and export controls
- Add the export command and API endpoint

**Can run in parallel with:** Wave 2 data and ML tasks

**Goal:** The app runs against the local FastAPI backend end to end (with the mock job), not just fixtures, including export.

**Scope:** `web/` API client configuration (API base URL from env), polling, export download, docs for running both locally.
**Out of scope:** Real analysis and real VOD media.
**Docs:** `docs/development/setup.md` · `docs/decisions/0007-web-application-stack.md`

**Acceptance criteria:**
- [ ] API base URL from an environment variable; mock mode still available.
- [ ] Paste link → job progresses → results → preview plays → trim → export downloads a playable 9:16 captioned MP4, all against the local backend.
- [ ] Backend errors surface in the UI with the agreed messages.
- [ ] `docs/development/setup.md` explains running backend and frontend together; PR includes a short screen recording or screenshots.

**Verify:** Run `uv run autoclipper serve` and `npm run dev`, then walk the full flow.

---

## Wave 3: Real data and the dataset

Real collection starts only after the permissions decision. Labels, splits and the training command can be built on synthetic data in the meantime.

### Add the ingest command and collection catalog

**Labels:** `ingestion` `needs-decision`
**Depends on:**
- Fetch a VOD's viewer clips with chunked queries
- Parse chat replay with pseudonymization
- Download VOD audio and normalize it to 16 kHz mono
- Decide real-data permissions and chat retention (OQ-17 and OQ-9)
- Adopt the dataset construction rules (ADR-0009)

**Can run in parallel with:** Build window labels and saved splits

**Goal:** `autoclipper ingest <vod>` gathers one VOD's metadata, clip snapshot, chat and audio, storing only what the permissions decision allows, and records everything in a catalog.

**Scope:** `src/autoclipper/ingest/` (orchestration, `catalog.py`), CLI command, tests with mocked adapters.
**Out of scope:** Scheduling (the collector task).
**Docs:** `docs/data/collection-plan.md` · `docs/architecture/data-model.md` § `catalog` · `docs/architecture/overview.md` § 5. Storage layout (proposal)

**Acceptance criteria:**
- [ ] Writes the adopted clip snapshot (with manifest) and the approved chat representation; raw chat deleted after parsing per policy.
- [ ] Idempotent and resumable: re-running skips finished parts; failures and unavailable VODs are recorded in the catalog.
- [ ] Tool versions recorded; no raw usernames on disk outside the transient raw file.

**Details to settle:** exact retained chat form and deletion step (from the permissions decision).

**Verify:** `uv run pytest tests/ingest` with mocked adapters; one authorized live run on a single VOD.

### Run a small authorized data pilot

**Labels:** `data` `ingestion` `needs-decision`
**Depends on:**
- Add the ingest command and collection catalog
- Choose pilot channels and data storage (OQ-4 and OQ-5)
- Register the team Twitch developer app

**Can run in parallel with:** Build window labels and saved splits; Add the model training command

**Goal:** Ingest about 5 VODs from the pilot channels to measure cost, coverage and data quality before scaling, and secure permitted demo media while it's still available.

**Scope:** Running ingestion; a written report at `docs/data/pilot-report.md` (aggregates only, no data files).
**Out of scope:** Scaling up collection.
**Docs:** `docs/data/collection-plan.md` § Targets (proposal) · `docs/data/data-governance.md`

**Acceptance criteria:**
- [ ] Report: download times, disk use, chat download success, clips per VOD-hour, % null offsets, saturated clip-query ranges.
- [ ] Permitted demo/evaluation media saved per the permissions decision, with its deletion date.
- [ ] Problems found are filed as `bug` issues; no raw data, media or usernames in git or GitHub.

**Verify:** The catalog shows the pilot VODs with status; the report's numbers trace back to catalog rows.

### Explore pilot data for chat lag and clip alignment

**Labels:** `spike` `data` `modeling` `needs-decision`
**Depends on:**
- Run a small authorized data pilot
- Compute chat features per window
- Compute audio features per window

**Can run in parallel with:** Build the scheduled collector

**Goal:** Check the assumptions the labels and features rely on before building the full dataset. Time box: about one day.

**Scope:** A notebook (outputs cleared) and a short findings section added to `docs/data/pilot-report.md`.
**Out of scope:** Changing adopted rules directly; propose changes as follow-up decisions with a new version.
**Docs:** `docs/data/labeling.md` § 5. Chat lag · `docs/data/labeling.md` § 1. What a viewer clip tells us (and what it doesn't)

**Acceptance criteria:**
- [ ] Estimated chat lag (chat-rate peaks relative to clip starts) with a plot.
- [ ] Where the payoff sits inside clips (clip-start bias) and how often chat/audio spikes line up with clips.
- [ ] Recommendations: keep or change window size, lead features, negative buffer.

**Verify:** The notebook reruns top to bottom from the pilot catalog.

### Build the scheduled collector

**Labels:** `ingestion` `needs-decision`
**Depends on:**
- Run a small authorized data pilot
- Choose pilot channels and data storage (OQ-4 and OQ-5)

**Can run in parallel with:** Explore pilot data for chat lag and clip alignment; Add the model training command

**Goal:** Collect new VODs from the chosen channels every day, and refresh clip snapshots on schedule, before VODs expire.

**Scope:** `autoclipper collect` command reading `configs/channels.yaml`; scheduling instructions for the chosen host (Task Scheduler or cron).
**Out of scope:** GitHub Actions cron (media size and runtime limits make it unsuitable).
**Docs:** `docs/data/collection-plan.md` § Collector design

**Acceptance criteria:**
- [ ] Finds new archive VODs per channel and ingests them; schedules clip refresh snapshots per the adopted horizon.
- [ ] Polite: respects rate limits, sequential media downloads.
- [ ] Writes a daily summary (new VODs, failures, disk use).
- [ ] Running on the chosen host, with a week of successful runs noted in the issue.

**Verify:** Two consecutive runs: the second ingests only new items.

### Build window labels and saved splits

**Labels:** `data` `needs-decision`
**Depends on:**
- Decide the first training target and label rules (OQ-1)
- Build windows and moments from the adopted rules

**Can run in parallel with:** Add the ingest command and collection catalog; Run a small authorized data pilot

**Goal:** Turn moments into training labels and freeze a broadcaster-grouped train/val/test split, so experiments are reproducible.

**Scope:** `src/autoclipper/labels/` (labels, `splits.py`), an `autoclipper build-dataset` command writing the dataset manifest, tests on synthetic data.
**Out of scope:** Building dataset v1 from real data.
**Docs:** `docs/data/labeling.md` § 4. Window labels · `docs/data/labeling.md` § 8. Splits · `docs/architecture/data-model.md` § Dataset versioning

**Acceptance criteria:**
- [ ] `window_labels` per the adopted label rules; missing ground truth is never written as `False`.
- [ ] Split by broadcaster with a seed, saved to `data/splits/<version>.json`, never regenerated silently.
- [ ] The quality checks in `docs/data/labeling.md` § 10 run automatically and print a summary.
- [ ] Tests: no broadcaster in two splits; deterministic for a seed.

**Verify:** `uv run pytest tests/labels`; `uv run autoclipper build-dataset` on the synthetic dataset.

### Add the model training command

**Labels:** `modeling` `needs-decision`
**Depends on:**
- Build window labels and saved splits
- Implement the B0–B4 baselines

**Can run in parallel with:** Run a small authorized data pilot; Build the scheduled collector

**Goal:** A config-driven training entry point for logistic regression and LightGBM, so experiments are reproducible commands rather than notebooks.

**Scope:** `src/autoclipper/models/` (logistic regression, LightGBM), `autoclipper train --config`, tests on synthetic data. Adds scikit-learn and LightGBM (ADR-0002).
**Out of scope:** Reporting results on real data.
**Docs:** `docs/ml/modeling-plan.md` § 2. Model progression · `docs/ml/modeling-plan.md` § 4. Training practices · `docs/ml/experiments.md`

**Acceptance criteria:**
- [ ] Both models implement the shared model interface; preprocessing fitted on train only.
- [ ] Each run saves model, config, feature list, dataset version, git SHA and seed under `artifacts/models/<run_id>/`.
- [ ] Class imbalance handled (class weights); tuning uses validation only.
- [ ] Training on the synthetic dataset beats B0 on the planted signal (a sanity test, not a result).

**Verify:** `uv run pytest tests/models`; `uv run autoclipper train --config <synthetic config>`.

### Build and quality-check dataset v1

**Labels:** `data` `needs-decision`
**Depends on:**
- Build the scheduled collector
- Build window labels and saved splits
- Compute chat features per window
- Compute audio features per window

**Can run in parallel with:** Add the analyze command for a VOD

**Goal:** The first frozen real dataset at the agreed scale, with features, labels and splits, that every reported experiment cites.

**Scope:** Running `build-dataset` on collected VODs; a dataset summary in `experiments/README.md` or `docs/data/` (counts only).
**Out of scope:** Training.
**Docs:** `docs/data/labeling.md` § 10. Quality checks (run on every dataset build) · `docs/architecture/data-model.md` § Dataset versioning

**Acceptance criteria:**
- [ ] Dataset `v1` manifest pins VOD IDs, snapshots, rule and feature versions, and the split.
- [ ] Quality checks pass or exceptions are explained; a manual spot-check of 10 positives and 10 negatives is recorded.
- [ ] Shared via the agreed storage; nothing in git.

**Details to settle:** final VOD count, based on what the collector gathered.

**Verify:** A teammate rebuilds `v1` from the shared inputs and gets the same counts.

---

## Wave 4: Baselines and trained models

### Run the baselines on dataset v1

**Labels:** `experiment` `modeling` `needs-decision`
**Depends on:**
- Build and quality-check dataset v1
- Implement the B0–B4 baselines
- Generate candidates from window scores

**Can run in parallel with:** Add the analyze command for a VOD

**Goal:** The reference numbers every model is compared with.

**Scope:** `experiments/exp-001-baselines/` (config + README); results on the validation split.
**Out of scope:** Test-split numbers (only for the final write-up).
**Docs:** `docs/ml/experiments.md` · `docs/ml/evaluation.md` § 6. Reporting format

**Acceptance criteria:**
- [ ] Hypothesis and primary metric written in the issue before running.
- [ ] All applicable B0–B4 on the same population and candidate budget, with 95% bootstrap CIs.
- [ ] README follows the results template; numbers only from this run.

**Verify:** Rerunning the documented command reproduces `metrics.json`.

### Train and evaluate a logistic regression model

**Labels:** `experiment` `modeling` `needs-decision`
**Depends on:**
- Run the baselines on dataset v1
- Add the model training command

**Can run in parallel with:** Train and evaluate a LightGBM model

**Goal:** The first trained model, compared honestly with the baselines, and coefficients showing which signals matter.

**Scope:** `experiments/exp-NNN-logreg/`.
**Out of scope:** Hyperparameter search beyond a small budget.
**Docs:** `docs/ml/modeling-plan.md` § 2. Model progression · `docs/ml/experiments.md`

**Acceptance criteria:**
- [ ] Validation results next to B0–B4 with CIs, over at least 3 seeds where randomness applies.
- [ ] Coefficients reported; conclusion says supported, refuted or inconclusive, including if a baseline wins.

**Verify:** Rerunning the documented command reproduces the reported metrics.

### Train and evaluate a LightGBM model

**Labels:** `experiment` `modeling` `needs-decision`
**Depends on:**
- Run the baselines on dataset v1
- Add the model training command

**Can run in parallel with:** Train and evaluate a logistic regression model

**Goal:** A stronger tabular model, measured the same way, to see whether non-linear combinations of chat and audio help.

**Scope:** `experiments/exp-NNN-lightgbm/`.
**Out of scope:** Neural models (not required).
**Docs:** `docs/ml/modeling-plan.md` § 2. Model progression · `docs/ml/modeling-plan.md` § 4. Training practices

**Acceptance criteria:**
- [ ] Budgeted tuning on validation only (≤ 30 trials), search space recorded.
- [ ] Validation results next to B0–B4 and logistic regression, with CIs; honest conclusion.

**Verify:** Rerunning the documented command reproduces the reported metrics.

---

## Wave 5: Integration

The app switches from mock jobs to the real pipeline. It uses the B4 heuristic by default, so integration doesn't wait for a model to win.

### Add the analyze command for a VOD

**Labels:** `backend` `modeling` `needs-decision`
**Depends on:**
- Add the ingest command and collection catalog
- Compute chat features per window
- Compute audio features per window
- Implement the B0–B4 baselines
- Generate candidates from window scores

**Can run in parallel with:** Wave 4 experiments

**Goal:** `autoclipper analyze <vod>` goes from a VOD link to ranked candidates in the agreed format.

**Scope:** Orchestration module and CLI command; tests with mocked ingestion and the synthetic dataset.
**Out of scope:** Training. The API worker (next task).
**Docs:** `docs/architecture/overview.md` § 3. Pipeline · `docs/architecture/data-model.md` § `candidates` (API/CLI output, JSON)

**Acceptance criteria:**
- [ ] Ingest (cached) → windows → features → scoring → candidates JSON.
- [ ] Scorer is configurable: B4 by default, or a saved model artifact; output records which was used.
- [ ] Works on a VOD with zero viewer clips; reports missing chat or audio clearly instead of failing silently.
- [ ] Stage timings logged.

**Verify:** `uv run pytest`; `uv run autoclipper analyze` on a synthetic VOD and on one authorized real VOD.

### Run real analysis jobs in the API worker

**Labels:** `backend` `needs-decision`
**Depends on:**
- Add the analyze command for a VOD
- Build the FastAPI job API with a mock analysis job

**Can run in parallel with:** Use the trained model in analysis

**Goal:** Jobs created from the app run the real analysis pipeline.

**Scope:** `src/autoclipper/api/` worker; progress reporting per stage.
**Out of scope:** Hosting; multi-user concurrency.
**Docs:** `docs/architecture/overview.md` § 4.7 Interfaces

**Acceptance criteria:**
- [ ] The job worker calls the analyze pipeline; progress reflects real stages.
- [ ] Bad link, expired VOD and missing chat map to the agreed error codes.
- [ ] The mock job remains available for frontend development.

**Verify:** `uv run pytest tests/api`; analyze one authorized VOD from the app.

### Preview and export real VOD media

**Labels:** `backend` `frontend` `export` `needs-decision`
**Depends on:**
- Decide real-data permissions and chat retention (OQ-17 and OQ-9)
- Run real analysis jobs in the API worker
- Add the export command and API endpoint

**Can run in parallel with:** Use the trained model in analysis

**Goal:** Preview and export real moments using the media route the permissions decision allows.

**Scope:** Media access layer (embed or permitted segment fetch), app preview, export from real media.
**Out of scope:** Smart crop.
**Docs:** `docs/data/data-governance.md` · `docs/architecture/overview.md` § 4.6 Export

**Acceptance criteria:**
- [ ] Preview uses the approved route; unavailable media shows the "media unavailable" state with a reason.
- [ ] Export fetches only permitted segments, never chat-lag shifted, and produces a 9:16 captioned MP4.
- [ ] Exported files follow the retention rules.

**Details to settle:** route implementation depends on the permissions decision.

**Verify:** Preview and export one candidate from an authorized real VOD in the app; PR includes screenshots.

### Use the trained model in analysis

**Labels:** `modeling` `backend` `needs-decision`
**Depends on:**
- Add the analyze command for a VOD
- Train and evaluate a logistic regression model

**Can run in parallel with:** Preview and export real VOD media

**Goal:** Ship the best validated scorer in the app: a trained model if it beats B4 on validation, otherwise B4, documented either way.

**Scope:** Model selection note, default scorer configuration, model artifact loading in analysis.
**Out of scope:** Using the test split to choose.
**Docs:** `docs/ml/evaluation.md` § 1. Principles

**Acceptance criteria:**
- [ ] Choice made from validation results only, with the reasoning in `docs/project/status.md`.
- [ ] Analysis loads the chosen artifact and records its run ID in the output.
- [ ] If LightGBM finishes later and wins on validation, swapping it in is a config change.

**Verify:** `uv run autoclipper analyze` on a synthetic VOD shows the chosen scorer in its output.

### Add an end-to-end test on a synthetic VOD

**Labels:** `tooling` `needs-decision`
**Depends on:**
- Add the analyze command for a VOD
- Add the export command and API endpoint

**Can run in parallel with:** Preview and export real VOD media

**Goal:** One CI test that runs the whole pipeline on synthetic data, so integration breaks are caught on every PR.

**Scope:** `tests/integration/`, CI setup (FFmpeg available).
**Out of scope:** Network or real data.
**Docs:** `docs/development/testing.md` § Test types

**Acceptance criteria:**
- [ ] Synthetic VOD → mocked ingest → features → candidates → export of the top candidate.
- [ ] Asserts the planted moment ranks near the top and the export is a valid 1080×1920 MP4.
- [ ] Runs in CI in a few minutes.

**Verify:** `uv run pytest tests/integration` locally and in CI.

---

## Wave 6: Demo and results

Demo of Demos is **Fri Dec 4, 2026**; final presentations are **Fri Dec 11, 2026**.

### Write up results against the baselines

**Labels:** `modeling` `docs` `needs-decision`
**Depends on:**
- Train and evaluate a logistic regression model
- Train and evaluate a LightGBM model

**Can run in parallel with:** Prepare and rehearse the demo

**Goal:** One honest, reproducible results summary for slides and the README.

**Scope:** `experiments/README.md` index, a final test-split evaluation run, results section linked from the README.
**Out of scope:** New experiments.
**Docs:** `docs/ml/evaluation.md` § 6. Reporting format · `docs/ml/experiments.md` § Rules

**Acceptance criteria:**
- [ ] Test-split numbers computed once for the chosen scorer and baselines, with CIs.
- [ ] Every number links to an `experiments/` entry; negative or mixed results stated plainly.
- [ ] Limitations listed (weak labels, channel mix, coverage).

**Verify:** Each number in the write-up is reproducible from its linked command.

### Prepare and rehearse the demo

**Labels:** `tooling` `docs` `needs-decision`
**Depends on:**
- Preview and export real VOD media
- Add an end-to-end test on a synthetic VOD

**Can run in parallel with:** Write up results against the baselines

**Goal:** A demo that works on demo night even if the network or Twitch doesn't.

**Scope:** Demo runbook (e.g. `docs/demo.md`), cached analysis of permitted demo VODs, fallback screen recording, final README/setup/status updates.
**Out of scope:** New features.
**Docs:** `docs/product/prd.md` § 8. Success metrics · `docs/project/risks.md`

**Acceptance criteria:**
- [ ] Runbook: start the app locally on Windows and macOS, demo script, and what to do if something fails.
- [ ] Permitted demo VODs pre-analyzed and cached; a recorded fallback video of the full flow.
- [ ] A non-team member completes paste → preview → trim → export without help.
- [ ] Uses the trained model if "Use the trained model in analysis" has merged; otherwise B4, stated honestly.
- [ ] README and `docs/project/status.md` describe what actually exists.

**Verify:** Full rehearsal on a second laptop, offline from cached data.

---

## Optional polish (Should)

Start these only after the MVP runs end to end (PRD scope rule). Each needs refinement before work starts.

### Add speech signals for shortlisted candidates

**Labels:** `modeling` `needs-decision`
**Depends on:**
- Add the analyze command for a VOD
- Train and evaluate a LightGBM model

**Can run in parallel with:** Run signal ablations for chat and audio

**Goal:** Use Whisper transcripts (keywords, exclamations, speech rate) on the shortlist only, and measure whether reranking improves.

**Scope:** New `speech` feature family in `docs/ml/features.md`, reranking experiment.
**Out of scope:** Transcribing whole VODs.
**Docs:** `docs/ml/features.md` · `docs/decisions/0004-staged-scope-chat-and-audio-first.md`

**Acceptance criteria:**
- [ ] Feature definitions added to `features.md` (new feature family; ADR update if the contract changes).
- [ ] Experiment compares with and without speech on validation, with CIs.

**Verify:** Experiment README reproducible from its command.

### Run signal ablations for chat and audio

**Labels:** `experiment` `modeling` `needs-decision`
**Depends on:**
- Train and evaluate a LightGBM model

**Can run in parallel with:** Add speech signals for shortlisted candidates

**Goal:** Answer "which signals matter?": chat only vs. audio only vs. both, plus lead features.

**Scope:** `experiments/exp-NNN-ablations/`.
**Out of scope:** New features.
**Docs:** `docs/ml/modeling-plan.md` § 5. Ablations we plan to report

**Acceptance criteria:**
- [ ] Ablations from the modeling plan on validation with CIs; conclusions stated honestly.

**Verify:** Reproducible from the documented command.

### Run the blind taste test

**Labels:** `experiment` `modeling` `needs-decision`
**Depends on:**
- Use the trained model in analysis
- Preview and export real VOD media

**Can run in parallel with:** Show why each moment ranked

**Goal:** Classmates rate our clips against the chat-spike baseline's clips without knowing which is which.

**Scope:** Rubric and sampling decision first (OQ-15), then the study and write-up.
**Out of scope:** Posting clips publicly.
**Docs:** `docs/ml/evaluation.md` § Human audit (separate quality assessment)

**Acceptance criteria:**
- [ ] Rubric (1–5 or yes/maybe/no), sampling and rater instructions agreed before the study.
- [ ] Shuffled, blinded clips; two raters per clip; agreement reported; only permitted media shown.

**Verify:** Results table reproducible from the recorded ratings.

### Stack the facecam over gameplay in vertical exports

**Labels:** `export` `needs-decision`
**Depends on:**
- Cut and reframe a source interval to a vertical MP4
- Preview and export real VOD media

**Can run in parallel with:** Cache processed VODs

**Goal:** A smart 9:16 layout: facecam on top, gameplay below, using face detection rather than a fixed crop.

**Scope:** `src/autoclipper/export/reframe.py` layout option; detection model as an optional dependency group.
**Out of scope:** Training a detector.
**Docs:** `docs/architecture/overview.md` § 4.6 Export

**Acceptance criteria:**
- [ ] Detects the facecam region on sampled frames and falls back to center crop when unsure.
- [ ] PR includes before/after screenshots.

**Verify:** Export the same moment with both layouts and compare.

### Show why each moment ranked

**Labels:** `frontend` `modeling` `needs-decision`
**Depends on:**
- Use the trained model in analysis
- Connect the web app to the API

**Can run in parallel with:** Show detailed job progress and clear errors

**Goal:** A short "why this moment" note per candidate (e.g. "chat spike + loud audio"), without implying it proves quality.

**Scope:** `top_signals` in the candidate output (per the contract) and a small UI element.
**Out of scope:** SHAP dashboards.
**Docs:** `docs/product/prd.md` § 5. User stories (US-9) · `docs/ml/modeling-plan.md` § 4. Training practices

**Acceptance criteria:**
- [ ] Top contributing signals computed for each candidate and shown in the app.
- [ ] PR includes screenshots.

**Verify:** Signals match the feature values for a hand-checked synthetic candidate.

### Cache processed VODs

**Labels:** `backend` `needs-decision`
**Depends on:**
- Run real analysis jobs in the API worker

**Can run in parallel with:** Show detailed job progress and clear errors

**Goal:** Re-analyzing a VOD that's already been processed returns in seconds.

**Scope:** Cache keyed by VOD and stage versions, within retention rules.
**Out of scope:** Hosted storage.
**Docs:** `docs/architecture/overview.md` § 6. Cross-cutting concerns

**Acceptance criteria:**
- [ ] Second analysis of the same VOD reuses stored stages; a version change invalidates the cache.

**Verify:** Time two consecutive analyses of one VOD.

### Show detailed job progress and clear errors

**Labels:** `frontend` `backend` `needs-decision`
**Depends on:**
- Run real analysis jobs in the API worker
- Connect the web app to the API

**Can run in parallel with:** Cache processed VODs

**Goal:** A real progress bar by stage and specific, friendly messages for a bad link, expired VOD or missing chat.

**Scope:** Job progress reporting and UI states.
**Out of scope:** Notifications.
**Docs:** `docs/product/prd.md` § Scope at a glance

**Acceptance criteria:**
- [ ] Per-stage progress shown; each known failure has a specific message and next step.
- [ ] PR includes screenshots of each error state.

**Verify:** Trigger each error with test inputs.

### Run the evaluation harness in CI

**Labels:** `tooling` `modeling` `needs-decision`
**Depends on:**
- Implement the B0–B4 baselines
- Generate a synthetic VOD dataset for development

**Can run in parallel with:** Cache processed VODs

**Goal:** CI runs the evaluation harness on the synthetic dataset, so metric code can't silently break.

**Scope:** CI job; small synthetic evaluation config.
**Out of scope:** Real data in CI.
**Docs:** `docs/ml/evaluation.md` § 7. Implementation notes

**Acceptance criteria:**
- [ ] CI generates the synthetic dataset, runs B0 and B4 through the harness, and checks the report is produced with expected ordering.

**Verify:** CI green; a deliberately broken metric fails the job.

---

## Future work (not drafted)

Out of the MVP and optional polish. Turn one into a draft only after the MVP works and the team agrees (see [PRD scope](../product/prd.md#scope-at-a-glance)):

- Video signal experiment (scene cuts, motion) as an ablation
- Hosted deployment and app database (OQ-18)
- Learning the chat delay automatically
- Thumbs up/down feedback that retrains the model
- Interactive heatmap dashboard
- AI-suggested titles and hooks
- Docker setup
- Online posting experiment (OQ-10; needs streamer permission)
- Early-ranking experiment with real cutoff snapshots (OQ-1, conditional)
