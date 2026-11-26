"""Tests for channel VOD listing (GET /api/v1/channels/vods) and bulk job queuing."""

import json
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from autoclipper.api import mock
from autoclipper.api.app import create_app
from autoclipper.api.channels import _SharedHelix, list_channel_vods
from autoclipper.api.service import MockService, SampleMedia
from autoclipper.config import MissingSettingError, TwitchCredentials
from autoclipper.features.chat import load_lexicon
from autoclipper.ingest.twitch_api import HelixClient, RetryPolicy
from autoclipper.ingest.vod import IngestTools
from autoclipper.schemas import CreateJobRequest, parse_channel

API = "/api/v1"
FIXTURES = Path(__file__).parent.parent / "ingest" / "fixtures"
ZERO_RETRY = RetryPolicy(
    max_rate_limit_retries=0,
    max_transient_retries=0,
    backoff_base_s=0.0,
    backoff_max_s=0.0,
)
OFFLINE_STREAMS = {"data": []}


def _load_fixture(name: str) -> dict[str, object]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _fake_helix(*responses: httpx.Response | Exception) -> HelixClient:
    queue = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "id.twitch.tv":
            return httpx.Response(
                200,
                json={
                    "access_token": "fake-token",
                    "expires_in": 3600,
                    "token_type": "bearer",
                },
            )
        reply = queue.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    return HelixClient(
        TwitchCredentials(SecretStr("fake-client-id"), SecretStr("fake-client-secret")),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        clock=lambda: datetime(2026, 10, 21, 12, 0, tzinfo=UTC),
        sleep=lambda _: None,
        retry_policy=ZERO_RETRY,
    )


@pytest.fixture
def samples(tmp_path: Path) -> SampleMedia:
    path = tmp_path / "samples" / "preview.mp4"
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(b"preview.mp4" * 100)
    return SampleMedia(path)


# --- Channel parsing ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("user_001", "user_001"),
        ("  User_001  ", "user_001"),
        ("@User_001", "user_001"),
        ("https://www.twitch.tv/user_001", "user_001"),
        ("http://twitch.tv/User_001/", "user_001"),
        ("twitch.tv/user_001", "user_001"),
        ("m.twitch.tv/User_001/videos?filter=archives&sort=time", "user_001"),
        ("https://www.twitch.tv/user_001/about#bio", "user_001"),
        ("https://www.twitch.tv/user_001/home", "user_001"),
        ("https://www.twitch.tv/user_001/schedule", "user_001"),
        ("https://www.twitch.tv/user_001/clips", "user_001"),
    ],
)
def test_parse_channel_accepts_logins_and_urls(raw: str, expected: str) -> None:
    assert parse_channel(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "9000000001",
        "https://www.twitch.tv/videos/9000000001",
        "twitch.tv/videos/9000000001",
        "twitch.tv/videos",
        "https://www.twitch.tv/directory",
        "https://clips.twitch.tv/SomeClipSlug",
        "https://www.twitch.tv/user_001/clip/SomeClipSlug",
        "https://www.youtube.com/@user_001",
        "bad name",
        "a" * 26,
        "_leading_underscore",
        "twitch.tv/9000000001",
    ],
)
def test_parse_channel_rejects_invalid_inputs(raw: str) -> None:
    assert parse_channel(raw) is None


# --- Helix-backed endpoint ---------------------------------------------------------------------


