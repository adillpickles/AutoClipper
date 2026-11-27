"""Tests for streaming VOD video uploads (``POST /api/v1/uploads``) and local file checks."""

import asyncio
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.types import Message, Scope

from autoclipper.api.app import API_PREFIX, create_app
from autoclipper.api.service import MockService, SampleMedia

API = API_PREFIX


@pytest.fixture
def samples(tmp_path: Path) -> SampleMedia:
    path = tmp_path / "samples" / "preview.mp4"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"preview.mp4" * 200)
    return SampleMedia(path)


@pytest.fixture
def make_upload_client(
    tmp_path: Path, samples: SampleMedia
) -> Iterator[Callable[..., tuple[TestClient, MockService]]]:
    clients: list[TestClient] = []

    def make(**kwargs: object) -> tuple[TestClient, MockService]:
        options: dict[str, object] = {
            "samples": samples,
            "output_dir": tmp_path / "artifacts",
            "step_s": 0.0,
            "synchronous": True,
            "probe_video": lambda path: path.suffix == ".mp4",
        }
        cors_origins = kwargs.pop("cors_origins", ("http://localhost:3000",))
        options.update(kwargs)
        service = MockService(**options)  # type: ignore[arg-type]
        client = TestClient(
            create_app(service, cors_origins=cors_origins),  # type: ignore[arg-type]
            raise_server_exceptions=False,
        )
        client.__enter__()
        clients.append(client)
        return client, service

    yield make
    for client in clients:
        client.__exit__(None, None, None)


def _iter_chunks(data: bytes, chunk_size: int = 4096) -> Iterator[bytes]:
    for start in range(0, len(data), chunk_size):
        yield data[start : start + chunk_size]


@pytest.mark.ffmpeg
def test_streaming_chunked_upload_creates_job_and_serves_media(
    make_upload_client: Callable[..., tuple[TestClient, MockService]],
    make_test_media: Callable[..., Path],
    tmp_path: Path,
) -> None:
    client, service = make_upload_client(probe_video=None)
    video = make_test_media("stream_vod.mp4", duration_s=3.0)
    raw = video.read_bytes()

    response = client.post(
        f"{API}/uploads?filename=my%20stream%20vod.mp4",
        content=_iter_chunks(raw, chunk_size=2048),
        headers={"Content-Type": "application/octet-stream"},
    )

    assert response.status_code == 201, response.text
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["upload_id"].startswith("upl_")
    assert body["filename"] == "my stream vod.mp4"
    assert body["size_bytes"] == len(raw)
    assert body["duration_s"] == pytest.approx(3.0, abs=0.25)
    assert str(tmp_path) not in response.text
    assert list(service.uploads_dir.glob("*.part")) == []

    # Attaching upload_id to a job works even for 9000000007 and never leaks server paths.
    created = client.post(
        f"{API}/jobs",
        json={"vod": "9000000007", "upload_id": body["upload_id"]},
    )
    assert created.status_code == 201, created.text
    job = created.json()
    assert job["video_path"] is None
    assert str(tmp_path) not in created.text

    media = client.get(f"{API}/jobs/{job['job_id']}/candidates/c001/media")
    assert media.status_code == 200
    assert media.json()["preview"]["status"] == "available"
    assert media.json()["export"]["status"] == "available"


@pytest.mark.ffmpeg
def test_streaming_upload_rejects_corrupt_video_and_cleans_up_files(
    make_upload_client: Callable[..., tuple[TestClient, MockService]],
    make_test_media: Callable[..., Path],
    tmp_path: Path,
) -> None:
    client, service = make_upload_client(probe_video=None)
    audio_only = make_test_media("tone.wav", video_size=None)

    bad_ext = client.post(
        f"{API}/uploads?filename=tone.wav",
        content=_iter_chunks(audio_only.read_bytes(), chunk_size=1024),
    )
    bad_bytes = client.post(
        f"{API}/uploads?filename=broken.mp4",
        content=_iter_chunks(b"not-a-valid-mp4-stream" * 200, chunk_size=512),
    )

    for resp in (bad_ext, bad_bytes):
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "invalid_video_upload"
        assert str(tmp_path) not in resp.text

    assert list(service.uploads_dir.glob("*")) == []


def test_upload_enforces_origin_allowlist(
    make_upload_client: Callable[..., tuple[TestClient, MockService]],
) -> None:
    client, service = make_upload_client(cors_origins=("http://localhost:3000",))

    blocked = client.post(
        f"{API}/uploads?filename=vod.mp4",
        content=b"video-bytes",
        headers={"Origin": "https://evil.example"},
    )
    allowed = client.post(
        f"{API}/uploads?filename=vod.mp4",
        content=b"video-bytes",
        headers={"Origin": "http://localhost:3000"},
    )

    assert blocked.status_code == 403
    assert blocked.json()["error"]["code"] == "origin_not_allowed"
    assert allowed.status_code == 201
    assert len(list(service.uploads_dir.glob("*.mp4"))) == 1


def test_upload_sanitizes_filename_and_rejects_bad_inputs(
    make_upload_client: Callable[..., tuple[TestClient, MockService]],
) -> None:
    client, service = make_upload_client()

    missing_name = client.post(f"{API}/uploads", content=b"abc")
    bad_ext = client.post(f"{API}/uploads?filename=notes.txt", content=b"abc")
    empty_body = client.post(f"{API}/uploads?filename=vod.mp4", content=b"")
    win_path_name = client.post(
        f"{API}/uploads?filename=C:\\Users\\Stream\\Videos\\vod.mp4",
        content=b"abc",
    )

    assert (missing_name.status_code, missing_name.json()["error"]["code"]) == (
        422,
        "invalid_request",
    )
    assert (bad_ext.status_code, bad_ext.json()["error"]["code"]) == (
        422,
        "invalid_video_upload",
    )
    assert (empty_body.status_code, empty_body.json()["error"]["code"]) == (
        422,
        "invalid_video_upload",
    )
    assert win_path_name.status_code == 201
    assert win_path_name.json()["filename"] == "vod.mp4"
    assert len(list(service.uploads_dir.glob("*.mp4"))) == 1


