# Data Handling

| | |
|---|---|
| **Status** | Current. Background: [data sources and delivery defaults](../decisions/0011-data-sources-and-local-delivery.md). |
| **Last updated** | 2026-10-31 |

AutoClipper uses TwitchDownloader and yt-dlp for VOD media and chat. These storage and processing conventions make inputs reusable, keep the repo small and keep credentials and chatter names out of committed files.

## 1. Where data lives

- **Default layout:** git-ignored `data/` and `artifacts/` folders ([storage layout](../architecture/overview.md#5-storage-layout)). `AUTOCLIPPER_DATA_DIR` and `AUTOCLIPPER_ARTIFACTS_DIR` can point to local or shared directories; hosted storage can be integrated with deployment work.
- Collect a VOD once and reuse its media, pseudonymized chat and processed tables through the team's chosen storage. Collection can run on a team machine or worker; each teammate does not need to run the downloaders.
- **Nothing heavy in git, issues or PRs:** no video, audio, chat dumps, processed data or model weights. Test fixtures are small and synthetic. Screenshots of the app are fine.
- Secrets (`.env`, Twitch credentials, API keys) are never committed. Share them with teammates privately.

## 2. Chat

- The chat parser replaces usernames and user IDs with salted hashes and redacts `@mentions`. Reuse its output for features; keep raw chat and chatter names out of committed files and logs.
- The raw chat download is deleted after parsing. The pseudonymized messages are kept in `data/interim/chat/` so features can be recomputed after the VOD expires.
- Features are per-window totals (message rate, emotes, unique chatters). Nothing needs individual chatters.

## 3. Downloading

- Use TwitchDownloader (chat, audio, video) and yt-dlp (audio, video). The pilot channels are in [`configs/channels.yaml`](../../configs/channels.yaml).
- Subscriber-only VODs need a subscriber login; the initial pilot uses VODs the collector can download with its configured tools.
- The Twitch API client backs off on rate limits; collection uses its retry behavior.
- Download the pilot early: VODs expire after 7–60 days.

## 4. Disk space

- Keep audio, not full video, for analysis. Fetch video only for the segments you preview or export, unless you already have the VOD file.
- Delete intermediate WAVs once features are computed; they can be regenerated.

## 5. PR checklist for data-touching changes

- [ ] Usernames are hashed before anything is written outside the raw download
- [ ] No media, data or usernames in the diff, tests, docs or logs
- [ ] New data sources are documented in [data sources](data-sources.md)
