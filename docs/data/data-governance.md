# Data Governance, Privacy and Platform Terms

| | |
|---|---|
| **Status** | Binding for all contributors. Items marked **Proposal** await team sign-off. |
| **Last updated** | 2026-10-09 |

AutoClipper processes content made by streamers and messages written by their chat communities. We want to be good citizens of those communities and of the platforms. These rules apply to all code, data and outputs of the project.

> This document is not legal advice. When in doubt, choose the more conservative option and raise it as an issue labeled `status:needs-decision`.

## 1. Rules for the repository

1. **No media in git.** No video, audio, thumbnails or exported clips.
2. **No raw chat in git**, including "just a small sample". Test fixtures must be **synthetic**: invented messages and fake usernames.
3. **No secrets in git.** Credentials live in `.env` (see `.env.example` and [SECURITY.md](../../SECURITY.md)).
4. **No real usernames in issues, PRs, docs or logs.** Refer to chatters by hash, if at all. Public streamer channel names are fine.
5. **No data attachments on GitHub.** Don't upload data files or recordings to issues or PRs.

## 2. Privacy: chat data

Chat messages are public at the time they're posted, but aggregating and storing them creates a dataset about people. We minimize what we keep:

- **Pseudonymize at ingestion.** Usernames and user IDs are replaced with `HMAC-SHA256(salt, id)` before anything is written to `interim/` or `processed/`. The salt lives only in `.env` (`AUTOCLIPPER_CHAT_HASH_SALT`).
- **Keep raw only as long as needed.** Raw replay files (with names) stay in `data/raw/chat/` on the collecting machine only, and are **not shared**. **Proposal:** delete raw chat after parsing and validation, and keep only the pseudonymized Parquet.
- **Minimize fields.** Keep only what features need (time, hashed author, text, emotes, a few badge flags).
- **Mentions in text** (`@username`) can re-identify people. **Proposal:** replace `@\w+` with `@user` during parsing.
- **No per-user analysis.** Features are aggregates per window (rates, counts, unique-chatter counts). We don't model or profile individual chatters.
- **Published outputs** (reports, slides, demo) show aggregates and never chat logs with identities.

## 3. Platform terms

### Twitch API (official)

- Use the Helix API under the [Twitch Developer Agreement](https://legal.twitch.com/legal/developer-agreement/) and Twitch's [Terms of Service](https://legal.twitch.com/legal/terms-of-service/). **Read both before building the collector.** Note anything relevant to data storage or retention in this doc via PR.
- Respect rate limits. Never attempt to bypass them.
- Credentials are per-app. Don't share them outside the team.

### Unofficial tools (TwitchDownloader, yt-dlp)

Downloading VOD media and chat replays goes through tools that aren't official Twitch APIs. Our position, **Proposal**:

- Use them for **research and personal analysis** of publicly available broadcasts, at low volume, without circumventing access controls. That means no subscriber-only VODs, no DRM, and no paywalled content.
- **Never redistribute** downloaded media or raw chat.
- Keep collection to the channels and volumes in the [collection plan](collection-plan.md).
- If a platform or streamer objects, stop and delete the relevant data.

### Publishing clips

- **Only publish clips of streamers who allow clipping and reposting.** Many do publicly, and some run clipping programs. Record the basis for permission (a link to the policy or written OK) in the experiment's README.
- Credit the streamer in the post.
- Respect takedown requests immediately.
- The online posting experiment (stretch) needs explicit team sign-off and a written plan (see [evaluation § online experiment](../ml/evaluation.md#5-online-experiment-stretch)).
- **No automated posting** from this codebase without a separate, reviewed decision (out of scope for the course project).

## 4. Data retention (proposal)

| Data | Where | Retention |
|---|---|---|
| Raw chat replay (with names) | Collecting machine, `data/raw/` | Delete after parse + validation |
| Pseudonymized chat | `data/interim/`, shared storage | Duration of project |
| Audio | Collecting machine, `data/raw/` | Until features are extracted and validated. Optionally keep for re-extraction during active development. |
| Processed tables / features | Shared storage | Duration of project |
| Exported clips | `artifacts/exports/` | Delete after demo unless published with permission |
| Model artifacts | `artifacts/models/` | Duration of project |

At the end of the project, the team decides what (if anything) to keep, and deletes the rest.

## 5. Responsible-use checklist for PRs that touch data

- [ ] Pseudonymization happens before anything leaves `data/raw/`
- [ ] No new fields with personal data without a reason in the PR
- [ ] No raw data, media or usernames in tests, docs or logs
- [ ] Collection volume and rate limits respected
- [ ] Any new source is documented in [data sources](data-sources.md) with its terms