def test_upload_cleans_up_partial_file_on_truncated_content_length(
    make_upload_client: Callable[..., tuple[TestClient, MockService]],
) -> None:
    client, service = make_upload_client()

    response = client.post(
        f"{API}/uploads?filename=vod.mp4",
        content=b"partial-bytes",
        headers={"Content-Length": "10000"},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_video_upload"
    assert list(service.uploads_dir.glob("*")) == []


def test_upload_cleans_up_partial_file_on_client_disconnect(
    tmp_path: Path, samples: SampleMedia
) -> None:
    service = MockService(
        samples=samples,
        output_dir=tmp_path / "artifacts",
        step_s=0.0,
        synchronous=True,
        probe_video=lambda path: path.suffix == ".mp4",
    )
    app = create_app(service)

    async def simulate_disconnect() -> list[Message]:
        events = iter(
            [
                {"type": "http.request", "body": b"first-chunk-", "more_body": True},
                {"type": "http.disconnect"},
            ]
        )
        sent: list[Message] = []

        async def receive() -> Message:
            return next(events)

        async def send(message: Message) -> None:
            sent.append(message)

        scope: Scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": f"{API}/uploads",
            "raw_path": f"{API}/uploads".encode("ascii"),
            "query_string": b"filename=vod.mp4",
            "headers": [(b"content-type", b"application/octet-stream")],
            "client": ("127.0.0.1", 12345),
            "server": ("127.0.0.1", 8000),
        }
        await app(scope, receive, send)
        return sent

    sent = asyncio.run(simulate_disconnect())
    assert sent[0]["status"] == 499
    assert list(service.uploads_dir.glob("*")) == []


def test_job_upload_id_validation_dedup_and_restart_persistence(
    make_upload_client: Callable[..., tuple[TestClient, MockService]],
    tmp_path: Path,
    samples: SampleMedia,
) -> None:
    client, _ = make_upload_client(synchronous=False, step_s=0.05)
    uploaded = client.post(f"{API}/uploads?filename=vod.mp4", content=b"video-bytes").json()
    upload_id = uploaded["upload_id"]

    local_file = tmp_path / "local.mp4"
    local_file.write_bytes(b"video-bytes")

    both = client.post(
        f"{API}/jobs",
        json={"vod": "9000000001", "video_path": str(local_file), "upload_id": upload_id},
    )
    assert both.status_code == 422
    assert both.json()["error"]["code"] == "invalid_request"
    assert set(both.json()["error"]["details"]["fields"]) == {"video_path", "upload_id"}

    missing_upload = client.post(
        f"{API}/jobs",
        json={"vod": "9000000001", "upload_id": "upl_0000000000000000"},
    )
    assert missing_upload.status_code == 422
    assert missing_upload.json()["error"]["code"] == "invalid_video_upload"

    traversal = client.post(
        f"{API}/jobs",
        json={"vod": "9000000001", "upload_id": "../secret.mp4"},
    )
    assert traversal.status_code == 422
    assert traversal.json()["error"]["code"] == "invalid_video_upload"

    first = client.post(f"{API}/jobs", json={"vod": "9000000007", "upload_id": upload_id})
    second = client.post(f"{API}/jobs", json={"vod": "9000000007", "upload_id": upload_id})
    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json()["job_id"] == first.json()["job_id"]
    job_id = first.json()["job_id"]

    # Wait for synchronous-equivalent completion before testing restart persistence.
    import time

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if client.get(f"{API}/jobs/{job_id}").json()["state"] == "done":
            break
        time.sleep(0.02)
    assert client.get(f"{API}/jobs/{job_id}").json()["state"] == "done"
    client.__exit__(None, None, None)

    restarted = MockService(
        samples=samples,
        output_dir=tmp_path / "artifacts",
        step_s=0.0,
        synchronous=True,
        probe_video=lambda path: path.suffix == ".mp4",
    )
    try:
        with TestClient(create_app(restarted)) as restarted_client:
            job_after = restarted_client.get(f"{API}/jobs/{job_id}")
            assert job_after.status_code == 200
            assert job_after.json()["video_path"] is None
            media = restarted_client.get(f"{API}/jobs/{job_id}/candidates/c001/media")
            assert media.status_code == 200
            # VOD 9000000007 has no downloaded segment; export only works if upload_id persisted.
            assert media.json()["export"]["status"] == "available"
    finally:
        restarted.close()


def test_local_video_path_rejects_bad_extensions_and_never_leaks_paths(
    make_upload_client: Callable[..., tuple[TestClient, MockService]],
    tmp_path: Path,
) -> None:
    client, _ = make_upload_client(probe_video=lambda _path: True)
    bad_ext = tmp_path / "secret_dir_name" / "script.exe"
    bad_ext.parent.mkdir(parents=True, exist_ok=True)
    bad_ext.write_bytes(b"mz")
    empty_mp4 = tmp_path / "secret_dir_name" / "empty.mp4"
    empty_mp4.write_bytes(b"")

    for bad_path in (bad_ext, empty_mp4, tmp_path / "secret_dir_name"):
        resp = client.post(
            f"{API}/jobs",
            json={"vod": "9000000001", "video_path": str(bad_path)},
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "invalid_video_path"
        assert "secret_dir_name" not in resp.text
        assert str(tmp_path) not in resp.text
