# Data Handling

| | |
|---|---|
| **Status** | Current. Background: [data sources and delivery defaults](../decisions/0011-data-sources-and-local-delivery.md). |
| **Last updated** | 2026-10-31 |

AutoClipper uses TwitchDownloader and yt-dlp for VOD media and chat. These storage and processing conventions make inputs reusable and keep the repo small.

## 1. Where data lives

- **Default layout:** git-ignored `data/` and `artifacts/` folders ([storage layout](../architecture/overview.md#5-storage-layout)). `AUTOCLIPPER_DATA_DIR` and `AUTOCLIPPER_ARTIFACTS_DIR` can point to local or shared directories; hosted storage can be integrated with deployment work.
- Collect a VOD once and reuse its media, pseudonymized chat and processed tables through the team's chosen storage. Collection can run on a team machine or worker; each teammate does not need to run the downloaders.
- **Nothing heavy in git, issues or PRs:** no video, audio, chat dumps, processed data or model weights. Test fixtures are small and synthetic. Screenshots of the app are fine.
- Secrets (`.env`, Twitch credentials, API keys) are never committed. Share them with teammates privately.

## 2. Chat

- The chat parser replaces user IDs with salted hashes (enough for unique-chatter counts) and redacts `@mentions`. Reuse its output for features.
- The raw chat download is deleted after parsing. The pseudonymized messages are kept in `data/interim/chat/` so features can be recomputed after the VOD expires.
- Features are per-window totals (message rate, emotes, unique chatters).
- **What leaves the machine for titles, and when:** nothing by default. Only when `AUTOCLIPPER_TITLE_LLM_BASE_URL` and `AUTOCLIPPER_TITLE_LLM_MODEL` are both set does the optional title polisher (`autoclipper.models.title_polish`, #65) send one request per analysis to that OpenAI-compatible endpoint, which can be a local server. It holds, for each of the top 20 moments, a positional ID, the deterministic title and reason, its duration and viewer-clip count, and the peak per-window aggregate numbers from `autoclipper.models.explain.ALLOWED_FEATURE_COLUMNS`: `chat_msg_rate`, `chat_msg_rate_z`, `chat_msg_rate_rz`, `chat_unique_rate_z`, `chat_laugh_rate`, `chat_hype_rate`, `chat_shock_rate`, `chat_keyword_rate`, `chat_emote_frac`, `chat_caps_frac`, `chat_repeat_frac`, `chat_event_count`, `chat_msg_rate_lead1`, `chat_msg_rate_lead2`, `audio_rms_max`, `audio_rms_max_z`, `audio_rms_rz`, `audio_onset_rate` and the optional `audio_evt_laughter`, `audio_evt_scream`, `audio_evt_cheer`, `audio_evt_music` (`audio_is_muted` is read only to skip muted windows and isn't sent). It never holds chat text, usernames, hashed IDs, the VOD title or channel, file paths or audio. The key (`AUTOCLIPPER_TITLE_LLM_API_KEY`) and URL are never logged or returned by the API, and a warning is logged when the key would go over plain `http://` to a host other than this machine.

## 3. Downloading

- Use TwitchDownloader (chat, audio, video) and yt-dlp (audio, video). The pilot channels are in [`configs/channels.yaml`](../../configs/channels.yaml).
- Subscriber-only VODs need a subscriber login; the initial pilot uses VODs the collector can download with its configured tools.
- The Twitch API client backs off on rate limits; collection uses its retry behavior.
- Download the pilot early: VODs expire after 7–60 days.

## 4. Disk space

- Keep audio, not full video, for analysis. Fetch video only for the segments you preview or export, unless you already have the VOD file.
- Delete intermediate WAVs once features are computed; they can be regenerated.

## 5. PR checklist for data-touching changes

- [ ] No media or data in the diff
- [ ] New data sources are documented in [data sources](data-sources.md)
