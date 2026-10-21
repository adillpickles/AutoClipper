"""Tests for the Helix client, against scripted responses (httpx.MockTransport, no network)."""

import copy
import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import httpx
import pytest
from pydantic import SecretStr

from autoclipper.config import TwitchCredentials
from autoclipper.ingest.twitch_api import (
    TOKEN_EXPIRY_MARGIN_S,
    HelixClient,
    MutedSegment,
    RetryPolicy,
    TwitchAPIError,
    TwitchAuthError,
    TwitchNotFoundError,
    TwitchRateLimitError,
    TwitchRequestError,
    TwitchResponseError,
    TwitchUnavailableError,
    parse_duration_s,
)

FIXTURES = Path(__file__).parent / "fixtures"
CLIENT_ID = "fake-client-id-0001"
CLIENT_SECRET = "fake-client-secret-0001"
TOKEN_LIFETIME_S = 3600
START = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)

type Reply = httpx.Response | Exception


def load(name: str) -> dict[str, object]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def ok(body: dict[str, object], **headers: str) -> httpx.Response:
    return httpx.Response(200, json=body, headers=headers)


def error(status: int, message: str = "error", **headers: str) -> httpx.Response:
    return httpx.Response(status, json={"status": status, "message": message}, headers=headers)


def ratelimit(remaining: int, reset: datetime) -> dict[str, str]:
    return {
        "Ratelimit-Limit": "800",
        "Ratelimit-Remaining": str(remaining),
        "Ratelimit-Reset": str(int(reset.timestamp())),
    }


class FakeClock:
    """A clock that only moves when the client sleeps (or a test advances it)."""

    def __init__(self) -> None:
        self.now = START
        self.sleeps: list[float] = []

    def __call__(self) -> datetime:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += timedelta(seconds=seconds)


class FakeTwitch:
    """MockTransport handler: issues numbered tokens and replays scripted Helix replies."""

    def __init__(self, *helix: Reply) -> None:
        self.helix = list(helix)
        self.token_replies: list[Reply] = []
        self.requests: list[httpx.Request] = []
        self.tokens_issued = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host == "id.twitch.tv":
            reply = self.token_replies.pop(0) if self.token_replies else None
            if reply is None:
                self.tokens_issued += 1
                reply = ok(
                    {
                        "access_token": f"fake-app-token-{self.tokens_issued}",
                        "expires_in": TOKEN_LIFETIME_S,
                        "token_type": "bearer",
                    }
                )
        else:
            reply = self.helix.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    @property
    def helix_requests(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.host == "api.twitch.tv"]

    @property
    def token_requests(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.host == "id.twitch.tv"]


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def make_client(clock: FakeClock) -> Callable[..., tuple[HelixClient, FakeTwitch]]:
    def make(*helix: Reply, policy: RetryPolicy | None = None) -> tuple[HelixClient, FakeTwitch]:
        fake = FakeTwitch(*helix)
        credentials = TwitchCredentials(SecretStr(CLIENT_ID), SecretStr(CLIENT_SECRET))
        http = httpx.Client(transport=httpx.MockTransport(fake))
        client = HelixClient(
            credentials, http_client=http, clock=clock, sleep=clock.sleep, retry_policy=policy
        )
        return client, fake

    return make


MakeClient = Callable[..., tuple[HelixClient, FakeTwitch]]


# --- Get Users -------------------------------------------------------------------------------


def test_get_users_parses_broadcasters_in_requested_order(
    make_client: MakeClient, clock: FakeClock
) -> None:
    client, fake = make_client(ok(load("helix_users.json")))

    users = client.get_users(["User_002", "user_001", "user_002"])

    assert [u.broadcaster_id for u in users] == ["9000000002", "9000000001"]
    assert users[1].login == "user_001"
    assert users[1].display_name == "User_001"
    assert [u.broadcaster_type for u in users] == ["", "partner"]
    assert all(isinstance(u.broadcaster_id, str) for u in users)
    assert users[0].fetched_at == clock.now
    assert fake.helix_requests[0].url.params.get_list("login") == ["user_002", "user_001"]


