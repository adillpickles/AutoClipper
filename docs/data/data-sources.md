# Data Sources

| | |
|---|---|
| **Status** | Reference. Endpoint details reflect Twitch's public docs as understood when this was written. **Verify against the official reference before implementing.** Items marked ⚠️ need a spike. |
| **Last updated** | 2026-10-09 |

> **Agents: don't invent API fields or parameters.** If a field you need isn't documented here or in the official reference, check the live docs or open a `type:spike` issue.

## Overview

| Source | What we get | Official? | Used for | Stage |
|---|---|---|---|---|
| Twitch Helix **Get Users** | Broadcaster ID, type | ✅ | Resolving channels | v1 |
| Twitch Helix **Get Videos** | VOD metadata, duration, muted segments | ✅ | VOD discovery | v1 |
| Twitch Helix **Get Clips** | Every viewer clip + `vod_offset` + views | ✅ | **Labels** | v1 |
| **TwitchDownloader** (CLI) | Chat replay with offsets | ❌ unofficial | Chat features | v1 |
| **yt-dlp** | VOD audio/video | ❌ unofficial | Audio features, export | v1 |
| Twitch **chat** (IRC / EventSub) | Live chat messages | ✅ | Backup chat source; live mode | M1 backup, stretch |
| 7TV / BTTV / FFZ emote APIs | Third-party emote sets | ✅ (their own APIs) | Emote features | optional |
| External short-form engagement data | Public clip-account videos + views | depends on tool | Short-form performance model | stretch |

---

## 1. Twitch Helix API (official)

- **Reference:** <https://dev.twitch.tv/docs/api/reference>
- **Base URL:** `https://api.twitch.tv/helix`
- **Auth:** register an app at <https://dev.twitch.tv/console>, then use the **client-credentials** grant to get an *app access token*:

  ```http
  POST https://id.twitch.tv/oauth2/token
    ?client_id=<TWITCH_CLIENT_ID>
    &client_secret=<TWITCH_CLIENT_SECRET>
    &grant_type=client_credentials
  ```

  Send `Client-Id: <id>` and `Authorization: Bearer <token>` on every request. Cache the token until shortly before `expires_in` runs out, and refresh on `401`.

- **Rate limits:** Helix uses a token-bucket limit per client. **Read the `Ratelimit-Limit`, `Ratelimit-Remaining` and `Ratelimit-Reset` response headers** instead of hard-coding a number. On `429`, sleep until `Ratelimit-Reset`.
- **Pagination:** responses include `pagination.cursor`. Pass it as `after` to get the next page. `first` sets the page size (max 100).

### Get Videos: <https://dev.twitch.tv/docs/api/reference/#get-videos>

- Query by `user_id` with `type=archive` for past broadcasts, or by `id` for specific VODs.
- Fields we use: `id`, `user_id`, `stream_id`, `title`, `created_at`, `duration` (string like `"3h8m33s"`, which we parse to seconds), `type`, `language`, `view_count`, `muted_segments` (`offset`, `duration`).

### Get Clips: <https://dev.twitch.tv/docs/api/reference/#get-clips>

- Query by `broadcaster_id` (plus `started_at`/`ended_at`), by `game_id`, or by clip `id`.
- **There's no "clips for this video" filter.** To get a VOD's clips, query the broadcaster over the VOD's time range `[created_at, created_at + duration + buffer]` and keep rows whose `video_id` matches.
- Fields we use: `id`, `broadcaster_id`, `creator_id` (hashed at ingest), `video_id`, `game_id`, `language`, `title`, `view_count`, `created_at`, `duration`, `vod_offset`, `is_featured`.
- **`vod_offset` can be null**, e.g. when the VOD is deleted or not yet processed. Store the row but exclude it from labeling.
- ⚠️ **Pagination completeness:** for channels with many clips, paginating over a long date range may not return every clip. **Chunk queries into short time ranges** (e.g. per hour of the VOD) and deduplicate by `clip_id`. Measure how complete this is in an early spike.
- ⚠️ **View counts grow over time** and may be updated with a delay. Always store `fetched_at`. For labeling, prefer a consistent observation lag (e.g. re-fetch clips N days after the VOD; see [collection plan](collection-plan.md)).