def test_list_channel_vods_paginates_and_marks_analyzed(
    tmp_path: Path, samples: SampleMedia
) -> None:
    service = MockService(samples=samples, output_dir=tmp_path, step_s=0.0, synchronous=True)
    # Pre-seed an on-disk analysis JSON for one VOD and finish an in-memory job for another.
    analysis_dir = tmp_path / "analysis"
    analysis_dir.mkdir(parents=True, exist_ok=True)
    (analysis_dir / "9100000002.json").write_text("{}", encoding="utf-8")
    done_job, _ = service.create_job(CreateJobRequest(vod="9100000003"))

    page1 = _load_fixture("helix_videos_archive_page1.json")
    page2 = _load_fixture("helix_videos_archive_page2.json")
    client = TestClient(
        create_app(
            service,
            helix_factory=lambda: _fake_helix(
                httpx.Response(200, json=_load_fixture("helix_users.json")),
                httpx.Response(200, json=OFFLINE_STREAMS),
                httpx.Response(200, json=page1),
                httpx.Response(200, json=page2),
            ),
        )
    )

    response = client.get(
        f"{API}/channels/vods",
        params={"channel": "https://www.twitch.tv/User_001/videos?filter=archives", "limit": 10},
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["channel"] == {
        "broadcaster_id": "9000000001",
        "login": "user_001",
        "display_name": "User_001",
        "profile_image_url": "https://example.invalid/profile.png",
    }
    assert body["truncated"] is False
    assert [v["video_id"] for v in body["vods"]] == ["9100000003", "9100000002", "9100000001"]
    first, second, third = body["vods"]
    assert first["thumbnail_url"] == "https://example.invalid/thumb-320x180.jpg"
    assert first["url"] == "https://www.twitch.tv/videos/9100000003"
    assert first["duration_s"] == 2700.0
    assert first["analyzed"] is True
    assert first["last_job_id"] == done_job.job_id
    # On-disk analysis JSON marks analyzed=True even without an in-memory job.
    assert second["analyzed"] is True
    assert second["last_job_id"] is None
    assert third["analyzed"] is False
    assert third["last_job_id"] is None


def test_list_channel_vods_excludes_in_progress_live_broadcast(
    tmp_path: Path, samples: SampleMedia
) -> None:
    """An active live broadcast's in-progress archive is excluded before applying limit."""
    service = MockService(samples=samples, output_dir=tmp_path, step_s=0.0, synchronous=True)
    # 9100000003 has stream_id 9200000003 in helix_videos_archive_page1.json.
    live_stream = {"data": [{"id": "9200000003", "user_id": "9000000001", "type": "live"}]}
    client = TestClient(
        create_app(
            service,
            helix_factory=lambda: _fake_helix(
                httpx.Response(200, json=_load_fixture("helix_users.json")),
                httpx.Response(200, json=live_stream),
                httpx.Response(200, json=_load_fixture("helix_videos_archive_page1.json")),
            ),
        )
    )

    response = client.get(f"{API}/channels/vods", params={"channel": "user_001", "limit": 1})

    assert response.status_code == 200
    body = response.json()
    # The in-progress broadcast (9100000003) is skipped, so the latest completed VOD appears.
    assert [v["video_id"] for v in body["vods"]] == ["9100000002"]
    assert body["truncated"] is True


def test_list_channel_vods_reads_done_jobs_after_twitch_lookup(
    tmp_path: Path, samples: SampleMedia
) -> None:
    """A job that finishes while the Twitch lookup is in flight is reflected in the response."""
    service = MockService(samples=samples, output_dir=tmp_path, step_s=0.0, synchronous=True)
    users = _load_fixture("helix_users.json")
    videos = _load_fixture("helix_videos_archive_page1.json")
    videos["pagination"] = {}
    finished_job_id: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "id.twitch.tv":
            return httpx.Response(
                200, json={"access_token": "tok", "expires_in": 3600, "token_type": "bearer"}
            )
        if request.url.path == "/helix/users":
            return httpx.Response(200, json=users)
        if request.url.path == "/helix/streams":
            return httpx.Response(200, json=OFFLINE_STREAMS)
        if request.url.path == "/helix/videos":
            job, _ = service.create_job(CreateJobRequest(vod="9100000003"))
            finished_job_id.append(job.job_id)
            return httpx.Response(200, json=videos)
        raise AssertionError(request.url.path)

    client = TestClient(
        create_app(
            service,
            helix_factory=lambda: HelixClient(
                TwitchCredentials(SecretStr("id"), SecretStr("secret")),
                http_client=httpx.Client(transport=httpx.MockTransport(handler)),
                clock=lambda: datetime(2026, 10, 21, 12, 0, tzinfo=UTC),
                sleep=lambda _: None,
                retry_policy=ZERO_RETRY,
            ),
        )
    )

    body = client.get(f"{API}/channels/vods", params={"channel": "user_001"}).json()
    assert len(finished_job_id) == 1
    assert body["vods"][0]["video_id"] == "9100000003"
    assert body["vods"][0]["analyzed"] is True
    assert body["vods"][0]["last_job_id"] == finished_job_id[0]


def test_shared_helix_reuses_app_token_across_lookups(tmp_path: Path, samples: SampleMedia) -> None:
    """Session-cached HelixClient fetches one OAuth token across repeated channel lookups."""
    token_requests = 0
    users = _load_fixture("helix_users.json")

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_requests
        if request.url.host == "id.twitch.tv":
            token_requests += 1
            return httpx.Response(
                200, json={"access_token": "cached-tok", "expires_in": 3600, "token_type": "bearer"}
            )
        if request.url.path == "/helix/users":
            return httpx.Response(200, json=users)
        if request.url.path == "/helix/streams":
            return httpx.Response(200, json=OFFLINE_STREAMS)
        if request.url.path == "/helix/videos":
            return httpx.Response(200, json={"data": [], "pagination": {}})
        raise AssertionError(request.url.path)

    http = httpx.Client(transport=httpx.MockTransport(handler))
    creds = TwitchCredentials(SecretStr("id"), SecretStr("secret"))
    shared = _SharedHelix(
        credentials_provider=lambda: creds,
        client_builder=lambda c: HelixClient(
            c,
            http_client=http,
            clock=lambda: datetime(2026, 10, 21, 12, 0, tzinfo=UTC),
            sleep=lambda _: None,
            retry_policy=ZERO_RETRY,
        ),
    )
    service = MockService(
        samples=samples,
        output_dir=tmp_path,
        step_s=0.0,
        synchronous=True,
        job_runner="pipeline",
        data_dir=tmp_path / "data",
        artifacts_dir=tmp_path,
        lexicon=load_lexicon(Path(__file__).resolve().parents[2] / "configs" / "lexicon.yaml"),
        tools_for=lambda: IngestTools(),
        segment_fetcher=lambda *_: (_ for _ in ()).throw(AssertionError("unused")),
    )
    try:
        first = list_channel_vods(service, "user_001", shared_helix=shared)
        second = list_channel_vods(service, "user_001", shared_helix=shared)
        assert first.channel.login == "user_001"
        assert second.channel.login == "user_001"
        assert token_requests == 1
    finally:
        shared.close()
        http.close()


def test_list_channel_vods_respects_limit_and_reports_truncated(
    tmp_path: Path, samples: SampleMedia
) -> None:
    service = MockService(samples=samples, output_dir=tmp_path, step_s=0.0, synchronous=True)
    client = TestClient(
        create_app(
            service,
            helix_factory=lambda: _fake_helix(
                httpx.Response(200, json=_load_fixture("helix_users.json")),
                httpx.Response(200, json=OFFLINE_STREAMS),
                httpx.Response(200, json=_load_fixture("helix_videos_archive_page1.json")),
            ),
        )
    )

    response = client.get(f"{API}/channels/vods", params={"channel": "user_001", "limit": 1})

    assert response.status_code == 200
    body = response.json()
    assert [v["video_id"] for v in body["vods"]] == ["9100000003"]
    assert body["truncated"] is True


def test_list_channel_vods_empty_channel_returns_200_empty_list(
    tmp_path: Path, samples: SampleMedia
) -> None:
    service = MockService(samples=samples, output_dir=tmp_path, step_s=0.0, synchronous=True)
    client = TestClient(
        create_app(
            service,
            helix_factory=lambda: _fake_helix(
                httpx.Response(200, json=_load_fixture("helix_users.json")),
                httpx.Response(200, json=OFFLINE_STREAMS),
                httpx.Response(200, json={"data": [], "pagination": {}}),
            ),
        )
    )

    response = client.get(f"{API}/channels/vods", params={"channel": "user_001"})

    assert response.status_code == 200
    body = response.json()
    assert body["channel"]["login"] == "user_001"
    assert body["vods"] == []
    assert body["truncated"] is False


def test_list_channel_vods_missing_credentials_returns_503(
    tmp_path: Path, samples: SampleMedia
) -> None:
    service = MockService(samples=samples, output_dir=tmp_path, step_s=0.0, synchronous=True)

    def missing_creds() -> HelixClient:
        raise MissingSettingError(
            ["TWITCH_CLIENT_ID", "TWITCH_CLIENT_SECRET"],
            needed_for="calling the Twitch API",
            docs="docs/development/setup.md",
        )

    client = TestClient(create_app(service, helix_factory=missing_creds))

    response = client.get(f"{API}/channels/vods", params={"channel": "user_001"})

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "twitch_credentials_missing"
    assert response.json()["error"]["details"] == {
        "missing": ["TWITCH_CLIENT_ID", "TWITCH_CLIENT_SECRET"]
    }


def test_list_channel_vods_unknown_channel_returns_404(
    tmp_path: Path, samples: SampleMedia
) -> None:
    service = MockService(samples=samples, output_dir=tmp_path, step_s=0.0, synchronous=True)
    client = TestClient(
        create_app(
            service,
            helix_factory=lambda: _fake_helix(httpx.Response(200, json={"data": []})),
        )
    )

    response = client.get(f"{API}/channels/vods", params={"channel": "nobody_here"})

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "channel_not_found"


def test_list_channel_vods_maps_twitch_auth_rate_limit_and_server_errors(
    tmp_path: Path, samples: SampleMedia
) -> None:
    service = MockService(samples=samples, output_dir=tmp_path, step_s=0.0, synchronous=True)

    auth_client = TestClient(
        create_app(
            service,
            helix_factory=lambda: _fake_helix(
                httpx.Response(401, json={"status": 401, "message": "Invalid OAuth token"}),
                httpx.Response(401, json={"status": 401, "message": "Invalid OAuth token"}),
            ),
        )
    )
    auth_resp = auth_client.get(f"{API}/channels/vods", params={"channel": "user_001"})
    assert auth_resp.status_code == 502
    assert auth_resp.json()["error"]["code"] == "twitch_auth_failed"

    rate_client = TestClient(
        create_app(
            service,
            helix_factory=lambda: _fake_helix(
                httpx.Response(429, json={"status": 429, "message": "Too many requests"})
            ),
        )
    )
    rate_resp = rate_client.get(f"{API}/channels/vods", params={"channel": "user_001"})
    assert rate_resp.status_code == 503
    assert rate_resp.json()["error"]["code"] == "upstream_unavailable"

    err_client = TestClient(
        create_app(
            service,
            helix_factory=lambda: _fake_helix(
                httpx.Response(503, json={"status": 503, "message": "Service unavailable"})
            ),
        )
    )
    err_resp = err_client.get(f"{API}/channels/vods", params={"channel": "user_001"})
    assert err_resp.status_code == 502
    assert err_resp.json()["error"]["code"] == "upstream_unavailable"


@pytest.mark.parametrize(
    ("channel_param", "expected_hint"),
    [
        ("https://www.twitch.tv/videos/9000000001", "VOD"),
        ("9000000001", "VOD"),
        ("https://youtube.com/@streamer", "channel"),
        ("   ", "channel"),
    ],
)
def test_list_channel_vods_rejects_invalid_channel_input(
    tmp_path: Path, samples: SampleMedia, channel_param: str, expected_hint: str
) -> None:
    service = MockService(samples=samples, output_dir=tmp_path, step_s=0.0, synchronous=True)
    client = TestClient(create_app(service))

    response = client.get(f"{API}/channels/vods", params={"channel": channel_param})

    assert response.status_code == 422
    err = response.json()["error"]
    assert err["code"] == "invalid_channel"
    assert expected_hint in err["message"]


def test_list_channel_vods_validates_query_parameters(tmp_path: Path, samples: SampleMedia) -> None:
    service = MockService(samples=samples, output_dir=tmp_path, step_s=0.0, synchronous=True)
    client = TestClient(create_app(service))

    missing = client.get(f"{API}/channels/vods")
    assert missing.status_code == 422
    assert missing.json()["error"]["code"] == "invalid_request"

    too_big = client.get(f"{API}/channels/vods", params={"channel": "user_001", "limit": 101})
    assert too_big.status_code == 422
    assert too_big.json()["error"]["code"] == "invalid_request"

    zero = client.get(f"{API}/channels/vods", params={"channel": "user_001", "limit": 0})
    assert zero.status_code == 422
    assert zero.json()["error"]["code"] == "invalid_request"

    non_int = client.get(f"{API}/channels/vods", params={"channel": "user_001", "limit": "1.0"})
    assert non_int.status_code == 422
    assert non_int.json()["error"]["code"] == "invalid_request"

    unknown_param = client.get(f"{API}/channels/vods", params={"channel": "user_001", "lmit": 1})
    assert unknown_param.status_code == 422
    assert unknown_param.json()["error"]["code"] == "invalid_request"
    assert "query.lmit" in unknown_param.json()["error"]["details"]["fields"]


# --- Mock mode scenarios -----------------------------------------------------------------------


def test_mock_channel_vods_scenarios_and_done_job_tracking(
    tmp_path: Path, samples: SampleMedia
) -> None:
    service = MockService(samples=samples, output_dir=tmp_path, step_s=0.0, synchronous=True)
    client = TestClient(create_app(service))

    normal = client.get(f"{API}/channels/vods", params={"channel": "twitch.tv/User_001"})
    assert normal.status_code == 200
    body = normal.json()
    assert body["channel"]["login"] == "user_001"
    assert body["channel"]["display_name"] == "User_001"
    assert body["truncated"] is False
    assert len(body["vods"]) == 5
    by_id = {v["video_id"]: v for v in body["vods"]}
    assert by_id["9000000001"]["analyzed"] is False
    assert by_id["9000000002"]["analyzed"] is True

    # Analyzing 9000000001 marks it analyzed, records last_job_id, and matches result metadata.
    created = client.post(f"{API}/jobs", json={"vod": by_id["9000000001"]["url"]})
    assert created.status_code == 201
    job_id = created.json()["job_id"]
    result_vod = client.get(f"{API}/jobs/{job_id}/result").json()["vod"]
    assert by_id["9000000001"]["title"] == result_vod["title"]
    assert by_id["9000000001"]["duration_s"] == result_vod["duration_s"]
    assert by_id["9000000001"]["created_at"] == result_vod["created_at"]

    after = client.get(f"{API}/channels/vods", params={"channel": "user_001"}).json()
    by_id_after = {v["video_id"]: v for v in after["vods"]}
    assert by_id_after["9000000001"]["analyzed"] is True
    assert by_id_after["9000000001"]["last_job_id"] == job_id

    # Truncation when limit is smaller than available mock VODs.
    limited = client.get(f"{API}/channels/vods", params={"channel": "user_001", "limit": 2}).json()
    assert len(limited["vods"]) == 2
    assert limited["truncated"] is True

    # Special mock channels cover empty, unknown, missing-credentials and Twitch-error states.
    empty = client.get(f"{API}/channels/vods", params={"channel": "empty_channel"})
    assert empty.status_code == 200
    assert empty.json()["vods"] == []

    unknown = client.get(f"{API}/channels/vods", params={"channel": "unknown_channel"})
    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == "channel_not_found"

    no_creds = client.get(f"{API}/channels/vods", params={"channel": "no_credentials"})
    assert no_creds.status_code == 503
    assert no_creds.json()["error"]["code"] == "twitch_credentials_missing"

    twitch_err = client.get(f"{API}/channels/vods", params={"channel": "twitch_error"})
    assert twitch_err.status_code == 502
    assert twitch_err.json()["error"]["code"] == "upstream_unavailable"


# --- Bulk job queuing concurrency guarantee ----------------------------------------------------


def test_bulk_queued_analysis_jobs_run_sequentially_fifo(
    tmp_path: Path, samples: SampleMedia
) -> None:
    """Queuing multiple VODs at once executes strictly one analysis job at a time in FIFO order."""
    active = 0
    max_active = 0
    order: list[str] = []
    lock = threading.Lock()
    done_all = threading.Event()

    def slow_analyze(
        video_id: str,
        **kwargs: object,
    ) -> object:
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
            order.append(video_id)
        try:
            time.sleep(0.03)
            scenario = mock.scenario_for(video_id)
            assert scenario.result is not None
            return scenario.result
        finally:
            with lock:
                active -= 1
                if len(order) == 3 and active == 0:
                    done_all.set()

    service = MockService(
        samples=samples,
        output_dir=tmp_path,
        step_s=0.0,
        synchronous=False,
        job_runner="pipeline",
        data_dir=tmp_path / "data",
        artifacts_dir=tmp_path,
        lexicon=load_lexicon(Path(__file__).resolve().parents[2] / "configs" / "lexicon.yaml"),
        tools_for=lambda: IngestTools(),
        segment_fetcher=lambda *_: (_ for _ in ()).throw(AssertionError("unused")),
        analyze=slow_analyze,  # type: ignore[arg-type]
    )
    try:
        client = TestClient(create_app(service))
        vod_ids = ["9000000001", "9000000002", "9000000003"]
        job_ids = []
        for vod_id in vod_ids:
            resp = client.post(f"{API}/jobs", json={"vod": vod_id})
            assert resp.status_code == 201
            job_ids.append(resp.json()["job_id"])

        assert done_all.wait(timeout=5.0)
        states: list[str] = []
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            states = [client.get(f"{API}/jobs/{jid}").json()["state"] for jid in job_ids]
            if all(state == "done" for state in states):
                break
            time.sleep(0.01)
        assert states == ["done", "done", "done"]
        assert max_active == 1
        assert order == vod_ids
    finally:
        service.close()