def test_get_users_sends_client_id_and_bearer_token(make_client: MakeClient) -> None:
    client, fake = make_client(ok(load("helix_users.json")))

    client.get_users(["user_001", "user_002"])

    headers = fake.helix_requests[0].headers
    assert headers["Client-Id"] == CLIENT_ID
    assert headers["Authorization"] == "Bearer fake-app-token-1"


def test_get_users_raises_not_found_listing_missing_logins(make_client: MakeClient) -> None:
    client, _ = make_client(ok(load("helix_users.json")))

    with pytest.raises(TwitchNotFoundError) as caught:
        client.get_users(["user_001", "user_003"])

    assert caught.value.missing == ("user_003",)
    assert caught.value.user_error


def test_get_users_splits_more_than_100_logins_into_batches(make_client: MakeClient) -> None:
    logins = [f"user_{i:03d}" for i in range(150)]
    pages = [
        ok({"data": [_user(login) for login in logins[:100]]}),
        ok({"data": [_user(login) for login in logins[100:]]}),
    ]
    client, fake = make_client(*pages)

    users = client.get_users(logins)

    assert [len(r.url.params.get_list("login")) for r in fake.helix_requests] == [100, 50]
    assert [u.login for u in users] == logins


@pytest.mark.parametrize("logins", ["user_001", [], ["user_001", "  "]])
def test_get_users_rejects_bad_login_arguments(
    make_client: MakeClient, logins: list[str] | str
) -> None:
    client, fake = make_client()

    with pytest.raises(ValueError, match="logins"):
        client.get_users(logins)
    assert fake.requests == []


def _user(login: str) -> dict[str, object]:
    return {"id": f"9{login[-3:]}", "login": login, "display_name": login, "broadcaster_type": ""}


# --- Get Videos ------------------------------------------------------------------------------


def test_get_videos_parses_vod_record(make_client: MakeClient, clock: FakeClock) -> None:
    client, fake = make_client(ok(load("helix_videos_by_id.json")))

    (vod,) = client.get_videos(["9100000001"])

    assert vod.video_id == "9100000001"
    assert vod.broadcaster_id == "9000000001"
    assert vod.stream_id == "9200000001"
    assert vod.title == "Synthetic stream one"
    assert vod.created_at == datetime(2026, 10, 1, 18, 0, 5, tzinfo=UTC)
    assert vod.created_at.tzinfo is UTC
    assert vod.duration_s == pytest.approx(3 * 3600 + 8 * 60 + 33)
    assert (vod.type, vod.language, vod.view_count) == ("archive", "en", 1234)
    assert vod.muted_segments == (MutedSegment(120.0, 30.0), MutedSegment(5400.0, 360.0))
    assert vod.game_ids is None
    assert vod.fetched_at == clock.now
    assert fake.helix_requests[0].url.params.get_list("id") == ["9100000001"]


def test_get_videos_raises_not_found_for_ids_twitch_ignored(make_client: MakeClient) -> None:
    client, _ = make_client(ok(load("helix_videos_by_id.json")))

    with pytest.raises(TwitchNotFoundError) as caught:
        client.get_videos(["9100000001", "9100000099"])

    assert caught.value.missing == ("9100000099",)


def test_get_videos_maps_404_to_not_found(make_client: MakeClient) -> None:
    client, fake = make_client(error(404, "Not Found"))

    with pytest.raises(TwitchNotFoundError) as caught:
        client.get_videos(["9100000099"])

    assert caught.value.missing == ("9100000099",)
    assert len(fake.helix_requests) == 1


def test_timestamps_with_offsets_are_converted_to_utc(make_client: MakeClient) -> None:
    body = _video_body(created_at="2026-10-01T20:00:05+02:00")
    client, _ = make_client(ok(body))

    (vod,) = client.get_videos(["9100000001"])

    assert vod.created_at == datetime(2026, 10, 1, 18, 0, 5, tzinfo=UTC)
    assert vod.created_at.utcoffset() == timedelta(0)


