"""Fetch the viewer clips of one VOD from Helix Get Clips.

Get Clips has no "clips of this video" filter (docs/data/data-sources.md § Get Clips). It lists
a broadcaster's clips by **creation time**, so `fetch_vod_clips` searches the clips created from
the broadcast's start until the capture time (viewers can clip a past VOD days later), in
consecutive date ranges. It keeps the clips whose ``video_id`` is the VOD's and drops duplicates:
Twitch's date filter includes clips created exactly on a range boundary in both ranges.

Twitch returns about 1,000 clips per query, across all pages, sorted by views. A range whose
listing reaches `SATURATION_RESULTS` may have lost clips past that limit, so it is split into
`SPLIT_PARTS` shorter ranges that are queried too, down to `MIN_RANGE`. A range that is still
saturated at that size is kept and reported in `ClipFetchSummary.saturated_ranges`: its clips
are possibly incomplete, never silently treated as complete. Ranges that were split are listed
in `ClipFetchSummary.split_ranges`.

Rows follow the ``clips`` table in docs/architecture/data-model.md. Creator IDs are pseudonymized
with `autoclipper.privacy.pseudonymize` as they are parsed; the raw ID is not kept. Saving the
rows to disk is the ingest command's job (#34).
"""

import itertools
import logging
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

from pydantic import SecretStr

from autoclipper.ingest.twitch_api import HelixClient, ResponseRecord, Vod
from autoclipper.privacy import pseudonymize

log = logging.getLogger(__name__)

CLIPS_PATH: Final = "/clips"

# Get Clips returns about 1,000 results per query across pages (verified 2026-10-09,
# data-sources.md). Paging stops there; a listing that reaches `SATURATION_RESULTS` counts as
# possibly cut off, with a margin because the exact cap isn't documented.
MAX_RESULTS_PER_QUERY: Final = 1000
SATURATION_RESULTS: Final = 900

# The first ranges are this long; a saturated range is split into `SPLIT_PARTS` shorter ones,
# down to `MIN_RANGE`. Six hours keeps a typical stream in one or two queries; a minute is about
# the busiest a range can usefully get. Quarters rather than halves need fewer levels of
# re-querying on a busy channel.
DEFAULT_RANGE: Final = timedelta(hours=6)
MIN_RANGE: Final = timedelta(minutes=1)
SPLIT_PARTS: Final = 4


