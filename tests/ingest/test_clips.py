"""Tests for fetching a VOD's viewer clips, against fake Get Clips responses (no network)."""

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from autoclipper.config import TwitchCredentials
from autoclipper.ingest.clips import (
    MAX_RESULTS_PER_QUERY,
    SATURATION_RESULTS,
    TimeRange,
    VodClips,
    fetch_vod_clips,
)
from autoclipper.ingest.twitch_api import HelixClient, TwitchResponseError, Vod
from autoclipper.privacy import pseudonymize

FIXTURES = Path(__file__).parent / "fixtures"
SALT = SecretStr("synthetic-test-salt")
VIDEO_ID = "9100000001"
BROADCASTER_ID = "9000000001"
VOD_START = datetime(2026, 10, 1, 18, 0, 5, tzinfo=UTC)
CAPTURED_AT = datetime(2026, 10, 9, 18, 0, 5, tzinfo=UTC)


def make_vod(created_at: datetime = VOD_START) -> Vod:
    return Vod(
        video_id=VIDEO_ID,
        broadcaster_id=BROADCASTER_ID,
        stream_id="9200000001",
        title="Synthetic stream one",
        created_at=created_at,
        duration_s=11313.0,
        type="archive",
        language="en",
        view_count=1234,
        muted_segments=(),
        game_ids=None,
        fetched_at=CAPTURED_AT,
    )


def load(name: str) -> dict[str, object]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def parse_time(text: str) -> datetime:
    return datetime.fromisoformat(text)