def _video_body(**changes: object) -> dict[str, object]:
    body = copy.deepcopy(load("helix_videos_by_id.json"))
    video = cast("list[dict[str, object]]", body["data"])[0]
    for key, value in changes.items():
        if value is _ABSENT:
            del video[key]
        else:
            video[key] = value
    return body


_ABSENT = object()


# --- Pagination ------------------------------------------------------------------------------


def test_archive_listing_follows_cursor_across_pages(make_client: MakeClient) -> None:
    client, fake = make_client(
        ok(load("helix_videos_archive_page1.json")), ok(load("helix_videos_archive_page2.json"))
    )

    listing = client.get_archive_videos("9000000001")

    assert [v.video_id for v in listing.items] == ["9100000003", "9100000002", "9100000001"]
    assert not listing.truncated
    first, second = (r.url.params for r in fake.helix_requests)
    assert (first["user_id"], first["type"], first["first"]) == ("9000000001", "archive", "100")
    assert "after" not in first
    assert second["after"] == "cursor-page-2"
    assert listing.items[0].muted_segments == ()  # null → no muted segments


def test_archive_listing_stops_at_max_results(make_client: MakeClient) -> None:
    client, fake = make_client(ok(load("helix_videos_archive_page1.json")))

    listing = client.get_archive_videos("9000000001", max_results=2)

    assert len(listing.items) == 2
    assert listing.truncated
    assert len(fake.helix_requests) == 1
    assert fake.helix_requests[0].url.params["first"] == "2"


def test_pages_are_trimmed_to_max_results(make_client: MakeClient) -> None:
    client, _ = make_client(ok(load("helix_videos_archive_page1.json")))

    listing = client.get_archive_videos("9000000001", max_results=1)

    assert [v.video_id for v in listing.items] == ["9100000003"]
    assert listing.truncated


def test_empty_page_ends_pagination_even_with_cursor(make_client: MakeClient) -> None:
    client, fake = make_client(
        ok(load("helix_videos_archive_page1.json")),
        ok({"data": [], "pagination": {"cursor": "cursor-page-3"}}),
    )

    listing = client.get_archive_videos("9000000001")

    assert len(listing.items) == 2
    assert not listing.truncated
    assert len(fake.helix_requests) == 2


def test_repeated_cursor_raises_instead_of_looping(make_client: MakeClient) -> None:
    page = load("helix_videos_archive_page1.json")
    client, _ = make_client(ok(page), ok(page))

    with pytest.raises(TwitchResponseError, match="same pagination cursor"):
        client.get_archive_videos("9000000001")


def test_archive_listing_rejects_non_archive_videos(make_client: MakeClient) -> None:
    body = _video_body(type="upload")
    client, _ = make_client(ok(body))

    with pytest.raises(TwitchResponseError, match="'type'"):
        client.get_archive_videos("9000000001")


@pytest.mark.parametrize(("max_results", "page_size"), [(0, 100), (10, 0), (10, 101)])
def test_get_pages_rejects_invalid_limits(
    make_client: MakeClient, max_results: int, page_size: int
) -> None:
    client, _ = make_client()

    with pytest.raises(ValueError, match="must be"):
        client.get_pages("/videos", [], max_results=max_results, page_size=page_size)


# --- Token -----------------------------------------------------------------------------------


def test_token_is_cached_between_requests(make_client: MakeClient) -> None:
    client, fake = make_client(ok(load("helix_users.json")), ok(load("helix_users.json")))

    client.get_users(["user_001"])
    client.get_users(["user_002"])

    assert len(fake.token_requests) == 1


@pytest.mark.parametrize(
    ("elapsed_s", "tokens"),
    [
        (TOKEN_LIFETIME_S - TOKEN_EXPIRY_MARGIN_S - 1, 1),
        (TOKEN_LIFETIME_S - TOKEN_EXPIRY_MARGIN_S, 2),
        (TOKEN_LIFETIME_S + 10, 2),
    ],
)
def test_token_is_replaced_shortly_before_it_expires(
    make_client: MakeClient, clock: FakeClock, elapsed_s: float, tokens: int
) -> None:
    client, fake = make_client(ok(load("helix_users.json")), ok(load("helix_users.json")))

    client.get_users(["user_001"])
    clock.now += timedelta(seconds=elapsed_s)
    client.get_users(["user_001"])

    assert fake.tokens_issued == tokens
    assert fake.helix_requests[1].headers["Authorization"] == f"Bearer fake-app-token-{tokens}"


