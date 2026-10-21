"""Arrow schemas for the synthetic tables.

They mirror the **proposed** tables in docs/architecture/data-model.md (column names, types and
nullability) for prototype use. They are not adopted contracts: the real schemas are ratified
under ADR-0009, and a change there must be mirrored here. Following the data model's
conventions: Twitch IDs are strings, VOD positions are ``float64`` seconds (``*_s``), wall-clock
times are ``timestamp[us, tz=UTC]`` (``*_at``) and every column is non-null unless the data model
marks it *nullable*.
"""

import pyarrow as pa

_TIMESTAMP = pa.timestamp("us", tz="UTC")


def _schema(*fields: tuple[str, pa.DataType] | tuple[str, pa.DataType, bool]) -> pa.Schema:
    """Build a schema from ``(name, type)`` (non-null) or ``(name, type, True)`` (nullable)."""
    return pa.schema([pa.field(f[0], f[1], nullable=len(f) == 3 and f[2]) for f in fields])


BROADCASTERS = _schema(
    ("broadcaster_id", pa.string()),
    ("login", pa.string()),
    ("display_name", pa.string()),
    ("broadcaster_type", pa.string()),
    ("fetched_at", _TIMESTAMP),
)

MUTED_SEGMENT = pa.struct(
    [
        pa.field("offset_s", pa.float64(), nullable=False),
        pa.field("duration_s", pa.float64(), nullable=False),
    ]
)

VODS = _schema(
    ("video_id", pa.string()),
    ("broadcaster_id", pa.string()),
    ("stream_id", pa.string(), True),
    ("title", pa.string()),
    ("created_at", _TIMESTAMP),
    ("duration_s", pa.float64()),
    ("type", pa.string()),
    ("language", pa.string()),
    ("view_count", pa.int64()),
    ("muted_segments", pa.list_(pa.field("element", MUTED_SEGMENT, nullable=False))),
    ("game_ids", pa.list_(pa.field("element", pa.string(), nullable=False)), True),
    ("fetched_at", _TIMESTAMP),
)

CLIPS = _schema(
    ("clip_id", pa.string()),
    ("video_id", pa.string(), True),
    ("broadcaster_id", pa.string()),
    ("creator_hash", pa.string()),
    ("title", pa.string()),
    ("game_id", pa.string()),
    ("language", pa.string()),
    ("created_at", _TIMESTAMP),
    ("vod_offset_s", pa.float64(), True),
    ("duration_s", pa.float64()),
    ("view_count", pa.int64()),
    ("is_featured", pa.bool_(), True),
    ("fetched_at", _TIMESTAMP),
)

CHAT_MESSAGES = _schema(
    ("video_id", pa.string()),
    ("offset_s", pa.float64()),
    ("user_hash", pa.string()),
    ("text", pa.string()),
    ("emote_ids", pa.list_(pa.field("element", pa.string(), nullable=False))),
    ("n_emotes", pa.int64()),
    ("is_subscriber", pa.bool_(), True),
    ("is_moderator", pa.bool_(), True),
    ("bits", pa.int64()),
    ("source", pa.string()),
)

CATALOG = _schema(
    ("object_type", pa.string()),
    ("object_key", pa.string()),
    ("status", pa.string()),
    ("tool", pa.string()),
    ("stage_version", pa.string()),
    ("created_at", _TIMESTAMP),
    ("error", pa.string(), True),
)