def rfc3339(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def clip_record(
    index: int,
    created_at: datetime,
    *,
    video_id: str = VIDEO_ID,
    vod_offset: int | None = 100,
    views: int = 1,
) -> dict[str, object]:
    return {
        "id": f"SyntheticClip{index:05d}",
        "broadcaster_id": BROADCASTER_ID,
        "creator_id": f"{9300000000 + index}",
        "video_id": video_id,
        "game_id": "509658",
        "language": "en",
        "title": f"synthetic clip {index}",
        "view_count": views,
        "created_at": rfc3339(created_at),
        "duration": 30.0,
        "vod_offset": vod_offset,
        "is_featured": False,
    }


@dataclass
class FakeGetClips:
    """Answers Get Clips like Helix: clips created in [started_at, ended_at] (both included),
    most-viewed first, 100 per page, and never more than `MAX_RESULTS_PER_QUERY` per query."""

    clips: list[dict[str, object]]
    queries: list[tuple[datetime, datetime]] = field(default_factory=list)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "id.twitch.tv":
            return httpx.Response(
                200, json={"access_token": "fake-token", "expires_in": 3600, "token_type": "bearer"}
            )
        params = request.url.params
        assert request.url.path == "/helix/clips"
        assert params["broadcaster_id"] == BROADCASTER_ID
        start, end = parse_time(params["started_at"]), parse_time(params["ended_at"])
        offset = int(params.get("after", "0"))
        if offset == 0:
            self.queries.append((start, end))
        matching = [c for c in self.clips if start <= parse_time(str(c["created_at"])) <= end]
        matching.sort(key=lambda c: -int(str(c["view_count"])))
        matching = matching[:MAX_RESULTS_PER_QUERY]
        page = matching[offset : offset + int(params["first"])]
        following = offset + len(page)
        pagination = {"cursor": str(following)} if following < len(matching) else {}
        return httpx.Response(200, json={"data": page, "pagination": pagination})


def client_for(handler: Callable[[httpx.Request], httpx.Response]) -> HelixClient:
    credentials = TwitchCredentials(SecretStr("fake-client-id"), SecretStr("fake-client-secret"))
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return HelixClient(
        credentials, http_client=http, clock=lambda: CAPTURED_AT, sleep=lambda _: None
    )


class Replay:
    """Replays recorded Helix pages in order and records the requests."""

    def __init__(self, *pages: dict[str, object]) -> None:
        self.pages = list(pages)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "id.twitch.tv":
            return httpx.Response(
                200, json={"access_token": "fake-token", "expires_in": 3600, "token_type": "bearer"}
            )
        self.requests.append(request)
        return httpx.Response(200, json=self.pages.pop(0) if self.pages else {"data": []})


# --- Recorded pages --------------------------------------------------------------------------


def fetch_recorded() -> tuple[Replay, VodClips]:
    replay = Replay(load("helix_clips_page1.json"), load("helix_clips_page2.json"))
    result = fetch_vod_clips(
        client_for(replay),
        make_vod(),
        salt=SALT,
        captured_at=VOD_START + timedelta(hours=5),
    )
    return replay, result


def test_recorded_pages_are_followed_by_cursor_and_parsed_into_clip_rows() -> None:
    replay, result = fetch_recorded()

    assert len(replay.requests) == 2  # one range, two pages
    first, second = replay.requests
    assert first.url.params["started_at"] == "2026-10-01T18:00:05Z"
    assert first.url.params["ended_at"] == "2026-10-01T23:00:05Z"
    assert "after" not in first.url.params
    assert second.url.params["after"] == "fake-cursor-page2"
    clips = result.clips
    assert [c.clip_id for c in clips] == [
        "SyntheticClip0001",
        "SyntheticClip0002",
        "SyntheticClip0004",
    ]
    one = clips[0]
    assert one.video_id == VIDEO_ID
    assert one.broadcaster_id == BROADCASTER_ID
    assert one.vod_offset_s == 1195.0
    assert isinstance(one.vod_offset_s, float)
    assert one.duration_s == pytest.approx(29.9)
    assert one.view_count == 900
    assert one.created_at == datetime(2026, 10, 1, 18, 20, tzinfo=UTC)
    assert one.fetched_at == CAPTURED_AT
    assert one.title == "synthetic clip 1"
    assert (one.game_id, one.language, one.is_featured) == ("509658", "en", False)


def test_creator_ids_are_pseudonymized_and_never_kept_raw() -> None:
    _, result = fetch_recorded()

    clip = result.clips[0]
    assert clip.creator_hash == pseudonymize("9300000001", SALT)
    assert "9300000001" not in repr(clip)
    assert "user_101" not in repr(clip)


def test_clip_without_a_vod_offset_is_kept_and_flagged() -> None:
    _, result = fetch_recorded()

    by_id = {c.clip_id: c for c in result.clips}
    assert by_id["SyntheticClip0002"].vod_offset_s is None
    assert not by_id["SyntheticClip0002"].has_vod_offset
    assert by_id["SyntheticClip0001"].has_vod_offset
    assert result.summary.n_without_vod_offset == 1


def test_clip_without_a_vod_link_is_dropped_and_counted() -> None:
    _, result = fetch_recorded()

    assert "SyntheticClip0003" not in {c.clip_id for c in result.clips}
    assert result.summary.n_other_videos == 1
    assert result.summary.n_clips == 3


# --- Date ranges -----------------------------------------------------------------------------


def test_search_runs_from_the_broadcast_start_to_the_capture_time_in_6_hour_ranges() -> None:
    fake = FakeGetClips([])

    result = fetch_vod_clips(client_for(fake), make_vod(), salt=SALT, captured_at=CAPTURED_AT)

    assert fake.queries[0][0] == VOD_START
    assert fake.queries[-1][1] == CAPTURED_AT
    assert len(fake.queries) == 8 * 4  # 8 days of 6 h ranges
    for (_, end), (start, _) in zip(fake.queries, fake.queries[1:], strict=False):
        assert end == start  # consecutive, no gaps
    assert result.summary.search_range == TimeRange(VOD_START, CAPTURED_AT)
    assert len(result.summary.ranges_queried) == len(fake.queries)
    assert result.summary.complete
    assert result.clips == ()


def test_capture_time_defaults_to_now() -> None:
    fake = FakeGetClips([])

    fetch_vod_clips(
        client_for(fake), make_vod(), salt=SALT, clock=lambda: VOD_START + timedelta(hours=1)
    )

    assert fake.queries == [(VOD_START, VOD_START + timedelta(hours=1))]


def test_clips_of_other_vods_from_the_same_channel_are_dropped() -> None:
    fake = FakeGetClips(
        [
            clip_record(1, VOD_START + timedelta(minutes=5)),
            clip_record(2, VOD_START + timedelta(minutes=6), video_id="9100000002"),
            clip_record(3, VOD_START + timedelta(days=2)),  # clipped from the VOD later
        ]
    )

    result = fetch_vod_clips(client_for(fake), make_vod(), salt=SALT, captured_at=CAPTURED_AT)

    assert [c.clip_id for c in result.clips] == ["SyntheticClip00001", "SyntheticClip00003"]
    assert result.summary.n_other_videos == 1


def test_clip_on_a_range_boundary_is_returned_once() -> None:
    boundary = VOD_START + timedelta(hours=6)  # both neighbouring ranges include it
    fake = FakeGetClips([clip_record(1, boundary)])

    result = fetch_vod_clips(client_for(fake), make_vod(), salt=SALT, captured_at=CAPTURED_AT)

    assert [c.clip_id for c in result.clips] == ["SyntheticClip00001"]
    assert result.summary.n_duplicates == 1
    assert result.summary.n_clips == 1


def test_saturated_range_is_split_until_every_clip_is_found() -> None:
    # 1,500 clips in the first hour: one 6 h query would return only Twitch's ~1,000.
    clips = [clip_record(i, VOD_START + timedelta(seconds=2 * i), views=i) for i in range(1500)]
    fake = FakeGetClips(clips)

    result = fetch_vod_clips(
        client_for(fake), make_vod(), salt=SALT, captured_at=VOD_START + timedelta(hours=6)
    )

    assert result.summary.n_clips == 1500
    assert result.summary.complete
    assert result.summary.saturated_ranges == ()
    assert len(result.summary.ranges_queried) > 1
    first = result.summary.ranges_queried[0]
    assert first == TimeRange(VOD_START, VOD_START + timedelta(hours=6))  # queried, then split
    # Every range a split produced holds fewer clips than the saturation threshold.
    leaves = result.summary.ranges_queried[1:]
    for span in leaves:
        inside = [
            c for c in clips if span.started_at <= parse_time(str(c["created_at"])) <= span.ended_at
        ]
        assert len(inside) < SATURATION_RESULTS or span.length >= timedelta(minutes=2)


def test_range_still_saturated_at_the_minimum_length_is_reported() -> None:
    # 950 clips in the same second: no split can bring a range under the threshold.
    burst = VOD_START + timedelta(minutes=30)
    fake = FakeGetClips([clip_record(i, burst, views=i) for i in range(950)])

    result = fetch_vod_clips(
        client_for(fake), make_vod(), salt=SALT, captured_at=VOD_START + timedelta(hours=1)
    )

    assert not result.summary.complete
    # A burst on a split point lies in both halves (the filter includes both ends).
    assert result.summary.saturated_ranges
    for span in result.summary.saturated_ranges:
        assert span.started_at <= burst <= span.ended_at
        assert span.length < timedelta(minutes=2)
    assert result.summary.n_clips == 950  # what Twitch did return is kept


def test_range_returning_the_query_limit_counts_as_saturated() -> None:
    # Exactly at the threshold: the listing may have been cut off.
    burst = VOD_START + timedelta(seconds=10)
    fake = FakeGetClips([clip_record(i, burst) for i in range(SATURATION_RESULTS)])

    result = fetch_vod_clips(
        client_for(fake), make_vod(), salt=SALT, captured_at=VOD_START + timedelta(minutes=1)
    )

    assert len(result.summary.saturated_ranges) == 1


# --- Errors ----------------------------------------------------------------------------------


def test_clip_of_another_broadcaster_is_a_response_error() -> None:
    record = clip_record(1, VOD_START)
    record["broadcaster_id"] = "9000000002"

    with pytest.raises(TwitchResponseError, match="broadcaster_id"):
        fetch_vod_clips(
            client_for(Replay({"data": [record], "pagination": {}})),
            make_vod(),
            salt=SALT,
            captured_at=VOD_START + timedelta(hours=1),
        )


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("duration", "30"),
        ("duration", -1),
        ("vod_offset", "100"),
        ("is_featured", "no"),
        ("creator_id", ""),
    ],
)
def test_malformed_clip_field_is_a_response_error(field_name: str, value: object) -> None:
    record = clip_record(1, VOD_START)
    record[field_name] = value

    with pytest.raises(TwitchResponseError, match=field_name):
        fetch_vod_clips(
            client_for(Replay({"data": [record], "pagination": {}})),
            make_vod(),
            salt=SALT,
            captured_at=VOD_START + timedelta(hours=1),
        )


def test_missing_is_featured_is_none() -> None:
    record = clip_record(1, VOD_START)
    del record["is_featured"]

    result = fetch_vod_clips(
        client_for(Replay({"data": [record], "pagination": {}})),
        make_vod(),
        salt=SALT,
        captured_at=VOD_START + timedelta(hours=1),
    )

    assert result.clips[0].is_featured is None


@pytest.mark.parametrize(
    "captured_at",
    [VOD_START, VOD_START - timedelta(hours=1), datetime(2026, 10, 2, 0, 0)],  # noqa: DTZ001
    ids=["at_start", "before_start", "naive"],
)
def test_capture_time_must_be_aware_and_after_the_vod_start(captured_at: datetime) -> None:
    with pytest.raises(ValueError, match=r"timezone-aware|after"):
        fetch_vod_clips(
            client_for(FakeGetClips([])), make_vod(), salt=SALT, captured_at=captured_at
        )


def test_time_range_rejects_naive_and_reversed_bounds() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        TimeRange(datetime(2026, 10, 1), VOD_START)  # noqa: DTZ001
    with pytest.raises(ValueError, match="after"):
        TimeRange(VOD_START, VOD_START)