def test_token_request_sends_credentials_in_form_body(make_client: MakeClient) -> None:
    client, fake = make_client(ok(load("helix_users.json")))

    client.get_users(["user_001"])

    (request,) = fake.token_requests
    assert request.method == "POST"
    assert str(request.url) == "https://id.twitch.tv/oauth2/token"
    form = httpx.QueryParams(request.content.decode())
    assert dict(form) == {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type": "client_credentials",
    }


def test_401_fetches_a_new_token_once_and_retries(make_client: MakeClient) -> None:
    client, fake = make_client(error(401, "Invalid OAuth token"), ok(load("helix_users.json")))

    users = client.get_users(["user_001"])

    assert users[0].login == "user_001"
    assert fake.tokens_issued == 2
    auth = [r.headers["Authorization"] for r in fake.helix_requests]
    assert auth == ["Bearer fake-app-token-1", "Bearer fake-app-token-2"]


def test_second_401_raises_auth_error_without_looping(make_client: MakeClient) -> None:
    client, fake = make_client(error(401), error(401), ok(load("helix_users.json")))

    with pytest.raises(TwitchAuthError) as caught:
        client.get_users(["user_001"])

    assert caught.value.status_code == 401
    assert caught.value.user_error
    assert (len(fake.helix_requests), len(fake.token_requests)) == (2, 2)


def test_rejected_credentials_raise_auth_error(make_client: MakeClient) -> None:
    client, fake = make_client()
    fake.token_replies = [error(403, "invalid client secret")]

    with pytest.raises(TwitchAuthError, match="TWITCH_CLIENT_SECRET") as caught:
        client.get_users(["user_001"])

    assert caught.value.status_code == 403
    assert fake.helix_requests == []


@pytest.mark.parametrize(
    "body",
    [
        {"expires_in": 3600},
        {"access_token": "", "expires_in": 3600},
        {"access_token": "fake-app-token-x"},
        {"access_token": "fake-app-token-x", "expires_in": 0},
        {"access_token": "fake-app-token-x", "expires_in": "3600"},
    ],
)
def test_malformed_token_response_raises(make_client: MakeClient, body: dict[str, object]) -> None:
    client, fake = make_client()
    fake.token_replies = [ok(body)]

    with pytest.raises(TwitchResponseError, match="oauth2/token"):
        client.get_users(["user_001"])


# --- Rate limits and retries -----------------------------------------------------------------


def test_429_waits_until_ratelimit_reset(make_client: MakeClient, clock: FakeClock) -> None:
    reset = START + timedelta(seconds=7)
    client, fake = make_client(
        error(429, "Too Many Requests", **ratelimit(0, reset)), ok(load("helix_users.json"))
    )

    client.get_users(["user_001"])

    assert clock.sleeps == [7.0]
    assert len(fake.helix_requests) == 2


@pytest.mark.parametrize(
    ("headers", "expected_wait_s"),
    [
        (ratelimit(0, START - timedelta(seconds=30)), 1.0),  # reset already past: clock skew
        (ratelimit(0, START + timedelta(days=30)), 60.0),  # absurd reset: capped
        ({}, 1.0),  # no headers: backoff
        ({"Ratelimit-Reset": "soon"}, 1.0),  # malformed headers: backoff
    ],
)
def test_429_wait_is_bounded(
    make_client: MakeClient, clock: FakeClock, headers: dict[str, str], expected_wait_s: float
) -> None:
    client, _ = make_client(error(429, **headers), ok(load("helix_users.json")))

    client.get_users(["user_001"])

    assert clock.sleeps == [expected_wait_s]


