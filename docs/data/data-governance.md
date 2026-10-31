# Data Handling

| | |
|---|---|
| **Status** | Current. Background: [data sources and local-first delivery](../decisions/0011-data-sources-and-local-delivery.md). |
| **Last updated** | 2026-10-31 |

AutoClipper works on Twitch VODs downloaded with TwitchDownloader and yt-dlp on the team's own machines. These are the house rules for that data: they keep the repo small, keep secrets safe and keep chatter names out of everything we write.

## 1. Where data lives

- **On team machines,** under the git-ignored `data/` and `artifacts/` folders ([storage layout](../architecture/overview.md#5-storage-layout)).
- Teammates can copy VODs, audio, chat files and processed tables to each other (shared drive or direct copy).
- **Nothing heavy in git, issues or PRs:** no video, audio, chat dumps, processed data or model weights. Test fixtures are small and synthetic. Screenshots of the app are fine.
- Secrets (`.env`, Twitch credentials, API keys) are never committed. Share them with teammates privately.

## 2. Chat

- The chat parser replaces usernames and user IDs with salted hashes and redacts `@mentions`. Keep it that way: no chatter names in files, logs, screenshots or slides.
- The raw chat download is deleted after parsing. The pseudonymized messages are kept in `data/interim/chat/` so features can be recomputed after the VOD expires.
- Features are per-window totals (message rate, emotes, unique chatters). Nothing needs individual chatters.

## 3. Downloading

- Use TwitchDownloader (chat, audio, video) and yt-dlp (audio, video). The pilot channels are in [`configs/channels.yaml`](../../configs/channels.yaml).
- Subscriber-only VODs can't be downloaded without a subscriber login; skip them.
- Respect Twitch API rate limits (the API client already backs off).
- Download the pilot early: VODs expire after 7–60 days.

## 4. Disk space

- Keep audio, not full video, for analysis. Fetch video only for the segments you preview or export, unless you already have the VOD file.
- Delete intermediate WAVs once features are computed; they can be regenerated.

## 5. PR checklist for data-touching changes

- [ ] Usernames are hashed before anything is written outside the raw download
- [ ] No media, data or usernames in the diff, tests, docs or logs
- [ ] New data sources are documented in [data sources](data-sources.md)