@dataclass(frozen=True, slots=True)
class TimeRange:
    """A span of clip creation time, both ends aware UTC datetimes in whole seconds.

    Twitch's ``started_at``/``ended_at`` filter includes both ends, so neighbouring ranges share
    their boundary second; duplicates are dropped by clip ID.
    """

    started_at: datetime
    ended_at: datetime

    def __post_init__(self) -> None:
        for name, value in (("started_at", self.started_at), ("ended_at", self.ended_at)):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"{name} must be timezone-aware")
            if value.microsecond:
                raise ValueError(f"{name} must be a whole second")
        if self.ended_at <= self.started_at:
            raise ValueError("ended_at must be after started_at")

    @property
    def length(self) -> timedelta:
        """How long the range is."""
        return self.ended_at - self.started_at

    def split(self, parts: int) -> tuple["TimeRange", ...]:
        """Split into `parts` consecutive ranges of about equal length, in whole seconds.

        Raises:
            ValueError: `parts` is below 2 or more than the range's seconds.
        """
        seconds = int(self.length.total_seconds())
        if not 2 <= parts <= seconds:
            raise ValueError(f"can't split a {seconds} s range into {parts} parts")
        bounds = [self.started_at + timedelta(seconds=seconds * i // parts) for i in range(parts)]
        bounds.append(self.ended_at)
        return tuple(TimeRange(a, b) for a, b in itertools.pairwise(bounds))

    def query(self) -> list[tuple[str, str]]:
        """The ``started_at``/``ended_at`` query parameters, in RFC 3339."""
        return [("started_at", _rfc3339(self.started_at)), ("ended_at", _rfc3339(self.ended_at))]


@dataclass(frozen=True, slots=True)
class Clip:
    """A viewer clip from Get Clips: a row of the ``clips`` table (data-model.md).

    Attributes:
        clip_id: The clip ID (``id``), an opaque string.
        video_id: The VOD the clip was made from; None when Twitch reports ``""`` (no VOD link).
        broadcaster_id: The channel's user ID.
        creator_hash: The clip creator's user ID, pseudonymized.
        title: The title the clipper wrote.
        game_id: The category at clip time.
        language: ISO 639-1 code, or ``"other"``.
        created_at: When the clip was made (UTC).
        vod_offset_s: Seconds from VOD start to the clip's start; None when Twitch has none
            (VOD deleted or still processing). Such clips can't be used as labels.
        duration_s: The clip's length in seconds.
        view_count: Views at ``fetched_at``.
        is_featured: Whether the broadcaster featured the clip; None if Twitch didn't say.
        fetched_at: When the response holding the clip was received (UTC).
    """

    clip_id: str
    video_id: str | None
    broadcaster_id: str
    creator_hash: str
    title: str
    game_id: str
    language: str
    created_at: datetime
    vod_offset_s: float | None
    duration_s: float
    view_count: int
    is_featured: bool | None
    fetched_at: datetime

    @property
    def has_vod_offset(self) -> bool:
        """False for clips without a VOD offset, which are kept but excluded from labeling."""
        return self.vod_offset_s is not None


@dataclass(frozen=True, slots=True)
class ClipFetchSummary:
    """What the search for a VOD's clips covered and found.

    Attributes:
        search_range: The whole creation-time span searched (broadcast start to capture time).
        ranges_queried: Every range queried, in order, including ones then split.
        split_ranges: Ranges that reached the result limit and were split into shorter ones.
        saturated_ranges: Ranges that reached the result limit even at the minimum length;
            clips created in them may be missing.
        n_clips: Clips of the VOD returned (after dropping duplicates).
        n_without_vod_offset: Of those, clips without a VOD offset.
        n_other_videos: Distinct clips of the channel that belong to another VOD or to none.
        n_duplicates: Repeated listings of the VOD's clips dropped (from range boundaries and
            from re-querying split ranges).
    """

    search_range: TimeRange
    ranges_queried: tuple[TimeRange, ...]
    split_ranges: tuple[TimeRange, ...]
    saturated_ranges: tuple[TimeRange, ...]
    n_clips: int
    n_without_vod_offset: int
    n_other_videos: int
    n_duplicates: int

    @property
    def complete(self) -> bool:
        """True when no range stayed at the result limit (Twitch gives no stronger guarantee)."""
        return not self.saturated_ranges


@dataclass(frozen=True, slots=True)
class VodClips:
    """The clips of one VOD, in creation order, and how they were found."""

    clips: tuple[Clip, ...]
    summary: ClipFetchSummary


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


def fetch_vod_clips(
    client: HelixClient,
    vod: Vod,
    *,
    salt: SecretStr,
    captured_at: datetime | None = None,
    clock: Callable[[], datetime] = _utc_now,
    initial_range: timedelta = DEFAULT_RANGE,
    min_range: timedelta = MIN_RANGE,
) -> VodClips:
    """Fetch every viewer clip of `vod` that Get Clips lists.

    The same as `fetch_channel_clips` for one VOD; see there for the arguments.
    """
    by_video = fetch_channel_clips(
        client,
        [vod],
        salt=salt,
        captured_at=captured_at,
        clock=clock,
        initial_range=initial_range,
        min_range=min_range,
    )
    return by_video[vod.video_id]


def fetch_channel_clips(
    client: HelixClient,
    vods: Sequence[Vod],
    *,
    salt: SecretStr,
    captured_at: datetime | None = None,
    clock: Callable[[], datetime] = _utc_now,
    initial_range: timedelta = DEFAULT_RANGE,
    min_range: timedelta = MIN_RANGE,
) -> dict[str, VodClips]:
    """Fetch the viewer clips of several VODs of one channel with one listing.

    The channel's clips are listed once, from the earliest VOD's start to the capture time, and
    sorted into the VODs, so ingesting several VODs of a channel doesn't list the same weeks
    again for each.

    Args:
        client: The Helix client.
        vods: The VODs, from `HelixClient.get_videos`, all of one broadcaster. The earliest
            ``created_at`` starts the search.
        salt: The pseudonymization key for creator IDs (``AUTOCLIPPER_CHAT_HASH_SALT``).
        captured_at: The end of the search; defaults to now (from `clock`).
        clock: Returns the current time as an aware datetime.
        initial_range: Length of the first ranges.
        min_range: The shortest range a saturated range is split into; at least 1 second.

    Returns:
        Each VOD's clips by ``video_id``, sorted by ``created_at`` then ``clip_id``, with a
        summary. The ranges in the summaries are the shared listing's.

    Raises:
        ValueError: `vods` is empty, mixes broadcasters or repeats a VOD; `captured_at` is
            naive or not after the earliest VOD's start; or a range length is too short.
        TwitchAPIError: A request failed; `TwitchResponseError` if a record of one of the VODs
            is malformed, or a record belongs to another broadcaster.
    """
    if not vods:
        raise ValueError("vods is empty")
    broadcaster_id = vods[0].broadcaster_id
    if any(vod.broadcaster_id != broadcaster_id for vod in vods):
        raise ValueError("vods must all belong to one broadcaster")
    wanted = {vod.video_id for vod in vods}
    if len(wanted) != len(vods):
        raise ValueError("vods lists a VOD more than once")
    if initial_range <= timedelta(0):
        raise ValueError("initial_range must be positive")
    if min_range < timedelta(seconds=1):
        raise ValueError("min_range must be at least 1 second (Twitch filters by the second)")
    end = clock() if captured_at is None else captured_at
    if end.tzinfo is None or end.utcoffset() is None:
        raise ValueError("captured_at must be timezone-aware")
    first_start = min(vod.created_at for vod in vods)
    search = TimeRange(_floor_second(first_start), _ceil_second(end))  # checks the order

    pending = _consecutive_ranges(search, initial_range)
    pending.reverse()  # a stack: pop the earliest range first
    queried: list[TimeRange] = []
    split: list[TimeRange] = []
    saturated: list[TimeRange] = []
    found: dict[str, dict[str, Clip]] = {video_id: {} for video_id in wanted}
    duplicates = dict.fromkeys(wanted, 0)
    listed: set[str] = set()  # every distinct clip ID of the channel seen
    while pending:
        current = pending.pop()
        queried.append(current)
        records = _list_clips(client, broadcaster_id, current)
        if len(records) >= SATURATION_RESULTS:
            parts = min(SPLIT_PARTS, int(current.length / min_range))
            if parts >= 2:
                # The clips already listed are kept (they are real); the shorter ranges then
                # find the ones past the limit, and their repeats are dropped as duplicates.
                pending.extend(reversed(current.split(parts)))
                split.append(current)
                how = "splitting"
            else:
                saturated.append(current)
                how = "still saturated at the minimum length"
            log.info(
                "clip range at the result limit, %s broadcaster_id=%s started_at=%s ended_at=%s "
                "n=%d",
                how,
                broadcaster_id,
                _rfc3339(current.started_at),
                _rfc3339(current.ended_at),
                len(records),
            )
        for record, fetched_at in records:
            if record.require_str("broadcaster_id") != broadcaster_id:
                raise record.invalid("broadcaster_id", "is not the broadcaster queried")
            clip_id = record.require_str("id", allow_empty=False)
            listed.add(clip_id)
            video_id = record.require_str("video_id")
            if video_id not in wanted:
                continue  # Another VOD's clip: not parsed, so its fields can't fail the fetch.
            if clip_id in found[video_id]:
                duplicates[video_id] += 1
            else:
                found[video_id][clip_id] = _parse_clip(record, fetched_at, salt)

    result: dict[str, VodClips] = {}
    for vod in vods:
        clips = tuple(
            sorted(found[vod.video_id].values(), key=lambda clip: (clip.created_at, clip.clip_id))
        )
        summary = ClipFetchSummary(
            search_range=search,
            ranges_queried=tuple(queried),
            split_ranges=tuple(split),
            saturated_ranges=tuple(saturated),
            n_clips=len(clips),
            n_without_vod_offset=sum(not clip.has_vod_offset for clip in clips),
            n_other_videos=len(listed) - len(clips),
            n_duplicates=duplicates[vod.video_id],
        )
        if saturated:
            log.warning(
                "clips possibly incomplete video_id=%s saturated_ranges=%d",
                vod.video_id,
                len(saturated),
            )
        log.info(
            "fetched clips video_id=%s n=%d without_offset=%d other_videos=%d duplicates=%d "
            "ranges=%d split=%d saturated=%d",
            vod.video_id,
            summary.n_clips,
            summary.n_without_vod_offset,
            summary.n_other_videos,
            summary.n_duplicates,
            len(queried),
            len(split),
            len(saturated),
        )
        result[vod.video_id] = VodClips(clips, summary)
    return result


def _list_clips(
    client: HelixClient, broadcaster_id: str, span: TimeRange
) -> list[tuple[ResponseRecord, datetime]]:
    listing = client.get_pages(
        CLIPS_PATH,
        [("broadcaster_id", broadcaster_id), *span.query()],
        max_results=MAX_RESULTS_PER_QUERY,
    )
    return [(record, page.fetched_at) for page in listing.items for record in page.records]


def _parse_clip(record: ResponseRecord, fetched_at: datetime, salt: SecretStr) -> Clip:
    creator_id = record.require_str("creator_id", allow_empty=False)
    if "vod_offset" not in record.raw:  # null is documented; a missing field is not
        raise record.invalid("vod_offset", "is missing")
    vod_offset = record.raw["vod_offset"]
    return Clip(
        clip_id=record.require_str("id", allow_empty=False),
        video_id=record.require_str("video_id") or None,
        broadcaster_id=record.require_str("broadcaster_id", allow_empty=False),
        creator_hash=pseudonymize(creator_id, salt),
        title=record.require_str("title"),
        game_id=record.require_str("game_id"),
        language=record.require_str("language"),
        created_at=record.require_timestamp("created_at"),
        vod_offset_s=None if vod_offset is None else _seconds(record, "vod_offset"),
        duration_s=_seconds(record, "duration"),
        view_count=record.require_int("view_count"),
        is_featured=_optional_bool(record, "is_featured"),
        fetched_at=fetched_at,
    )


def _seconds(record: ResponseRecord, field: str) -> float:
    """A non-negative, finite number of seconds (Twitch sends ``duration`` as a float)."""
    value = record.raw.get(field)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise record.invalid(field, f"is {type(value).__name__}, expected a number")
    if not math.isfinite(value) or value < 0:
        raise record.invalid(field, "is negative or not finite")
    return float(value)


def _optional_bool(record: ResponseRecord, field: str) -> bool | None:
    value = record.raw.get(field)
    if value is not None and not isinstance(value, bool):
        raise record.invalid(field, f"is {type(value).__name__}, expected a boolean")
    return value


def _consecutive_ranges(search: TimeRange, length: timedelta) -> list[TimeRange]:
    step = timedelta(seconds=max(1, math.ceil(length.total_seconds())))
    ranges: list[TimeRange] = []
    start = search.started_at
    while start < search.ended_at:
        end = min(start + step, search.ended_at)
        ranges.append(TimeRange(start, end))
        start = end
    return ranges


def _floor_second(value: datetime) -> datetime:
    return value.astimezone(UTC).replace(microsecond=0)


def _ceil_second(value: datetime) -> datetime:
    floored = _floor_second(value)
    return floored if floored == value else floored + timedelta(seconds=1)


def _rfc3339(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