### Get Users: <https://dev.twitch.tv/docs/api/reference/#get-users>

- Resolve `login` → `id` and `broadcaster_type` (`partner`/`affiliate`/`""`).

### VOD retention ⚠️

Twitch deletes past broadcasts after a retention period that depends on the account type. It has historically been about 7 days by default and longer, up to about 60 days, for Partners and some other account types. **Check the current policy.** Either way, **VODs we don't collect promptly are gone**, and clips may lose their `video_id` link afterwards. This is why [collection](collection-plan.md) runs continuously.

## 2. Chat replay: TwitchDownloader (unofficial)

- **Repo:** <https://github.com/lay295/TwitchDownloader>. We use the **CLI** (`TwitchDownloaderCLI`), not the GUI.
- Typical usage (check `--help` for the installed version):

  ```bash
  TwitchDownloaderCLI chatdownload --id <video_id> -o data/raw/chat/<video_id>.json
  ```

- Output: JSON with per-comment `content_offset_seconds`, commenter info, message fragments and emote IDs.
- **Risks:** relies on Twitch's undocumented internal API and can break without warning. Pin the version in the catalog (`tool` column) and fail loudly on schema surprises.
- **Privacy:** raw output contains usernames. Parse it into `chat_messages` with hashed IDs immediately. The raw file stays only in git-ignored `data/raw/`.

## 3. VOD media: yt-dlp (unofficial)

- **Repo:** <https://github.com/yt-dlp/yt-dlp>
- **Audio only** for feature extraction. Twitch VODs usually expose an audio-only format:

  ```bash
  yt-dlp -f "bestaudio" -o "data/raw/audio/%(id)s.%(ext)s" https://www.twitch.tv/videos/<video_id>
  ```

- **Segments** for export, so we never download the full video:

  ```bash
  yt-dlp --download-sections "*<start>-<end>" -f "best[height<=1080]" -o "…" https://www.twitch.tv/videos/<video_id>
  ```

- Audio is then normalized with FFmpeg to 16 kHz mono WAV for analysis.
- **Risks:** unofficial and may conflict with platform terms (see [governance](data-governance.md)). Keep yt-dlp up to date, because Twitch changes break old versions.

## 4. Live chat: official Twitch chat (backup and live mode)

The replay tool can break, so a backup logger records chat **live** for target channels:

- **Twitch IRC** over WebSocket (`wss://irc-ws.chat.twitch.tv:443`): <https://dev.twitch.tv/docs/chat/>
- **EventSub** `channel.chat.message`: <https://dev.twitch.tv/docs/eventsub/eventsub-subscription-types/>. Requires a user token with the right scopes.

Live offsets must be aligned to VOD time: `offset_s = message_time - vod.created_at`. Validate the alignment on a VOD where we also have replay chat. ⚠️ The stream-start to VOD-start offset may not be exactly zero.

## 5. Third-party emotes (optional)

Chat in many communities is dominated by 7TV/BTTV/FFZ emotes, which appear as plain text in messages. Their public APIs list a channel's emote set, so we can count them as emotes rather than words. Optional for v1.

## 6. External short-form engagement data (stretch)

To predict performance **on short-form platforms** rather than on Twitch, we'd need short-form clip accounts' videos and view counts. One option is a third-party account-export tool licensed by a team member. It has a monthly limit on how many accounts can be scanned, so choose high-volume accounts. Use is subject to that tool's terms and the platforms' terms. See OQ-3 in [open questions](../project/open-questions.md). **Not part of v1/v2.**

## 7. Related public datasets (for context, not required)

- *Video Highlight Prediction Using Audience Chat Reactions* (Fu et al., EMNLP 2017) studied highlight prediction from live-stream chat and video for esports broadcasts. It's useful as related work and as a sanity check that chat signals work. Check its dataset license before using any data.
