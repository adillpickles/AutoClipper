# Data Sources

| | |
|---|---|
| **Status** | Reference. Endpoint details reflect Twitch's public docs as understood when this was written. **Verify against the official reference before implementing.** Items marked ⚠️ need a spike. |
| **Last updated** | 2026-10-13 |

> **Agents: don't invent API fields or parameters.** If a field you need isn't documented here or in the official reference, check the live docs or open a `spike` issue.

## Overview

| Source | What we get | Official? | Used for | Stage |
|---|---|---|---|---|
| Twitch Helix **Get Users** | Broadcaster ID, type | ✅ | Resolving channels | v1 |
| Twitch Helix **Get Videos** | VOD metadata, duration, muted segments | ✅ | VOD discovery | v1 |
| Twitch Helix **Get Clips** | Retrievable viewer clips + `vod_offset` + views (query limits apply) | ✅ | Candidates, signals and possible weak labels | Initial analysis |
| **TwitchDownloader** (CLI) | Chat replay with offsets | ❌ unofficial | Chat features | v1 |
| **yt-dlp** | VOD audio/video | ❌ unofficial | Audio features, export | v1 |
| Twitch **chat** (IRC / EventSub) | Live chat messages | ✅ | Backup chat source; live mode | M1 backup, stretch |
| 7TV / BTTV / FFZ emote APIs | Third-party emote sets | ✅ (their own APIs) | Emote features | optional |
| External short-form engagement data | Public clip-account videos + views | depends on tool | Short-form performance model | out of scope this semester |

---

## 1. Twitch Helix API (official)