def test_429_retries_are_capped(make_client: MakeClient, clock: FakeClock) -> None:
    replies = [error(429, **ratelimit(0, START + timedelta(seconds=5)))] * 3
    client, fake = make_client(*replies, policy=RetryPolicy(max_rate_limit_retries=2))

    with pytest.raises(TwitchRateLimitError) as caught:
        client.get_users(["user_001"])

    assert not caught.value.user_error
    assert len(fake.helix_requests) == 3
    assert len(clock.sleeps) == 2


def test_empty_bucket_waits_for_reset_before_next_request(
    make_client: MakeClient, clock: FakeClock
) -> None:
    reset = START + timedelta(seconds=12)
    client, _ = make_client(
        ok(load("helix_users.json"), **ratelimit(0, reset)), ok(load("helix_users.json"))
    )

    client.get_users(["user_001"])
    assert clock.sleeps == []
    assert client.rate_limit is not None
    assert client.rate_limit.remaining == 0
    client.get_users(["user_001"])

    assert clock.sleeps == [12.0]


def test_5xx_is_retried_with_backoff(make_client: MakeClient, clock: FakeClock) -> None:
    client, fake = make_client(error(503), error(500), ok(load("helix_users.json")))

    client.get_users(["user_001"])

    assert clock.sleeps == [1.0, 2.0]
    assert len(fake.helix_requests) == 3


def test_5xx_retries_are_capped(make_client: MakeClient, clock: FakeClock) -> None:
    client, fake = make_client(error(502), error(502), error(502), ok(load("helix_users.json")))

    with pytest.raises(TwitchUnavailableError, match="status 502") as caught:
        client.get_users(["user_001"])

    assert not caught.value.user_error
    assert len(fake.helix_requests) == 3
    assert clock.sleeps == [1.0, 2.0]


def test_transport_errors_are_retried_then_raised(
    make_client: MakeClient, clock: FakeClock
) -> None:
    request = httpx.Request("GET", "https://api.twitch.tv/helix/users")
    failures = [httpx.ConnectTimeout("timed out", request=request)] * 2
    client, _ = make_client(*failures, policy=RetryPolicy(max_transient_retries=1))

    with pytest.raises(TwitchUnavailableError, match="ConnectTimeout") as caught:
        client.get_users(["user_001"])

    assert isinstance(caught.value.__cause__, httpx.ConnectTimeout)
    assert clock.sleeps == [1.0]


@pytest.mark.parametrize("status", [400, 403, 422])
def test_other_4xx_raise_request_error_without_retry(
    make_client: MakeClient, clock: FakeClock, status: int
) -> None:
    client, fake = make_client(error(status, "The value in the id query parameter is not valid"))

    with pytest.raises(TwitchRequestError, match="id query parameter") as caught:
        client.get_videos(["not-a-video"])

    assert caught.value.status_code == status
    assert len(fake.helix_requests) == 1
    assert clock.sleeps == []


def test_invalid_json_raises_response_error(make_client: MakeClient) -> None:
    client, _ = make_client(httpx.Response(200, text="<html>oops</html>"))

    with pytest.raises(TwitchResponseError, match="not valid JSON"):
        client.get_users(["user_001"])


def test_retry_policy_rejects_negative_values() -> None:
    with pytest.raises(ValueError, match="retry counts"):
        RetryPolicy(max_transient_retries=-1)
    with pytest.raises(ValueError, match="waits"):
        RetryPolicy(max_rate_limit_wait_s=float("inf"))


# --- Field parsing ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "seconds"),
    [
        ("3h8m33s", 11313.0),
        ("45m", 2700.0),
        ("59s", 59.0),
        ("1h0m0s", 3600.0),
        ("3m21s", 201.0),
        ("0s", 0.0),
        ("2h", 7200.0),
        ("1h5s", 3605.0),
        ("90m", 5400.0),
        ("125s", 125.0),
        ("48h0m0s", 172800.0),
    ],
)
def test_parse_duration_s_accepts_twitch_durations(text: str, seconds: float) -> None:
    assert parse_duration_s(text) == pytest.approx(seconds)


