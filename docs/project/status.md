# Project Status

> **The living "what exists right now" document.** Every agent session reads it first. Update it **in the same PR** that changes the state. Don't mark anything done unless it's merged to `main` and tested.

**Last updated:** 2026-11-04
**Current phase:** Building (week of Nov 2 in the [roadmap](../product/roadmap.md))
**Next milestone:** M1 Data: pilot VODs downloaded and parsed
**Key dates:** Demo of Demos Fri Dec 4, 2026 · Final presentations and demo night Fri Dec 11, 2026

## Summary

Scope, architecture and the plan are documented, with concrete defaults for building the MVP. The planned app has two outputs: ranked, captioned 9:16 clips and a 16:9 highlights video. The initial setup targets a laptop; collection/storage can be shared and hosted deployment remains an option. The Python package has a `synth` command for deterministic fake VOD datasets, configuration, logging, a shared FFmpeg runner, a Helix metadata client, a pseudonymizing chat replay parser and vertical-MP4 export (cut, reframe, encode). Python and frontend tooling run in CI. The Next.js frontend has a placeholder page. **There is no real dataset, model, analysis pipeline or backend yet.** No results exist.

## What exists

| Area | State | Notes |
|---|---|---|
| Documentation | ✅ Initial set (PR #2) | Product scope, architecture, data, ML and development guidance; shared task workflow |
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
| Twitch developer app | ✅ Registered (#4) | One shared team app; credentials supplied through untracked `.env` files or worker/deployment environment variables |
| Data collection | ❌ Not started | Five pilot channels in [`configs/channels.yaml`](../../configs/channels.yaml); collect the VODs in `configs/pilot_vods.txt` (#35) once on a suitable machine or worker and reuse the outputs through shared storage ([data sources and delivery](../decisions/0011-data-sources-and-local-delivery.md)) |
| Labels / features / models / eval | ❌ Not started | Approach decided: [modeling plan § 0](../ml/modeling-plan.md#0-the-plan-in-plain-english) |
| Audio download | 🟡 Draft only (#19) | PR #59 is unmerged; its code is not on `main`. |
| Export | 🟡 Cut and reframe only (#20) | `autoclipper.export`: exact source-interval cut (padding separate, clamped to where video and audio both have content, no lag shift), `center_crop` and `configured_region` layouts (the region set with `AUTOCLIPPER_EXPORT_REGION`), 1080×1920 H.264/AAC MP4 written atomically. Tested on generated media only. No captions (#21) and no CLI/API command (#32) yet. Details: [overview § 4.6](../architecture/overview.md#46-export) |
| License | ✅ MIT | [`LICENSE`](../../LICENSE) |
| Web app | 🟡 Scaffold only (#6) | Next.js + TypeScript (strict) + Tailwind in `web/` with ESLint, Prettier and its own CI workflow (`web.yml`); one placeholder page. No screens (#29–#31), API client (#33) or FastAPI backend (#28) yet. |

## Decisions

Everything needed for the MVP has a documented default: data sources, pilot channels, the model target and delivery options ([data sources and delivery](../decisions/0011-data-sources-and-local-delivery.md)), [labeling](../data/labeling.md) and [evaluation](../ml/evaluation.md), the backend/frontend API including the highlights video ([API contract](../architecture/api-contract.md)) and the app experience ([PRD § 4](../product/prd.md#4-the-app-experience)). There is no open-questions list: technical choices the docs don't cover are made by whoever builds the issue and explained in the PR ([AGENTS.md § 10](../../AGENTS.md#10-when-youre-unsure)).

## Suggested next tasks

Follow the week of Nov 2 in the [roadmap](../product/roadmap.md): fix and merge the audio draft (PR #59), captions (#21), fetch viewer clips (#17), the ingest command (#34) and the pilot download (#35), time windows (#22), chat and audio features (#23, #24), the backend with a fake job (#28) and the app screens (#29–#31).

## Known issues / caveats

- The Python setup was tested locally on Windows only; Linux runs in CI (Ubuntu). Nobody has tried macOS yet.
- Chat replay and VOD download rely on unofficial tools ([data sources](../data/data-sources.md)).
- Clip coverage, chat lag and alignment still need to be measured in a pilot.
- The original design-doc schedule slipped; the [roadmap](../product/roadmap.md) was replanned on 2026-10-31.
