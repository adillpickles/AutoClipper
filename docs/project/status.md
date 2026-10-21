# Project Status

> **The living "what exists right now" document.** Every agent session reads it first. Update it **in the same PR** that changes the state. Don't mark anything done unless it's merged to `main` and tested.

**Last updated:** 2026-10-21
**Current phase:** M0 Foundations
**Next milestone:** M1 Data collection ([roadmap](../product/roadmap.md))
**Key dates:** Demo of Demos Fri Dec 4, 2026 · Final presentations and demo night Fri Dec 11, 2026

## Summary

Scope, architecture and team workflow are documented, and the core tech stack is decided. The Python package has a `synth` command for deterministic fake VOD datasets, configuration, logging, a shared FFmpeg runner, a Helix metadata client and a pseudonymizing chat replay parser. Python and frontend tooling run in CI. The Next.js frontend has a placeholder page. **There is no real dataset, model, analysis pipeline or backend yet.** No results exist.

## What exists

| Area | State | Notes |
|---|---|---|
| Documentation | ✅ Initial set (PR #2) | Product scope, architecture, data, ML, development and agent workflow docs, ADRs |
| Repo config | ✅ Initial set (PR #2) | `.gitignore`, `.gitattributes`, `.editorconfig`, `.env.example`, issue/PR templates |
| GitHub settings | ✅ Configured | `main` requires a PR and squash merge, with no second-person approval required. Direct pushes, force pushes and deletion stay blocked; admin bypass is PR-only. |
| Labels | ✅ Simplified set live | 20 category-first labels per [labels](labels.md) |
| Team access | ⚠️ Owner only | Teammates still need to be added as collaborators (OQ-11) |
| Issue backlog | ✅ Published | Waves 0–6 are issues #3–#50 (numbers in the [backlog](backlog.md) index); optional drafts not published yet |
| Python scaffold | ✅ In place (#5) | uv project (Python 3.12, `src/` layout, `uv.lock`), `autoclipper` CLI shell with `--help`/`--version`, Ruff, Pyright (`standard`), pytest (live tests deselected by default) and pre-commit hooks. Commands: [`AGENTS.md` § 5](../../AGENTS.md#5-commands) |
| Config and logging | ✅ In place (#14) | `autoclipper.config` (pydantic-settings: environment, then `.env` in the working directory; cached `get_settings()`; secrets as `SecretStr`), key=value UTC log lines on stderr set up by the CLI (`autoclipper.logging`), and `autoclipper.privacy.pseudonymize` (salted HMAC-SHA256). Usage: [conventions § 4 and § 6](../development/conventions.md#4-configuration) |
| Helix client | ✅ In place (#16) | `autoclipper.ingest.twitch_api.HelixClient`: app token (cached, re-fetched once on `401`), `Ratelimit-*`/`429` waits, capped `5xx` retries, Get Users and Get Videos (by ID; archives by user, paginated with a result cap) parsed into `broadcasters`/`vods` records. Unit-tested against synthetic responses only; never run against real Twitch (no app yet, OQ-8) |
| Chat replay parsing | ✅ In place (#18), tested on synthetic files only | `autoclipper.ingest.chat`: runs `TwitchDownloaderCLI chatdownload` (pinned 1.56.5) through an injectable runner and parses its JSON into in-memory, pseudonymized rows of the proposed `chat_messages` table, with @mentions and known names redacted. Never run against real Twitch (OQ-9/OQ-17). Parsed messages are not written to disk; the raw file is deleted after parsing. Field reference: [data sources § 2](../data/data-sources.md#2-chat-replay-twitchdownloader-unofficial) |
| CI | ✅ In place (#5/#6) | Python: locked uv install, Ruff, Pyright and pytest on Ubuntu and Windows. Frontend: npm install, lint, typecheck, format and build on Ubuntu for web changes. Not required checks yet (#3) |
| FFmpeg runner | ✅ In place (#51) | `autoclipper.ffmpeg`: FFmpeg/FFprobe commands as argument lists, an injectable runner with timeouts and typed errors, `probe` (duration, streams) and the version string. CI installs FFmpeg (apt on Ubuntu, a pinned, checksum-verified build on Windows); FFmpeg tests skip locally without it but fail in CI ([testing](../development/testing.md#ffmpeg-tests)) |
| Synthetic dataset | ✅ In place (#15) | `autoclipper synth --seed <n>` writes 3 fake VODs (Parquet tables in the proposed data-model schemas, chat, 16 kHz WAVs, test-pattern videos and a SHA-256 manifest) to `data/synthetic/seed-<n>/`; same seed, same bytes. Details: [testing § Synthetic dataset](../development/testing.md#synthetic-dataset) |
| Audio download | ✅ In place (#19) | `autoclipper.ingest.media`: audio-only VOD download with yt-dlp to `data/raw/audio/<video_id>.<ext>` (resumable, checked against the reported duration, no FFmpeg run by yt-dlp, typed errors for unavailable, subscriber-only, no audio-only format, network and tool failures), then FFmpeg normalization to 16 kHz mono WAV in `data/interim/audio/` that keeps the VOD timeline, rejects sources with more than 1 % undecodable audio and is checked against the source's duration. Tested with fakes and generated tones only: no real download has been run, and real downloads wait for source authorization (OQ-17) |
| Twitch developer app | ❌ Not registered | OQ-8 |
| Data collection | ❌ Not started | Needs channel list (OQ-4) and source/privacy decisions (OQ-9/OQ-17) before real data |
| Labels / features / models / eval | ❌ Not started | Training target is OQ-1 |
| Export | 🟡 Draft only (#20) | PR #60 is unmerged; its code is not on `main`. |
| Web app | 🟡 Scaffold only (#6) | Next.js + TypeScript (strict) + Tailwind in `web/` with ESLint, Prettier and its own CI workflow (`web.yml`); one placeholder page. No screens (#29–#31), API client (#33) or FastAPI backend (#28) yet. |

## Decisions pending

See [open questions](open-questions.md). Each one has a draft in Wave 0 (team access) or Wave 1 (`decision` tasks) of the [backlog](backlog.md):

1. **Teammate access** (OQ-11): blocks teammates picking up issues.
2. **Job and candidate API contract** (ADR-0009, #13): a draft is in the [API contract](../architecture/api-contract.md), awaiting team review and adoption. Until it's adopted, it blocks the FastAPI and Next.js screens.
3. **Dataset construction rules and v0 feature set** (ADR-0009): block windows, moments and features.
4. **Source permissions and chat retention** (OQ-9/OQ-17): block real collection. Synthetic development can proceed.
5. **Channels, collection scale and storage** (OQ-4/OQ-5): block the pilot and collector.
6. **Training target** (OQ-1) and **evaluation protocol** (ADR-0009): block labels, metrics and reported models.

## Suggested next tasks

Work through the [backlog](backlog.md) in dependency order. Next implementation work includes viewer clips (#17) and fixes to the unmerged audio/export drafts (#19/#20). Team access (#3), the Twitch app (#4), and the pending source, dataset, feature and evaluation decisions (#7–#12) still need attention. The API proposal from #13 is available for adoption before backend/screens build against it; merging the proposal did not adopt its decision points.

## Known issues / caveats

- The Python setup was tested locally on Windows only; Linux runs in CI (Ubuntu). Nobody has tried macOS yet.
- Chat replay and VOD download rely on unofficial tools ([data sources](../data/data-sources.md)).
- Clip coverage, chat lag and alignment still need to be measured in a pilot.
- The team's original week-by-week schedule is behind (no code as of Oct 16); replan at the weekly sync.

## Changelog of this file

- 2026-10-21: made second-person PR approval optional; retained PR-only squash merges and protection against direct pushes to `main`.

- 2026-10-09: initial documentation drafted and GitHub settings configured.
- 2026-10-13: contract and workflow repairs after the first audit.
- 2026-10-16: core ADRs accepted, scope table added to the PRD, contributor workflow simplified, optional review loop documented.
- 2026-10-16: implementation backlog drafted; label policy simplified.
- 2026-10-17: simplified labels applied on GitHub; backlog Waves 0–6 published as issues #3–#50.
- 2026-10-19: Python package scaffold, tooling and CI added (#5).

- 2026-10-19: Next.js frontend scaffolded in `web/` with lint, typecheck, format and build CI (#6).
- 2026-10-19: job, candidate, media and export API contract drafted for #13 (proposed, not adopted).
- 2026-10-20: config module, logging setup and pseudonymization helper added (#14).
- 2026-10-20: shared FFmpeg runner and media probe added, FFmpeg installed in CI (#51).
- 2026-10-21: deterministic synthetic dataset generator and `autoclipper synth` added (#15).
- 2026-10-21: Twitch Helix client for app auth, Get Users and Get Videos added (#16).
- 2026-10-21: chat replay wrapper and pseudonymizing parser added (#18).
- 2026-10-24: VOD audio download (yt-dlp) and 16 kHz mono normalization added (#19).
