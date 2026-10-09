# Data Model

| | |
|---|---|
| **Status** | Proposal. Becomes the binding contract once [ADR-0005](../decisions/0005-local-storage-parquet-duckdb.md) is accepted. |
| **Last updated** | 2026-10-09 |

These are the tables that pipeline stages exchange. **Any schema change, including adding a nullable column, requires an ADR or ADR update in the same PR** (see [AGENTS.md § Hard rules](../../AGENTS.md#6-hard-rules-never-break-these)), because it can break downstream stages and invalidate past results. Document the change here too.

## Conventions

- **Storage:** Parquet, one file per table, or partitioned by `video_id` for large tables.
- **IDs:** Twitch IDs are **strings**, kept exactly as the API returns them.
- **VOD time:** `float64` seconds from VOD start, with column names ending `_s`.
- **Wall-clock time:** `timestamp[us, tz=UTC]`, column names ending `_at`.
- **Pseudonymized user identifiers:** `str` HMAC-SHA256 hex digests (salted, see `.env.example`), column names ending `_hash`.
- **Nullability:** every column is non-null unless marked *nullable*.

## Entity relationships

```mermaid
erDiagram
    BROADCASTER ||--o{ VOD : streams
    VOD ||--o{ CLIP : "has (via video_id)"
    VOD ||--o{ CHAT_MESSAGE : has
    VOD ||--o{ WINDOW : "sliced into"
    VOD ||--o{ MOMENT : has
    MOMENT ||--|{ CLIP : "merges"
    WINDOW }o--o{ MOMENT : "overlaps"
    WINDOW ||--o{ FEATURE_ROW : "described by"
    WINDOW ||--o{ PREDICTION : "scored by"
    MOMENT ||--o{ PREDICTION : "scored by"
```

---

## `broadcasters`

| Column | Type | Notes |
|---|---|---|
| `broadcaster_id` | str | Twitch user ID |
| `login` | str | Channel login name (public) |
| `display_name` | str | |
| `broadcaster_type` | str | `partner`, `affiliate` or `""` (affects VOD retention) |
| `fetched_at` | timestamp | |

Streamer identities are public and not pseudonymized. Chatters are (see `chat_messages`).

## `vods`

Source: Helix **Get Videos**.

| Column | Type | Notes |
|---|---|---|
| `video_id` | str | Primary key |
| `broadcaster_id` | str | FK → `broadcasters` |
| `stream_id` | str, *nullable* | |
| `title` | str | |
| `created_at` | timestamp | When the broadcast started |
| `duration_s` | float | Parsed from Twitch's `"3h8m33s"` format |
| `type` | str | `archive` (past broadcast), `highlight`, `upload`. We use `archive`. |
| `language` | str | |
| `view_count` | int | At fetch time |
| `muted_segments` | list<struct<offset_s: float, duration_s: float>> | Audio is muted here, so loudness features are unreliable in these spans |
| `game_ids` | list<str>, *nullable* | If derivable (e.g. from clips). A VOD can span several categories. |
| `fetched_at` | timestamp | |

## `clips`

Source: Helix **Get Clips**. One row per viewer-made clip.

| Column | Type | Notes |
|---|---|---|
| `clip_id` | str | Primary key |
| `video_id` | str, *nullable* | FK → `vods`. Null if the clip isn't linked to an available VOD. |
| `broadcaster_id` | str | |
| `creator_hash` | str | Pseudonymized clip creator |
| `title` | str | Written by the clipper. May be useful as weak text signal. |
| `game_id` | str | Category at clip time |
| `language` | str | |
| `created_at` | timestamp | When the clip was made |
| `vod_offset_s` | float, *nullable* | Clip start in the VOD. **Null** happens (VOD deleted, processing delay). Such rows are excluded from labeling. |
| `duration_s` | float | Clip length (Twitch clips are short, typically ≤ 60 s) |
| `view_count` | int | At `fetched_at`. Grows over time, so always store when it was observed. |
| `is_featured` | bool, *nullable* | |
| `fetched_at` | timestamp | |

## `chat_messages`

Source: TwitchDownloader chat replay (primary) or the live chat logger (backup). Partitioned by `video_id`.

| Column | Type | Notes |
|---|---|---|
| `video_id` | str | |
| `offset_s` | float | Message time from VOD start |
| `user_hash` | str | Pseudonymized author. **Never store raw names.** |
| `text` | str | Message text. May contain other users' @-mentions. See governance for the redaction policy. |
| `emote_ids` | list<str> | Native Twitch emotes in the message |
| `n_emotes` | int | Including third-party emotes where detectable |
| `is_subscriber` | bool, *nullable* | From badges |
| `is_moderator` | bool, *nullable* | From badges |
| `bits` | int | 0 if none |
| `source` | str | `replay` or `live` |

## `moments`

Derived from `clips` by the moment-merging rule in [labeling](../data/labeling.md#2-from-clips-to-moments).

| Column | Type | Notes |
|---|---|---|
| `moment_id` | str | Deterministic: `{video_id}:{start_s:.0f}` |
| `video_id` | str | |
| `start_s` | float | Earliest start among member clips |
| `end_s` | float | Latest end among member clips |
| `n_clips` | int | Number of distinct viewer clips merged |
| `total_views` | int | Sum of member clip views (at observation time) |
| `max_views` | int | |
| `first_clip_created_at` | timestamp | Lets us measure how fast viewers clipped the moment |
| `clip_ids` | list<str> | |
| `views_observed_at` | timestamp | Latest `fetched_at` among member clips |

## `windows`

Fixed-length slices of each VOD. Built by [labeling](../data/labeling.md#3-windows).

| Column | Type | Notes |
|---|---|---|
| `window_id` | str | `{video_id}:{index}` |
| `video_id` | str | |
| `index` | int | 0-based |
| `start_s` | float | |
| `end_s` | float | |
| `is_muted` | bool | Overlaps a muted segment |
| `label_pos` | bool | Window overlaps a moment (after lag rules) |
| `label_n_clips` | int | Graded label: clips overlapping this window |
| `label_moment_id` | str, *nullable* | |
| `split` | str | `train`, `val` or `test`, from the saved split file |

## `features_<family>`

One table per signal family (`features_chat`, `features_audio`, `features_video`), keyed by `window_id`. Column definitions live in [features](../ml/features.md), and that file is the contract for feature names.

| Column | Type | Notes |
|---|---|---|
| `window_id` | str | Join key |
| `<feature columns>` | float | Named `<family>_<signal>_<stat>`, e.g. `chat_msg_rate_z` |
| `feature_version` | str | Bumped when a feature's definition changes |

## `predictions`

| Column | Type | Notes |
|---|---|---|
| `run_id` | str | Experiment/run that produced it |
| `target_type` | str | `window` or `moment` |
| `target_id` | str | `window_id` or `moment_id` |
| `score` | float | Higher = more clip-worthy |
| `top_signals` | list<str>, *nullable* | For explanations (v2) |

## `candidates` (API/CLI output, JSON)

What `autoclipper analyze` returns and what the UI renders.

```json
{
  "video_id": "1234567890",
  "model_run_id": "exp-007",
  "candidates": [
    {
      "candidate_id": "1234567890:15342",
      "rank": 1,
      "score": 0.93,
      "start_s": 15342.0,
      "end_s": 15372.0,
      "source": "viewer_clips",
      "n_viewer_clips": 14,
      "top_signals": ["chat_msg_rate_z", "audio_rms_max_z"]
    }
  ]
}
```

`source` is `viewer_clips` (v1) or `model` (v2: a moment no viewer clipped).

## `catalog`

| Column | Type | Notes |
|---|---|---|
| `object_type` | str | `vod_meta`, `clips`, `chat`, `audio`, … |
| `object_key` | str | Usually `video_id` |
| `status` | str | `ok`, `failed`, `unavailable` (e.g. VOD deleted) |
| `tool` | str | e.g. `helix`, `twitchdownloader@1.x`, `yt-dlp@2026.x` |
| `stage_version` | str | |
| `created_at` | timestamp | |
| `error` | str, *nullable* | |

## Dataset versioning

A **dataset version** (e.g. `v1`) pins: the set of `video_id`s, the label rules version, the feature versions and the split file. It's recorded in `data/splits/<version>.json` along with the counts. Experiments always cite a dataset version. Bump the version when any of these change, never edit one in place.