- **Reference:** <https://dev.twitch.tv/docs/api/reference>. The details below were checked against it and the [API guide](https://dev.twitch.tv/docs/api/guide) on 2026-10-21; `autoclipper.ingest.twitch_api` implements them.
- **Base URL:** `https://api.twitch.tv/helix`
- **Auth:** register an app at <https://dev.twitch.tv/console>, then use the [**client-credentials** grant](https://dev.twitch.tv/docs/authentication/getting-tokens-oauth/#client-credentials-grant-flow) to get an *app access token*. The parameters go in an `x-www-form-urlencoded` body, not the URL:

  ```http
  POST https://id.twitch.tv/oauth2/token
  Content-Type: application/x-www-form-urlencoded

  client_id=<TWITCH_CLIENT_ID>&client_secret=<TWITCH_CLIENT_SECRET>&grant_type=client_credentials
  ```

  The response has `access_token`, `expires_in` (seconds) and `token_type`. Send `Client-Id: <id>` and `Authorization: Bearer <token>` on every request. Cache the token until shortly before `expires_in` runs out, and on `401` get a new one (app tokens [can't be refreshed](https://dev.twitch.tv/docs/authentication/refresh-tokens/), only requested again).

- **Rate limits:** Helix uses a token-bucket limit per client. **Read the `Ratelimit-Limit`, `Ratelimit-Remaining` and `Ratelimit-Reset` response headers** instead of hard-coding a number. `Ratelimit-Reset` is a Unix epoch timestamp in seconds. On `429`, sleep until `Ratelimit-Reset`. On `503`, the guide says to retry once.
- **Pagination:** responses include `pagination.cursor`. Pass it as `after` to get the next page. `first` sets the page size (max 100). An empty `pagination` object means there are no more pages; near the end a cursor can also lead to an empty page.

### Get Videos: <https://dev.twitch.tv/docs/api/reference/#get-videos>

- Query by `user_id` with `type=archive` for past broadcasts, or by `id` (up to 100 per request) for specific VODs. By `id`, unknown IDs are ignored, and the response is `404` only if none were found.
- Fields we use: `id`, `user_id`, `stream_id` (null unless the video is an archive), `title`, `created_at`, `duration` (string like `"3h8m33s"`, which we parse to seconds), `type`, `language`, `view_count`, `muted_segments` (`offset`, `duration`, both integer seconds).
- There's no game/category field, so `vods.game_ids` can't be filled from Get Videos.
- `muted_segments` is `null` when no segments are muted. Normalize documented null to `[]` internally; missing/malformed fields require validation rather than silently assuming no muting.

### Get Clips: <https://dev.twitch.tv/docs/api/reference/#get-clips>

- Query by `broadcaster_id` (plus `started_at`/`ended_at`), by `game_id`, or by clip `id`.
- **There's no "clips for this video" filter.** Query the broadcaster by clip-creation date range and keep rows whose `video_id` matches. The broadcast's wall-clock interval is only an initial discovery range; refreshed snapshots need a documented horizon for clips created later.
- Fields we use: `id`, `broadcaster_id`, `creator_id` (hashed at ingest), `video_id`, `game_id`, `language`, `title`, `view_count`, `created_at`, `duration`, `vod_offset`, `is_featured`.
- **`vod_offset` can be null**, e.g. when the VOD is deleted or not yet processed. Store the row but exclude it from labeling.
- `video_id` may be an **empty string** when the associated video is unavailable; normalize it to internal null. Broadcaster/game queries return clips by descending views, so incomplete discovery can favor popular clips.
- **Pagination limit (verified 2026-10-09):** [Get Clips](https://dev.twitch.tv/docs/api/reference/#get-clips) returns approximately 1,000 results across pages for a query. **Chunk queries into short time ranges** and deduplicate by `clip_id`; subdivide saturated ranges. A completed page sequence does not prove all clips were retrieved. ⚠️ Measure coverage and record potentially incomplete ranges in the early spike.
- **Clip creation time is not VOD event time.** Date filters select when a clip was created; viewers can clip a past VOD later. Searching only the broadcast's wall-clock interval can miss those clips. The collection spike must choose and document the later discovery horizon for refreshed snapshots, then filter by `video_id`.
- ⚠️ **View counts grow over time** and may be updated with a delay. Always store `fetched_at`. For labeling, prefer a consistent observation lag (e.g. re-fetch clips N days after the VOD; see [collection plan](collection-plan.md)).

### Authorized clip media

The current official [Get Clips Download](https://dev.twitch.tv/docs/api/reference/#get-clips-download) endpoint offers download URLs for authorized broadcaster/editor workflows. It is not an unrestricted arbitrary-channel VOD download API. Evaluate this route and its current authorization requirements before defaulting to unofficial media tooling; it covers existing clips, not additional surrounding VOD footage. OQ-17 still governs retention, preview and export permissions.

### Get Users: <https://dev.twitch.tv/docs/api/reference/#get-users>

- Resolve `login` → `id` and `broadcaster_type` (`partner`/`affiliate`/`""`). Up to 100 `id` and `login` values in total per request.

### VOD retention

Checked 2026-10-13: [Twitch's VOD policy](https://help.twitch.tv/s/article/video-on-demand) states 7 days for ordinary broadcasters, 14 for Affiliates and 60 for Partners, Prime and Turbo users. `broadcaster_type` alone does not reveal every retention entitlement. Deletion or unpublishing may happen earlier. Features do not preserve video; secure permitted demo/evaluation media while available and report unavailable sources ([collection](collection-plan.md)). Recheck policy before implementing collection.

## 2. Chat replay: TwitchDownloader (unofficial)

- **Repo:** <https://github.com/lay295/TwitchDownloader>. We use the **CLI** (`TwitchDownloaderCLI`), not the GUI.
- **Pinned version:** release **1.56.5** (tag `1.56.5`, commit `f8335ca`), which writes chat JSON format **1.4.0** ([`ChatRootInfo.cs` L16][td-info]). `autoclipper.ingest.chat` rejects other format versions with `ChatDownloadError` and logs a warning for other tool versions. It reads the tool version from the banner the CLI prints first on stdout, `TwitchDownloaderCLI 1.56.5 Copyright (c) …` ([`Program.cs` L79–L94][td-banner]). Per the source, `TwitchDownloaderCLI --version` prints the name and version to stderr and exits with code 1.
- The wrapper runs ([`ChatDownloadArgs.cs`][td-args], [`IFileCollisionArgs.cs`][td-collision]):

  ```bash
  TwitchDownloaderCLI chatdownload --id <video_id> --output data/raw/chat/<video_id>.json --collision Overwrite
  ```

  Optional `--embed-images` also embeds the BTTV/FFZ/7TV emote lists (with images), the only way the file reveals third-party emotes.
- **Fields we read** (all from [`ChatRoot.cs`][td-root] unless noted; how the downloader fills them: [`ChatDownloader.cs`][td-dl]):

  | JSON field | Becomes | Source |
  |---|---|---|
  | `FileInfo.Version` `{Major, Minor, Patch}` | format check | [`ChatRootInfo.cs` L5][td-info] |
  | `video.id` | file must match the requested VOD | L203; [downloader L456][td-dl-vid] |
  | `comments[].content_id` | per-message VOD check | L163; [downloader L170][td-dl-comment] |
  | `comments[].content_offset_seconds` | `offset_s` | L164 |
  | `comments[].commenter._id` | `user_hash` (the stable user ID; `name` is the login, `display_name` the display name) | L23–L25; [downloader L172–L177][td-dl-comment]; [GQL `commenter.id`][td-gql] |
  | `commenter.name`, `commenter.display_name` | only used to redact names from text; a comment without both is skipped as malformed | L23, L25 |
  | `message.body` | `text`, redacted (concatenated fragment texts) | L105; [downloader L248][td-dl-body] |
  | `message.fragments[].text` / `.emoticon.emoticon_id` | `emote_ids`, `n_emotes` | L49, L55–L56, L107 |
  | `message.user_badges[]._id` | `is_subscriber` (`subscriber` or `founder`), `is_moderator` (`moderator`) | L71, L108 |
  | `message.bits_spent` | `bits` (0 if absent). The downloader takes the first cheer amount in the body ([`TwitchRegex.cs` L10][td-bits]), so later cheers in the same message aren't added. | L106; [downloader L250–L254][td-dl-body] |
  | `embeddedData.thirdParty[].name` | third-party emotes in `n_emotes`: whitespace-separated tokens of non-emote fragments that equal a name | L231, L263; [downloader L561–L576][td-dl-embed] |

- **Risks:** relies on Twitch's undocumented internal API and can break without warning. Pin the version in the catalog (`tool` column) and fail loudly on schema surprises.
- **Privacy:** raw output contains usernames. Parse transiently, apply the approved OQ-9 minimization/redaction policy and delete raw files after validation. No raw sharing; git-ignored storage is not a retention authorization. The parser hashes `commenter._id`, deletes the raw file after a successful parse and redacts text: `@` mentions become `@user`, and any word equal to a commenter's login or display name, or a viewer named in a gift-sub notice, becomes `user`. Its module docstring states the exact rules and what they miss.

[td-root]: https://github.com/lay295/TwitchDownloader/blob/1.56.5/TwitchDownloaderCore/TwitchObjects/ChatRoot.cs
[td-info]: https://github.com/lay295/TwitchDownloader/blob/1.56.5/TwitchDownloaderCore/TwitchObjects/ChatRootInfo.cs#L5-L16
[td-banner]: https://github.com/lay295/TwitchDownloader/blob/1.56.5/TwitchDownloaderCLI/Program.cs#L79-L94
[td-args]: https://github.com/lay295/TwitchDownloader/blob/1.56.5/TwitchDownloaderCLI/Modes/Arguments/ChatDownloadArgs.cs#L7-L26
[td-collision]: https://github.com/lay295/TwitchDownloader/blob/1.56.5/TwitchDownloaderCLI/Modes/Arguments/IFileCollisionArgs.cs#L8
[td-dl]: https://github.com/lay295/TwitchDownloader/blob/1.56.5/TwitchDownloaderCore/ChatDownloader.cs
[td-dl-vid]: https://github.com/lay295/TwitchDownloader/blob/1.56.5/TwitchDownloaderCore/ChatDownloader.cs#L456
[td-dl-comment]: https://github.com/lay295/TwitchDownloader/blob/1.56.5/TwitchDownloaderCore/ChatDownloader.cs#L164-L177
[td-dl-body]: https://github.com/lay295/TwitchDownloader/blob/1.56.5/TwitchDownloaderCore/ChatDownloader.cs#L196-L254
[td-dl-embed]: https://github.com/lay295/TwitchDownloader/blob/1.56.5/TwitchDownloaderCore/ChatDownloader.cs#L561-L576
[td-gql]: https://github.com/lay295/TwitchDownloader/blob/1.56.5/TwitchDownloaderCore/TwitchObjects/Gql/GqlCommentResponse.cs#L8-L13
[td-bits]: https://github.com/lay295/TwitchDownloader/blob/1.56.5/TwitchDownloaderCore/Tools/TwitchRegex.cs#L10-L11

## 3. VOD media: yt-dlp (unofficial)

- **Repo:** <https://github.com/yt-dlp/yt-dlp>
- **Audio only** for feature extraction. Twitch VODs usually expose an audio-only format:

  ```bash
  yt-dlp -f "bestaudio" -o "data/raw/audio/%(id)s.%(ext)s" https://www.twitch.tv/videos/<video_id>
  ```

- **Segments** for preview/export can reduce download cost, subject to source authorization and a pilot verifying tool behavior:

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

This is a provisional alignment estimate, not proof that API `created_at` is an exact live-clock origin. Validate against stream metadata and known source events, store the alignment basis and apply it once. Do not mix live wall-clock time with replay offsets without that check.

## 5. Third-party emotes (optional)

Chat in many communities is dominated by 7TV/BTTV/FFZ emotes, which appear as plain text in messages. Their public APIs list a channel's emote set, so we can count them as emotes rather than words. Optional for v1.

## 6. External short-form engagement data (stretch)

To predict performance **on short-form platforms** rather than on Twitch, we'd need short-form clip accounts' videos and view counts. One option is a third-party account-export tool licensed by a team member. It has a monthly limit on how many accounts can be scanned, so choose high-volume accounts. Use is subject to that tool's terms and the platforms' terms. See OQ-3 in [open questions](../project/open-questions.md). **Not part of v1/v2.**

## 7. Related public datasets (for context, not required)

- *Video Highlight Prediction Using Audience Chat Reactions* (Fu et al., EMNLP 2017) studied highlight prediction from live-stream chat and video for esports broadcasts. It's useful as related work and as a sanity check that chat signals work. Check its dataset license before using any data.
