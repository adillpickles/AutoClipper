"""Tests for streaming VOD video uploads (``POST /api/v1/uploads``) and local file checks."""

import asyncio
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import BinaryIO

import pytest
from fastapi.testclient import TestClient
from starlette.types import Message, Receive, Scope, Send

from autoclipper.api.app import API_PREFIX, create_app
from autoclipper.api.service import MockService, SampleMedia
from autoclipper.ffmpeg import ffmpeg_command, run_command

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
        chunk_size = kwargs.pop("chunk_size", None)
        options.update(kwargs)
        service = MockService(**options)  # type: ignore[arg-type]
        app = create_app(service, cors_origins=cors_origins)  # type: ignore[arg-type]

        async def dispatch(scope: Scope, receive: Receive, send: Send) -> None:
            if not isinstance(chunk_size, int) or scope.get("path") != f"{API}/uploads":
                await app(scope, receive, send)
                return
            # TestClient joins HTTP request iterators. Deliver their data as successive ASGI
            # events so the endpoint exercises its streaming loop with the real video.
            body = (await receive())["body"]
            chunks = _iter_chunks(body, chunk_size=chunk_size)
            finished = False

            async def chunked_receive() -> Message:
                nonlocal finished
                if finished:
                    return await receive()
                chunk = next(chunks, b"")
                finished = not chunk
                return {"type": "http.request", "body": chunk, "more_body": not finished}

            await app(scope, chunked_receive, send)

        client = TestClient(dispatch, raise_server_exceptions=False)
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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, service = make_upload_client(probe_video=None, chunk_size=2048)
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
    assert sorted(path.suffix for path in service.uploads_dir.iterdir()) == [".json", ".mp4"]

    # Attaching upload_id to a job works even for 9000000007 and never leaks server paths.
    created = client.post(
        f"{API}/jobs",
        json={"vod": "9000000007", "upload_id": body["upload_id"]},
    )
    assert created.status_code == 201, created.text
    job = created.json()
    assert job["video_path"] is None
    assert str(tmp_path) not in created.text
    listed = client.get(f"{API}/jobs").json()["items"]
    assert [summary["video_path"] for summary in listed] == [None]

    media_url = f"{API}/jobs/{job['job_id']}/candidates/c001/media"
    stored = service.uploads_dir / f"{body['upload_id']}.mp4"
    moved = tmp_path / "moved.mp4"
    prepare = MockService._prepare_file_media

    def prepare_while_missing(service: MockService, job_id: str) -> None:
        stored.rename(moved)
        prepare(service, job_id)

    with monkeypatch.context() as patch:
        patch.setattr(MockService, "_prepare_file_media", prepare_while_missing)
        assert client.get(media_url).json()["preview"]["reason"] == "local_file_missing"
    moved.rename(stored)

    media = client.get(media_url)
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
    cover_art = tmp_path / "cover.jpg"
    run_command(
        ffmpeg_command("-f", "lavfi", "-i", "color=size=32x32", "-frames:v", "1", cover_art),
        timeout_s=30.0,
    )
    cover_only = tmp_path / "cover-only.mp4"
    run_command(
        ffmpeg_command(
            "-i",
            audio_only,
            "-i",
            cover_art,
            "-map",
            "0:a",
            "-map",
            "1:v",
            "-c:v",
            "copy",
            "-disposition:v",
            "attached_pic",
            cover_only,
        ),
        timeout_s=30.0,
    )
    cover_response = client.post(
        f"{API}/uploads?filename=cover.mp4", content=cover_only.read_bytes()
    )

    for resp in (bad_ext, bad_bytes, cover_response):
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
    unknown_query = client.post(f"{API}/uploads?filename=vod.mp4&typo=1", content=b"abc")
    assert unknown_query.status_code == 422
    assert unknown_query.json()["error"]["code"] == "invalid_request"
    assert unknown_query.json()["error"]["details"] == {"fields": ["typo"]}
    assert list(service.uploads_dir.glob("*")) == []
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


@pytest.mark.parametrize("during_probe", [False, True])
def test_upload_cleans_up_partial_file_on_client_disconnect(
    tmp_path: Path, samples: SampleMedia, during_probe: bool
) -> None:
    async def simulate_disconnect() -> list[Message]:
        loop = asyncio.get_running_loop()
        probe_started = asyncio.Event()
        disconnected = threading.Event()

        def probe_video(path: Path) -> bool:
            assert during_probe
            assert path.read_bytes() == b"first-chunk-"
            loop.call_soon_threadsafe(probe_started.set)
            assert disconnected.wait(timeout=5.0)
            return True

        service = MockService(
            samples=samples,
            output_dir=tmp_path / "artifacts",
            step_s=0.0,
            synchronous=True,
            probe_video=probe_video,
        )
        sent_body = False
        sent: list[Message] = []

        async def receive() -> Message:
            nonlocal sent_body
            if not sent_body:
                sent_body = True
                return {
                    "type": "http.request",
                    "body": b"first-chunk-",
                    "more_body": not during_probe,
                }
            if during_probe:
                await probe_started.wait()
                disconnected.set()
            return {"type": "http.disconnect"}

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
        try:
            await create_app(service)(scope, receive, send)
            assert probe_started.is_set() == during_probe
            assert list(service.uploads_dir.iterdir()) == []
        finally:
            service.close()
        return sent

    sent = asyncio.run(simulate_disconnect())
    assert sent[0]["status"] == 499


@pytest.mark.parametrize("fail_open", [False, True])
def test_upload_reports_disk_io_failure_and_cleans_up(
    make_upload_client: Callable[..., tuple[TestClient, MockService]],
    monkeypatch: pytest.MonkeyPatch,
    fail_open: bool,
) -> None:
    client, service = make_upload_client()
    real_open = Path.open

    class FailingClose:
        def __init__(self, handle: BinaryIO) -> None:
            self.handle = handle
            self.closed = False

        def write(self, chunk: bytes) -> int:
            return self.handle.write(chunk)

        def close(self) -> None:
            self.handle.close()
            raise OSError("simulated close failure")

    def open_file(path: Path, mode: str = "r", *args: object, **kwargs: object) -> object:
        if path.parent == service.uploads_dir and mode == "xb":
            if fail_open:
                raise OSError("simulated open failure")
            return FailingClose(real_open(path, "xb"))
        return real_open(path, mode, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "open", open_file)
    response = client.post(f"{API}/uploads?filename=vod.mp4", content=b"video-bytes")
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "upload_failed"
    assert list(service.uploads_dir.iterdir()) == []


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
