# Project Status

> **The living "what exists right now" document.** Every agent session reads it first. Update it **in the same PR** that changes the state. Don't mark anything done unless it's merged to `main` and tested.

**Last updated:** 2026-10-31
**Current phase:** Building (week of Nov 2 in the [roadmap](../product/roadmap.md))
**Next milestone:** M1 Data: pilot VODs downloaded and parsed
**Key dates:** Demo of Demos Fri Dec 4, 2026 · Final presentations and demo night Fri Dec 11, 2026

## Summary

Scope, architecture and the plan are documented, and everything needed for the MVP is decided. The app has two outputs: ranked, captioned 9:16 clips and a 16:9 highlights video, and runs locally. The Python package has a `synth` command for deterministic fake VOD datasets, configuration, logging, a shared FFmpeg runner, a Helix metadata client and a pseudonymizing chat replay parser. Python and frontend tooling run in CI. The Next.js frontend has a placeholder page. **There is no real dataset, model, analysis pipeline or backend yet.** No results exist.

## What exists

| Area | State | Notes |
|---|---|---|
| Documentation | ✅ Initial set (PR #2) | Product scope, architecture, data, ML, development and agent workflow docs, ADRs |
| Repo config | ✅ Initial set (PR #2) | `.gitignore`, `.gitattributes`, `.editorconfig`, `.env.example`, issue/PR templates |
| GitHub settings | ✅ Configured | `main` requires a PR and squash merge, with no second-person approval required. Direct pushes, force pushes and deletion stay blocked; admin bypass is PR-only. |
| Labels | ✅ Live | Per [labels](labels.md) |
| Team access | ✅ All four members | Collaborators with write access |
| Issues | ✅ Rewritten 2026-10-31 | Plain-English GitHub issues with a standard format ([writing issues](../agents/writing-tasks.md)); order in the [roadmap](../product/roadmap.md). Highlights video: #71, #72 |
| Python scaffold | ✅ In place (#5) | uv project (Python 3.12, `src/` layout, `uv.lock`), `autoclipper` CLI shell with `--help`/`--version`, Ruff, Pyright (`standard`), pytest (live tests deselected by default) and pre-commit hooks. Commands: [`AGENTS.md` § 5](../../AGENTS.md#5-commands) |
| Config and logging | ✅ In place (#14) | `autoclipper.config` (pydantic-settings: environment, then `.env` in the working directory; cached `get_settings()`; secrets as `SecretStr`), key=value UTC log lines on stderr set up by the CLI (`autoclipper.logging`), and `autoclipper.privacy.pseudonymize` (salted HMAC-SHA256). Usage: [conventions § 4 and § 6](../development/conventions.md#4-configuration) |
| Helix client | ✅ In place (#16) | `autoclipper.ingest.twitch_api.HelixClient`: app token (cached, re-fetched once on `401`), `Ratelimit-*`/`429` waits, capped `5xx` retries, Get Users and Get Videos (by ID; archives by user, paginated with a result cap) parsed into `broadcasters`/`vods` records. Unit-tested against synthetic responses; the team Twitch app is registered and its token request verified |
| Chat replay parsing | ✅ In place (#18), tested on synthetic files only | `autoclipper.ingest.chat`: runs `TwitchDownloaderCLI chatdownload` (pinned 1.56.5) through an injectable runner and parses its JSON into in-memory, pseudonymized rows of the `chat_messages` table, with @mentions and known names redacted. Not yet run on a real VOD. The raw file is deleted after parsing; saving the pseudonymized table to `data/interim/chat/` is part of the ingest command (#34). Field reference: [data sources § 2](../data/data-sources.md#2-chat-replay-twitchdownloader-unofficial) |
| CI | ✅ In place (#5/#6) | Python: locked uv install, Ruff, Pyright and pytest on Ubuntu and Windows. Frontend: npm install, lint, typecheck, format and build on Ubuntu for web changes. Not required checks yet (#3) |
| FFmpeg runner | ✅ In place (#51) | `autoclipper.ffmpeg`: FFmpeg/FFprobe commands as argument lists, an injectable runner with timeouts and typed errors, `probe` (duration, streams) and the version string. CI installs FFmpeg (apt on Ubuntu, a pinned, checksum-verified build on Windows); FFmpeg tests skip locally without it but fail in CI ([testing](../development/testing.md#ffmpeg-tests)) |
| Synthetic dataset | ✅ In place (#15) | `autoclipper synth --seed <n>` writes 3 fake VODs (Parquet tables in the data-model schemas, chat, 16 kHz WAVs, test-pattern videos and a SHA-256 manifest) to `data/synthetic/seed-<n>/`; same seed, same bytes. Details: [testing § Synthetic dataset](../development/testing.md#synthetic-dataset) |
| Twitch developer app | ✅ Registered (#4) | One shared team app; credentials shared privately and kept in each member's `.env` |
| Data collection | ❌ Not started | Five pilot channels in [`configs/channels.yaml`](../../configs/channels.yaml); VOD links collected in `configs/pilot_vods.txt` (#35) and downloaded locally by each team member ([data sources and delivery](../decisions/0011-data-sources-and-local-delivery.md)) |
| Labels / features / models / eval | ❌ Not started | Approach decided: [modeling plan § 0](../ml/modeling-plan.md#0-the-plan-in-plain-english) |
| Audio download / export | 🟡 Drafts only (#19/#20) | PR #59 (audio) and PR #60 (vertical export) are unmerged. Each has open review findings (listed in #19 and #20) and fails only the Ubuntu CI job, on FFmpeg-version-dependent tests. Their code is not on `main`. |
| License | ✅ MIT | [`LICENSE`](../../LICENSE), [ADR-0006](../decisions/0006-license.md) |
| Web app | 🟡 Scaffold only (#6) | Next.js + TypeScript (strict) + Tailwind in `web/` with ESLint, Prettier and its own CI workflow (`web.yml`); one placeholder page. No screens (#29–#31), API client (#33) or FastAPI backend (#28) yet. |

## Decisions

Everything needed for the MVP is decided: data sources, pilot channels, the model target and local-first delivery ([data sources and delivery](../decisions/0011-data-sources-and-local-delivery.md)), the [analysis rules](../decisions/0009-analysis-and-evaluation-contract-repair.md), the backend/frontend API including the highlights video ([API contract](../architecture/api-contract.md)) and the app experience ([PRD § 4](../product/prd.md#4-the-app-experience)). There is no open-questions list: technical choices the docs don't cover are made by whoever builds the issue and explained in the PR ([AGENTS.md § 10](../../AGENTS.md#10-when-youre-unsure)).

## Suggested next tasks

Follow the week of Nov 2 in the [roadmap](../product/roadmap.md): fix and merge the audio and export drafts (PR #59, #60), fetch viewer clips (#17), the ingest command (#34) and the pilot download (#35), time windows (#22), chat and audio features (#23, #24), the backend with a fake job (#28) and the app screens (#29–#31).

## Known issues / caveats

- The Python setup was tested locally on Windows only; Linux runs in CI (Ubuntu). Nobody has tried macOS yet.
- Chat replay and VOD download rely on unofficial tools ([data sources](../data/data-sources.md)).
- Clip coverage, chat lag and alignment still need to be measured in a pilot.
- The original design-doc schedule slipped; the [roadmap](../product/roadmap.md) was replanned on 2026-10-31.

## Changelog of this file

- 2026-10-21: made second-person PR approval optional; retained PR-only squash merges and protection against direct pushes to `main`.

- 2026-10-09: initial documentation drafted and GitHub settings configured.
- 2026-10-13: contract and workflow repairs after the first audit.
- 2026-10-16: core ADRs accepted, scope table added to the PRD, contributor workflow simplified, optional review loop documented.
- 2026-10-16: implementation backlog drafted; label policy simplified.
- 2026-10-17: simplified labels applied on GitHub; backlog Waves 0–6 published as issues #3–#50.
- 2026-10-19: Python package scaffold, tooling and CI added (#5).

- 2026-10-19: Next.js frontend scaffolded in `web/` with lint, typecheck, format and build CI (#6).
- 2026-10-19: job, candidate, media and export API contract drafted for #13.
- 2026-10-20: config module, logging setup and pseudonymization helper added (#14).
- 2026-10-20: shared FFmpeg runner and media probe added, FFmpeg installed in CI (#51).
- 2026-10-21: deterministic synthetic dataset generator and `autoclipper synth` added (#15).
- 2026-10-21: Twitch Helix client for app auth, Get Users and Get Videos added (#16).
- 2026-10-21: chat replay wrapper and pseudonymizing parser added (#18).
- 2026-10-30: API contract adopted (#13).
- 2026-10-30: data sources, chat handling, training target, experiment tracking and hosting decided; analysis rules (ADR-0009) accepted; the `needs-decision` label removed.
- 2026-10-31: product experience updated (ranked, ready-made clips), roadmap replanned by week, agent rules simplified, data handling rewritten in plain English, issues rewritten in a standard format; Twitch app registered (#4).
- 2026-10-31: second output added (16:9 highlights video for viewers; #71, #72); local-first delivery with a website + download as the distribution path; permission gates and the open-questions list removed; API contract cleaned of decision points and extended with highlights and local video files; MIT license; `decision` label removed.