@pytest.mark.parametrize(
    "text",
    ["", "3h8m33", "8m3h", "1.5s", "-1s", "+1s", "PT3M21S", "3h 8m", " 59s", "1h60m", "1m60s"],
)
def test_parse_duration_s_rejects_malformed_text(text: str) -> None:
    with pytest.raises(ValueError, match=r"duration|below 60"):
        parse_duration_s(text)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("title", _ABSENT, "field 'title' is missing"),
        ("duration", "three hours", "field 'duration' is not a duration"),
        ("created_at", "2026-10-01T18:00:05", "field 'created_at' is not an RFC 3339"),
        ("view_count", "1234", "field 'view_count' is str"),
        ("view_count", True, "field 'view_count' is bool"),
        ("user_id", 9000000001, "field 'user_id' is int"),
        ("stream_id", _ABSENT, "field 'stream_id' is missing"),
        ("muted_segments", _ABSENT, "field 'muted_segments' is missing"),
        ("muted_segments", {"offset": 1}, "field 'muted_segments' is dict"),
        ("muted_segments", [{"offset": 1}], "field 'muted_segments[0].duration' is missing"),
        ("muted_segments", [{"offset": -1, "duration": 3}], "muted_segments[0].offset' is neg"),
        ("muted_segments", ["x"], "field 'muted_segments[0]' is not an object"),
    ],
)
def test_invalid_video_fields_raise_errors_naming_the_field(
    make_client: MakeClient, field: str, value: object, message: str
) -> None:
    client, _ = make_client(ok(_video_body(**{field: value})))

    with pytest.raises(TwitchResponseError, match=message.replace("[", r"\[")) as caught:
        client.get_videos(["9100000001"])

    assert "id=9100000001" in str(caught.value)
    if isinstance(value, str):
        assert value not in str(caught.value)


def test_null_stream_id_and_muted_segments_are_accepted(make_client: MakeClient) -> None:
    client, _ = make_client(ok(_video_body(stream_id=None, muted_segments=None)))

    (vod,) = client.get_videos(["9100000001"])

    assert vod.stream_id is None
    assert vod.muted_segments == ()


def test_naive_clock_is_rejected(make_client: MakeClient, clock: FakeClock) -> None:
    client, _ = make_client(ok(load("helix_users.json")))
    clock.now = datetime(2026, 10, 21, 12, 0)  # noqa: DTZ001 - the point of the test

    with pytest.raises(ValueError, match="timezone-aware"):
        client.get_users(["user_001"])


# --- Secrets ---------------------------------------------------------------------------------


def test_secrets_never_appear_in_logs_errors_or_repr(
    make_client: MakeClient, caplog: pytest.LogCaptureFixture
) -> None:
    echo = f"bad token fake-app-token-2 for {CLIENT_ID} / {CLIENT_SECRET}"
    client, _ = make_client(error(401), error(401, echo))
    caplog.set_level(logging.DEBUG)

    with pytest.raises(TwitchAuthError) as caught:
        client.get_users(["user_001"])

    exposed = [caplog.text, str(caught.value), repr(caught.value), repr(client)]
    for secret in (CLIENT_ID, CLIENT_SECRET, "fake-app-token-1", "fake-app-token-2"):
        assert not any(secret in text for text in exposed), secret
    assert "[redacted]" in str(caught.value)
    assert "retrying" not in caplog.text
    assert "status=401 action=refetch" in caplog.text


def test_error_classes_say_whether_the_user_can_fix_them() -> None:
    user_errors = {cls.__name__: cls.user_error for cls in _all_subclasses(TwitchAPIError)}

    assert user_errors == {
        "TwitchAuthError": True,
        "TwitchRequestError": True,
        "TwitchNotFoundError": True,
        "TwitchRateLimitError": False,
        "TwitchUnavailableError": False,
        "TwitchResponseError": False,
    }


def _all_subclasses(cls: type[TwitchAPIError]) -> list[type[TwitchAPIError]]:
    return [sub for direct in cls.__subclasses__() for sub in [direct, *_all_subclasses(direct)]]
