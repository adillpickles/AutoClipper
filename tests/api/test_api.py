"""Tests for the mock backend against docs/architecture/api-contract.md (FastAPI test client)."""

import asyncio
import contextlib
import inspect
import io
import itertools
import json
import os
import shutil
import threading
import time
import zipfile
from collections.abc import Callable, Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import httpx2
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from autoclipper.analysis import AnalysisResult, AnalysisWarning
from autoclipper.analyze import AnalyzeError
from autoclipper.api import mock
from autoclipper.api.app import create_app
from autoclipper.api.service import MockService, SampleMedia, ensure_sample_media
from autoclipper.config import TwitchCredentials
from autoclipper.export.captions import CaptioningError, Stage, Transcriber, Word
from autoclipper.export.clip import ClipSpec, IntervalOutsideMediaError, SourceMissingError
from autoclipper.export.cut import IntervalTooShortError
from autoclipper.export.errors import ExportError
from autoclipper.export.highlights import (
    HighlightsRequest,
    HighlightsResult,
    Segment,
    render_highlights,
)
from autoclipper.export.highlights import (
    Stage as HighlightsStage,
)
from autoclipper.features.chat import Lexicon, load_lexicon
from autoclipper.ffmpeg import FFmpegExitError, ffmpeg_command, probe, run_command
from autoclipper.ingest.chat import PINNED_TOOL_VERSION, CommandResult
from autoclipper.ingest.media import (
    DownloadedMedia,
    DownloadRequest,
    VideoSegment,
    VodAccessRestrictedError,
)
from autoclipper.ingest.twitch_api import HelixClient
from autoclipper.ingest.vod import IngestTools

API = "/api/v1"


@pytest.fixture
def samples(tmp_path: Path) -> SampleMedia:
    """A stand-in sample file: the routes only serve bytes, they never decode them."""
    path = tmp_path / "samples" / "preview.mp4"
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(b"preview.mp4" * 1000)
    return SampleMedia(path)


class FakeRenderer:
    """Stands in for `render_clip`: records each spec, reports the stages, writes a file."""

    def __init__(self, error: Exception | None = None, *, delay_s: float = 0.0) -> None:
        self.error = error
        self.delay_s = delay_s
        self.specs: list[ClipSpec] = []
        self.stages: list[Stage] = []
        self.regions: list[object] = []
        self.on_stage_seen: Callable[[ClipSpec], None] | None = None

    def __call__(
        self,
        spec: ClipSpec,
        /,
        *,
        region: tuple[float, float, float, float] | None,
        transcriber: Transcriber | None,
        on_stage: Callable[[Stage], None],
    ) -> object:
        self.specs.append(spec)
        self.regions.append(region)
        stages: list[Stage] = ["transcribing", "rendering"] if spec.captions else ["rendering"]
        for stage in stages:
            on_stage(stage)
            self.stages.append(stage)
            time.sleep(self.delay_s)
            if self.on_stage_seen is not None:
                self.on_stage_seen(spec)
        if self.error is not None:
            raise self.error
        spec.output.parent.mkdir(parents=True, exist_ok=True)
        spec.output.write_bytes(b"rendered clip")
        return None


class FakeHighlightsRenderer:
    """Stands in for `render_highlights`: records each request, reports the stages, writes."""

    def __init__(
        self,
        error: Exception | None = None,
        *,
        delay_s: float = 0.0,
        duration_s: float | None = None,
    ) -> None:
        self.error = error
        self.delay_s = delay_s
        self.duration_s = duration_s
        self.requests: list[HighlightsRequest] = []
        self.stages: list[HighlightsStage] = []

    def __call__(
        self,
        request: HighlightsRequest,
        /,
        *,
        transcriber: Transcriber | None,
        on_stage: Callable[[HighlightsStage], None],
    ) -> HighlightsResult:
        del transcriber  # the fake writes no captions; the stages show whether they were asked
        self.requests.append(request)
        stages: list[HighlightsStage] = (
            ["obtaining_media", "transcribing", "rendering"]
            if request.captions
            else ["obtaining_media", "rendering"]
        )
        for stage in stages:
            on_stage(stage)
            self.stages.append(stage)
            time.sleep(self.delay_s)
        if self.error is not None:
            raise self.error
        request.output.parent.mkdir(parents=True, exist_ok=True)
        request.output.write_bytes(b"rendered highlights")
        return HighlightsResult(
            output=request.output,
            segments=tuple(request.segments),
            order=request.order,
            captions=request.captions,
            duration_s=self.duration_s
            if self.duration_s is not None
            else sum(segment.end_s - segment.start_s for segment in request.segments),
            width=1920,
            height=1080,
            video_codec="h264",
            audio_codec="aac",
        )


@pytest.fixture
def make_client(
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
            "render": FakeRenderer(),
            "highlights_render": FakeHighlightsRenderer(),
        }
        options.update(kwargs)
        service = MockService(**options)  # type: ignore[arg-type]
        client = TestClient(create_app(service), raise_server_exceptions=False)
        client.__enter__()
        clients.append(client)
        return client, service

    yield make
    for client in clients:
        client.__exit__(None, None, None)


@pytest.fixture
def client(make_client: Callable[..., tuple[TestClient, MockService]]) -> TestClient:
    return make_client()[0]


def done_job(client: TestClient, vod: str = "9000000001") -> dict[str, Any]:
    response = client.post(f"{API}/jobs", json={"vod": vod})
    assert response.status_code == 201, response.text
    job = client.get(f"{API}/jobs/{response.json()['job_id']}").json()
    assert job["state"] in ("done", "failed")
    return job


def error_code(response: object) -> str:
    return response.json()["error"]["code"]  # type: ignore[attr-defined]


# --- Health and jobs ---------------------------------------------------------------------------


def test_health_reports_the_mock_runner(
    make_client: Callable[..., tuple[TestClient, MockService]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("autoclipper.api.service.captions_available", lambda: False)
    client, _ = make_client()

    response = client.get(f"{API}/health")

    assert response.json() == {
        "status": "ok",
        "schema_version": "1.1",
        "job_runner": "mock",
        "captions_available": False,
    }


def test_health_reports_captions_available_from_package_or_transcriber(
    make_client: Callable[..., tuple[TestClient, MockService]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("autoclipper.api.service.captions_available", lambda: True)
    client_pkg, _ = make_client()
    assert client_pkg.get(f"{API}/health").json()["captions_available"] is True

    monkeypatch.setattr("autoclipper.api.service.captions_available", lambda: False)
    client_custom, _ = make_client(transcriber=lambda _wav: [Word("hi", 0.0, 0.4)])
    assert client_custom.get(f"{API}/health").json()["captions_available"] is True

    client_override, _ = make_client(captions_supported=True)
    assert client_override.get(f"{API}/health").json()["captions_available"] is True


def test_preflight_returns_readiness_checks_with_no_store(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TWITCH_CLIENT_ID", "secret-id-xyz")
    monkeypatch.setenv("TWITCH_CLIENT_SECRET", "secret-val-xyz")
    monkeypatch.setenv("AUTOCLIPPER_CHAT_HASH_SALT", "secret-salt-xyz")

    response = client.get(f"{API}/preflight")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json; charset=utf-8"
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["status"] in ("ok", "warn", "fail")
    assert [c["name"] for c in body["checks"]] == [
        "ffmpeg",
        "twitch_downloader",
        "chat_hash_salt",
        "captions",
        "twitch_credentials",
    ]
    for check in body["checks"]:
        assert set(check) == {"name", "label", "status", "message", "fix", "source"}
        if check["status"] == "ok":
            assert check["fix"] is None
        else:
            assert isinstance(check["fix"], str)
            assert check["fix"]
    for secret in ("secret-id-xyz", "secret-val-xyz", "secret-salt-xyz"):
        assert secret not in response.text


def test_preflight_uses_pipeline_severity_and_accepts_custom_provider(
    tmp_path: Path, samples: SampleMedia
) -> None:
    from autoclipper.schemas import PreflightCheck, PreflightReport

    service = MockService(
        samples=samples,
        output_dir=tmp_path / "artifacts",
        step_s=0.0,
        synchronous=True,
        job_runner="pipeline",
        data_dir=tmp_path / "data",
        artifacts_dir=tmp_path / "artifacts",
        lexicon=load_lexicon(LEXICON_PATH),
        tools_for=IngestTools,
        segment_fetcher=unused_fetcher,
    )
    default_app = create_app(service)
    with TestClient(default_app) as pipeline_http:
        report = pipeline_http.get(f"{API}/preflight").json()
        by_name = {c["name"]: c["status"] for c in report["checks"]}
        assert by_name["twitch_credentials"] == "fail"
        assert by_name["chat_hash_salt"] == "warn"

    custom = PreflightReport(
        status="warn",
        checks=(
            PreflightCheck(
                name="captions",
                label="Captions extra",
                status="warn",
                message="Captions unavailable.",
                fix="uv sync --extra captions",
            ),
        ),
    )
    custom_service = MockService(
        samples=samples,
        output_dir=tmp_path / "artifacts",
        step_s=0.0,
        synchronous=True,
    )
    with TestClient(create_app(custom_service, preflight=lambda: custom)) as custom_http:
        response = custom_http.get(f"{API}/preflight")
        assert response.status_code == 200
        assert response.json() == json.loads(custom.model_dump_json())


def test_a_job_from_start_to_finish(client: TestClient) -> None:
    created = client.post(f"{API}/jobs", json={"vod": "https://www.twitch.tv/videos/9000000001"})

    assert created.status_code == 201
    job = created.json()
    assert created.headers["location"] == f"{API}/jobs/{job['job_id']}"
    assert created.headers["cache-control"] == "no-store"
    assert job["state"] == "queued"
    assert job["video_id"] == "9000000001"
    assert job["progress"] is None
    assert job["created_at"].endswith("Z")
    assert set(job) == {
        "job_id", "video_id", "video_path", "state", "progress", "warnings", "error",
        "created_at", "started_at", "updated_at", "finished_at",
    }  # fmt: skip
    assert job["job_id"].startswith("job_")
    assert len(job["job_id"]) == 4 + 16

    status = client.get(f"{API}/jobs/{job['job_id']}")
    assert status.headers["cache-control"] == "no-store"
    finished = status.json()
    assert finished["state"] == "done"
    assert finished["progress"] is None
    assert finished["finished_at"] is not None

    result = client.get(f"{API}/jobs/{job['job_id']}/result").json()
    assert result["schema_version"] == "1.1"
    assert result["vod"]["video_id"] == "9000000001"
    assert result["scorer"] == {"kind": "mock", "name": "mock", "run_id": None}
    assert [c["source"] for c in result["candidates"]] == ["both", "discovered", "viewer_clips"]
    assert result["candidates"][0]["top_signals"] == ["chat_msg_rate_z", "audio_rms_max_z"]
    assert result["candidates"][0]["title"] == "Loud audio & laughing chat"
    assert "3 viewer clips" in result["candidates"][0]["reason"]
    assert len(result["candidates"][0]["signal_details"]) >= 3
    assert result["n_viewer_clips_total"] == 4


@pytest.mark.parametrize(
    "vod",
    ["9000000001", "twitch.tv/videos/9000000001", "HTTP://M.TWITCH.TV/videos/9000000001/?t=1h"],
)
def test_vod_links_and_bare_ids_are_accepted(client: TestClient, vod: str) -> None:
    response = client.post(f"{API}/jobs", json={"vod": f"  {vod} "})

    assert response.status_code == 201
    assert response.json()["video_id"] == "9000000001"


@pytest.mark.parametrize(
    "vod", ["twitch.tv/user_001", "v9000000001", "twitch.tv/videos/", "clips.twitch.tv/x", ""]
)
def test_other_links_are_invalid_vod_links(client: TestClient, vod: str) -> None:
    response = client.post(f"{API}/jobs", json={"vod": vod})

    assert response.status_code == 422
    assert error_code(response) == "invalid_vod_link"


def test_a_video_path_must_be_an_existing_video(client: TestClient, tmp_path: Path) -> None:
    video = tmp_path / "vod.mp4"
    video.write_bytes(b"x")

    ok = client.post(f"{API}/jobs", json={"vod": "9000000001", "video_path": str(video)})
    relative = client.post(f"{API}/jobs", json={"vod": "9000000001", "video_path": "vod.mp4"})
    missing = client.post(
        f"{API}/jobs", json={"vod": "9000000001", "video_path": str(tmp_path / "nope.mp4")}
    )
    not_video = tmp_path / "notes.txt"
    not_video.write_text("x")
    other = client.post(f"{API}/jobs", json={"vod": "9000000001", "video_path": str(not_video)})

    assert ok.status_code == 201
    assert ok.json()["video_path"] == str(video)
    for response in (relative, missing, other):
        assert response.status_code == 422
        assert error_code(response) == "invalid_video_path"


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({}, "vod"),
        ({"vod": 9000000001}, "vod"),
        ({"vod": "9000000001", "extra": 1}, "extra"),
        ({"vod": "9000000001", "video_path": 3}, "video_path"),
    ],
)
def test_malformed_job_requests_are_invalid_requests(
    client: TestClient, body: dict[str, object], field: str
) -> None:
    response = client.post(f"{API}/jobs", json=body)

    assert response.status_code == 422
    assert error_code(response) == "invalid_request"
    assert field in response.json()["error"]["details"]["fields"]


def test_a_body_that_isnt_json_is_an_invalid_request(client: TestClient) -> None:
    response = client.post(
        f"{API}/jobs", content=b"{not json", headers={"Content-Type": "application/json"}
    )

    assert response.status_code == 422
    assert error_code(response) == "invalid_request"


def test_scenario_9000000002_has_no_viewer_clips(client: TestClient) -> None:
    job = done_job(client, "9000000002")

    result = client.get(f"{API}/jobs/{job['job_id']}/result").json()

    assert result["n_viewer_clips_total"] == 0
    assert result["candidates"] == [
        {
            "candidate_id": "c001",
            "rank": 1,
            "score": 1.8,
            "start_s": 12.0,
            "end_s": 40.0,
            "source": "discovered",
            "n_viewer_clips": 0,
            "top_signals": ["chat_msg_rate_z"],
            "title": "Hype chat surge",
            "reason": "Chat jumped to 4\u00d7 its usual rate with lots of hype reactions",
            "signal_details": [
                {
                    "kind": "chat_rate",
                    "label": "Chat rate",
                    "detail": "4\u00d7 usual stream rate (z=2.4)",
                },
                {
                    "kind": "chat_hype",
                    "label": "Chat reaction",
                    "detail": "High hype-reaction activity",
                },
            ],
        }
    ]


def test_scenario_9000000003_is_done_with_a_chat_missing_warning(client: TestClient) -> None:
    job = done_job(client, "9000000003")

    result = client.get(f"{API}/jobs/{job['job_id']}/result").json()

    assert [w["code"] for w in job["warnings"]] == ["chat_missing"]  # type: ignore[union-attr]
    assert result["warnings"] == job["warnings"]
    assert result["vod"]["duration_s"] == 300.0
    assert [c["source"] for c in result["candidates"]] == ["discovered", "viewer_clips"]
    # Chat is missing on 9000000003, so no candidate should report chat signals.
    for cand in result["candidates"]:
        assert not any(sig.startswith("chat_") for sig in cand["top_signals"])
        assert not any(detail["kind"].startswith("chat_") for detail in cand["signal_details"])


def test_scenario_9000000004_fails_with_vod_unavailable(client: TestClient) -> None:
    job = done_job(client, "9000000004")

    assert job["state"] == "failed"
    assert job["error"]["code"] == "vod_unavailable"  # type: ignore[index]
    assert job["progress"]["stage"] == "resolving_vod"  # type: ignore[index]
    result = client.get(f"{API}/jobs/{job['job_id']}/result")
    assert result.status_code == 409
    assert result.json()["error"]["details"] == {"state": "failed"}


def test_scenario_9000000005_fails_with_no_usable_signals(client: TestClient) -> None:
    job = done_job(client, "9000000005")

    assert job["error"]["code"] == "no_usable_signals"  # type: ignore[index]
    assert [w["code"] for w in job["warnings"]] == ["chat_missing", "audio_missing"]  # type: ignore[union-attr]
    assert job["progress"] == {
        "stage": "downloading_audio",
        "stage_index": 4,
        "stage_count": 6,
        "stage_fraction": None,
        "message": "Downloading audio",
    }


def test_scenario_9000000006_has_zero_candidates(client: TestClient) -> None:
    job = done_job(client, "9000000006")

    result = client.get(f"{API}/jobs/{job['job_id']}/result").json()

    assert result["candidates"] == []
    assert result["n_viewer_clips_total"] == 0


def test_unknown_job_and_path_and_method_use_the_error_envelope(client: TestClient) -> None:
    missing = client.get(f"{API}/jobs/job_0000000000000000")
    unknown = client.get(f"{API}/nothing")
    method = client.delete(f"{API}/health")

    assert (missing.status_code, error_code(missing)) == (404, "job_not_found")
    assert (unknown.status_code, error_code(unknown)) == (404, "not_found")
    assert (method.status_code, error_code(method)) == (405, "method_not_allowed")
    assert missing.json()["error"]["details"] is None


def test_an_unexpected_error_is_a_500_internal_error(
    make_client: Callable[..., tuple[TestClient, MockService]], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, service = make_client()

    def broken(job_id: str) -> object:
        raise RuntimeError("boom")

    monkeypatch.setattr(service, "get_job", broken)
    response = client.get(f"{API}/jobs/job_x")

    assert response.status_code == 500
    assert response.json() == {
        "error": {
            "code": "internal_error",
            "message": "Something went wrong on the server. Try again.",
            "details": None,
        }
    }


# --- Background worker -------------------------------------------------------------------------


def wait_until(check: Callable[[], bool], timeout_s: float = 10.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not check():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.01)


def test_the_worker_runs_queued_jobs_in_order_and_dedupes_running_ones(
    make_client: Callable[..., tuple[TestClient, MockService]],
) -> None:
    client, _ = make_client(synchronous=False, step_s=0.05)

    first = client.post(f"{API}/jobs", json={"vod": "9000000001"})
    again = client.post(f"{API}/jobs", json={"vod": "twitch.tv/videos/9000000001"})
    second = client.post(f"{API}/jobs", json={"vod": "9000000002"})
    job_id = first.json()["job_id"]

    assert first.status_code == 201
    assert again.status_code == 200
    assert again.json()["job_id"] == job_id
    early = client.get(f"{API}/jobs/{job_id}/result")
    assert early.status_code == 409
    assert error_code(early) == "job_not_done"
    states: set[str] = set()

    def finished() -> bool:
        job = client.get(f"{API}/jobs/{job_id}").json()
        states.add(job["state"])
        if job["state"] == "running":
            assert job["progress"]["stage_count"] == 6
        return job["state"] == "done"

    wait_until(finished)
    assert "running" in states
    wait_until(
        lambda: client.get(f"{API}/jobs/{second.json()['job_id']}").json()["state"] == "done"
    )
    # A finished job is never reused.
    rerun = client.post(f"{API}/jobs", json={"vod": "9000000001"})
    assert rerun.status_code == 201
    assert rerun.json()["job_id"] != job_id


# --- Media -------------------------------------------------------------------------------------


def test_candidate_media_is_the_sample_video_with_full_bounds(client: TestClient) -> None:
    job = done_job(client)

    media = client.get(f"{API}/jobs/{job['job_id']}/candidates/c001/media").json()

    assert media["video_id"] == "9000000001"
    preview = media["preview"]
    assert preview["status"] == "available"
    assert preview["kind"] == "video_file"
    assert (preview["media_start_s"], preview["media_end_s"]) == (0.0, 360.0)
    assert media["export"] == {
        "status": "available",
        "reason": None,
        "message": None,
        "min_start_s": 0.0,
        "max_end_s": 360.0,
        "max_duration_s": 90.0,
        "layouts": ["center_crop"],  # configured_region needs AUTOCLIPPER_EXPORT_REGION
    }
    file = client.get(preview["url"])
    assert file.status_code == 200
    assert file.headers["content-type"] == "video/mp4"
    assert file.headers["accept-ranges"] == "bytes"
    partial = client.get(preview["url"], headers={"Range": "bytes=0-9"})
    assert partial.status_code == 206
    assert partial.content == file.content[:10]


def test_media_of_9000000007_is_unavailable(client: TestClient) -> None:
    job = done_job(client, "9000000007")

    media = client.get(f"{API}/jobs/{job['job_id']}/candidates/c001/media").json()

    assert media["preview"]["status"] == "unavailable"
    assert media["preview"]["reason"] == "source_unavailable"
    assert media["export"]["layouts"] == []
    assert media["export"]["max_duration_s"] is None


def test_media_errors(client: TestClient) -> None:
    job = done_job(client)

    unknown_candidate = client.get(f"{API}/jobs/{job['job_id']}/candidates/c999/media")
    unknown_media = client.get(f"{API}/media/med_0000000000000000")

    assert error_code(unknown_candidate) == "candidate_not_found"
    assert (unknown_media.status_code, error_code(unknown_media)) == (404, "media_not_found")


# --- Exports -----------------------------------------------------------------------------------


def export_body(job_id: object, **changes: object) -> dict[str, object]:
    body: dict[str, object] = {
        "job_id": job_id,
        "candidate_id": "c001",
        "start_s": 115.5,
        "end_s": 150.0,
        "layout": "center_crop",
        "captions": True,
    }
    body.update(changes)
    return body


def test_an_export_from_start_to_finish(client: TestClient, tmp_path: Path) -> None:
    job = done_job(client)

    created = client.post(f"{API}/exports", json=export_body(job["job_id"]))

    assert created.status_code == 201
    export = created.json()
    assert created.headers["location"] == f"{API}/exports/{export['export_id']}"
    assert export["state"] == "queued"
    assert export["download_url"] is None
    done = client.get(f"{API}/exports/{export['export_id']}").json()
    assert done["state"] == "done"
    assert done["download_url"] == f"{API}/exports/{export['export_id']}/file"
    assert done["filename"] == "user_001_2026-10-12_Loud-audio-laughing-chat_00-01-55.mp4"
    assert (done["start_s"], done["end_s"], done["captions"]) == (115.5, 150.0, True)
    file = client.get(done["download_url"])
    assert file.status_code == 200
    assert file.headers["content-type"] == "video/mp4"
    assert file.headers["content-disposition"] == (
        'attachment; filename="user_001_2026-10-12_Loud-audio-laughing-chat_00-01-55.mp4"'
    )
    # Each export has its own file, keyed by its ID.
    assert (tmp_path / "artifacts" / "exports" / f"{export['export_id']}.mp4").is_file()


def test_an_integer_bound_is_accepted_and_strict_types_are_enforced(client: TestClient) -> None:
    job = done_job(client)

    ok = client.post(f"{API}/exports", json=export_body(job["job_id"], start_s=115))
    text = client.post(f"{API}/exports", json=export_body(job["job_id"], start_s="115.5"))
    yes = client.post(f"{API}/exports", json=export_body(job["job_id"], captions="yes"))
    layout = client.post(f"{API}/exports", json=export_body(job["job_id"], layout="stacked"))

    assert ok.status_code == 201
    assert error_code(text) == "invalid_request"
    assert yes.json()["error"] == {
        "code": "invalid_request",
        "message": "The request is invalid: captions must be true or false.",
        "details": {"fields": ["captions"]},
    }
    assert error_code(layout) == "invalid_request"


@pytest.mark.parametrize(
    ("start_s", "end_s"),
    [
        (100.0, 200.0),
        (-1.0, 30.0),
        (340.0, 361.0),
        (50.0, 50.0),
        (60.0, 40.0),
        (120.0, 120.1),
        (120.0, 120.49),
    ],
    ids=[
        "over_90_s",
        "before_start",
        "past_end",
        "empty",
        "reversed",
        "under_min_0_1_s",
        "under_min_0_49_s",
    ],
)
def test_out_of_bounds_exports_are_rejected(
    client: TestClient, start_s: float, end_s: float
) -> None:
    job = done_job(client)

    response = client.post(
        f"{API}/exports", json=export_body(job["job_id"], start_s=start_s, end_s=end_s)
    )

    assert response.status_code == 422
    assert error_code(response) == "invalid_export_bounds"
    assert response.json()["error"]["details"] == {
        "min_start_s": 0.0,
        "max_end_s": 360.0,
        "max_duration_s": 90.0,
    }


def test_short_export_rejected_before_queueing_while_highlights_keeps_separate_minimum(
    make_client: Callable[..., tuple[TestClient, MockService]],
) -> None:
    """POST /exports rejects sub-0.5 s clips with 422 before queueing, accepts the 0.5 s
    boundary (with float epsilon), and POST /highlights keeps its own separate minimum."""
    clip_renderer = FakeRenderer()
    highlights_renderer = FakeHighlightsRenderer()
    client, _ = make_client(render=clip_renderer, highlights_render=highlights_renderer)
    job = done_job(client)

    too_short = client.post(
        f"{API}/exports", json=export_body(job["job_id"], start_s=120.0, end_s=120.1)
    )
    assert (too_short.status_code, error_code(too_short)) == (422, "invalid_export_bounds")
    assert "at least 0.5 seconds" in too_short.json()["error"]["message"]
    assert clip_renderer.specs == []  # never queued

    # 4.1 - 3.6 is 0.49999999999999956 in IEEE-754; epsilon slack accepts it like plan_cut.
    at_minimum = client.post(
        f"{API}/exports", json=export_body(job["job_id"], start_s=3.6, end_s=4.1)
    )
    assert at_minimum.status_code == 201

    # Highlights keep their separate minimum behavior (`min_duration_s=0.0`).
    short_highlights = client.post(
        f"{API}/highlights",
        json={
            "job_id": job["job_id"],
            "segments": [{"candidate_id": "c001", "start_s": 120.0, "end_s": 120.1}],
        },
    )
    assert short_highlights.status_code == 201


def test_the_90_second_message_matches_the_contract(client: TestClient) -> None:
    job = done_job(client)

    response = client.post(
        f"{API}/exports", json=export_body(job["job_id"], start_s=100.0, end_s=200.0)
    )

    assert response.json()["error"]["message"] == (
        "Clips can be at most 90 seconds long. Shorten the selection and try again."
    )


def test_export_checks_run_in_the_contract_order(client: TestClient) -> None:
    failed = done_job(client, "9000000004")
    no_media = done_job(client, "9000000007")
    job = done_job(client)

    unknown_job = client.post(f"{API}/exports", json=export_body("job_0000000000000000"))
    not_done = client.post(f"{API}/exports", json=export_body(failed["job_id"]))
    unknown_candidate = client.post(
        f"{API}/exports", json=export_body(job["job_id"], candidate_id="c999")
    )
    # Media unavailable wins over bad bounds and an unlisted layout.
    unavailable = client.post(
        f"{API}/exports",
        json=export_body(no_media["job_id"], start_s=0.0, end_s=500.0, layout="configured_region"),
    )

    assert (unknown_job.status_code, error_code(unknown_job)) == (404, "job_not_found")
    assert (not_done.status_code, error_code(not_done)) == (409, "job_not_done")
    assert (unknown_candidate.status_code, error_code(unknown_candidate)) == (
        404,
        "candidate_not_found",
    )
    assert (unavailable.status_code, error_code(unavailable)) == (409, "media_unavailable")
    assert unavailable.json()["error"]["details"] == {"reason": "source_unavailable"}


def test_export_file_errors(client: TestClient) -> None:
    job = done_job(client)
    export = client.post(f"{API}/exports", json=export_body(job["job_id"])).json()

    unknown = client.get(f"{API}/exports/exp_0000000000000000/file")
    status_unknown = client.get(f"{API}/exports/exp_0000000000000000")
    assert error_code(unknown) == "export_not_found"
    assert error_code(status_unknown) == "export_not_found"
    file = client.get(f"{API}/exports/{export['export_id']}/file")
    assert file.status_code == 200


def test_a_removed_export_file_is_export_file_missing(client: TestClient, tmp_path: Path) -> None:
    job = done_job(client)
    export = client.post(f"{API}/exports", json=export_body(job["job_id"])).json()
    (tmp_path / "artifacts" / "exports" / f"{export['export_id']}.mp4").unlink()

    response = client.get(f"{API}/exports/{export['export_id']}/file")

    assert (response.status_code, error_code(response)) == (404, "export_file_missing")


def test_the_export_renders_exactly_what_was_asked_from_the_sample_video(
    make_client: Callable[..., tuple[TestClient, MockService]], samples: SampleMedia
) -> None:
    renderer = FakeRenderer()
    client, _ = make_client(render=renderer)
    job = done_job(client)

    export = client.post(f"{API}/exports", json=export_body(job["job_id"])).json()

    (spec,) = renderer.specs
    assert spec.source == samples.preview  # no video_path: the mock's sample video
    assert (spec.interval.start_s, spec.interval.end_s) == (115.5, 150.0)
    assert (spec.layout, spec.captions, spec.overwrite) == ("center_crop", True, False)
    assert spec.output.name == f"{export['export_id']}.mp4"
    assert renderer.stages == ["transcribing", "rendering"]


def test_an_export_of_a_job_with_a_video_path_renders_that_file(
    make_client: Callable[..., tuple[TestClient, MockService]], tmp_path: Path
) -> None:
    renderer = FakeRenderer()
    client, _ = make_client(render=renderer)
    video = tmp_path / "my vod.mp4"
    video.write_bytes(b"x")
    job = client.post(f"{API}/jobs", json={"vod": "9000000001", "video_path": str(video)}).json()

    client.post(f"{API}/exports", json=export_body(job["job_id"], captions=False))

    assert renderer.specs[0].source == video
    assert renderer.stages == ["rendering"]  # no transcribing without captions


def test_export_progress_walks_the_three_stages(
    make_client: Callable[..., tuple[TestClient, MockService]],
) -> None:
    renderer = FakeRenderer()
    client, service = make_client(render=renderer)
    seen: list[tuple[str, int, int]] = []

    def record(spec: ClipSpec) -> None:
        export = service.get_export(spec.output.stem)
        assert export.state == "running"
        assert export.progress is not None
        seen.append(
            (export.progress.stage, export.progress.stage_index, export.progress.stage_count)
        )

    renderer.on_stage_seen = record
    job = done_job(client)

    client.post(f"{API}/exports", json=export_body(job["job_id"]))

    assert seen == [("transcribing", 2, 3), ("rendering", 3, 3)]


def test_configured_region_is_offered_and_passed_on_when_set(
    make_client: Callable[..., tuple[TestClient, MockService]],
) -> None:
    renderer = FakeRenderer()
    region = (0.25, 0.0, 0.5, 1.0)
    client, _ = make_client(render=renderer, export_region=region)
    job = done_job(client)

    media = client.get(f"{API}/jobs/{job['job_id']}/candidates/c001/media").json()
    created = client.post(
        f"{API}/exports", json=export_body(job["job_id"], layout="configured_region")
    )

    assert media["export"]["layouts"] == ["center_crop", "configured_region"]
    assert created.status_code == 201
    assert renderer.specs[0].layout == "configured_region"
    assert renderer.regions == [region]


def test_configured_region_without_a_region_is_an_invalid_request(client: TestClient) -> None:
    job = done_job(client)

    response = client.post(
        f"{API}/exports", json=export_body(job["job_id"], layout="configured_region")
    )

    assert (response.status_code, error_code(response)) == (422, "invalid_request")
    assert response.json()["error"]["details"] == {"fields": ["layout"]}


@pytest.mark.parametrize(
    ("error", "code", "message_part"),
    [
        (CaptioningError("transcription failed"), "captioning_failed", "export without captions"),
        (ExportError("bad layout"), "render_failed", "couldn't be rendered"),
        (
            FFmpegExitError(ffmpeg_command("-version"), 1, b"boom"),
            "render_failed",
            "couldn't be rendered",
        ),
        (IntervalOutsideMediaError("short"), "render_failed", "doesn't cover this whole clip"),
        (IntervalTooShortError("starts after the end"), "render_failed", "doesn't cover"),
        (OSError(28, "No space left on device"), "render_failed", "couldn't be rendered"),
        (SourceMissingError("gone"), "media_unavailable", "missing or can't be read"),
        (RuntimeError("a bug"), "internal_error", "Something went wrong"),
    ],
    ids=[
        "captions",
        "export",
        "ffmpeg",
        "outside_media",
        "after_media",
        "disk",
        "source_missing",
        "bug",
    ],
)
def test_render_failures_get_the_contract_codes(
    make_client: Callable[..., tuple[TestClient, MockService]],
    error: Exception,
    code: str,
    message_part: str,
) -> None:
    client, _ = make_client(render=FakeRenderer(error))
    job = done_job(client)

    created = client.post(f"{API}/exports", json=export_body(job["job_id"])).json()
    export = client.get(f"{API}/exports/{created['export_id']}").json()
    file = client.get(f"{API}/exports/{created['export_id']}/file")

    assert export["state"] == "failed"
    assert export["error"]["code"] == code
    assert message_part in export["error"]["message"]
    assert export["download_url"] is None
    assert (file.status_code, error_code(file)) == (409, "export_not_done")
    if code == "media_unavailable":
        assert export["error"]["details"] == {"reason": "local_file_missing"}


def test_a_video_path_removed_after_the_analysis_is_local_file_missing(
    client: TestClient, tmp_path: Path
) -> None:
    video = tmp_path / "vod.mp4"
    video.write_bytes(b"x")
    job = client.post(f"{API}/jobs", json={"vod": "9000000001", "video_path": str(video)}).json()
    video.unlink()

    media = client.get(f"{API}/jobs/{job['job_id']}/candidates/c001/media").json()
    response = client.post(f"{API}/exports", json=export_body(job["job_id"]))

    assert media["preview"]["reason"] == "local_file_missing"
    assert media["export"]["status"] == "unavailable"
    assert media["export"]["layouts"] == []
    assert (response.status_code, error_code(response)) == (409, "media_unavailable")
    assert response.json()["error"]["details"] == {"reason": "local_file_missing"}


def test_a_running_export_dedupes_and_its_file_isnt_ready(
    make_client: Callable[..., tuple[TestClient, MockService]],
) -> None:
    client, _ = make_client(synchronous=False, step_s=0.05, render=FakeRenderer(delay_s=0.2))
    job = client.post(f"{API}/jobs", json={"vod": "9000000001"}).json()
    wait_until(lambda: client.get(f"{API}/jobs/{job['job_id']}").json()["state"] == "done")

    first = client.post(f"{API}/exports", json=export_body(job["job_id"]))
    again = client.post(f"{API}/exports", json=export_body(job["job_id"]))
    early = client.get(f"{API}/exports/{first.json()['export_id']}/file")

    assert again.status_code == 200
    assert again.json()["export_id"] == first.json()["export_id"]
    assert (early.status_code, error_code(early)) == (409, "export_not_done")
    seen: set[str] = set()

    def done() -> bool:
        export = client.get(f"{API}/exports/{first.json()['export_id']}").json()
        if export["progress"]:
            seen.add(export["progress"]["stage"])
            assert export["progress"]["stage_count"] == 3
        return export["state"] == "done"

    wait_until(done)
    assert "transcribing" in seen or "rendering" in seen


def test_exports_and_highlights_share_one_render_queue(
    make_client: Callable[..., tuple[TestClient, MockService]],
) -> None:
    client, _ = make_client(synchronous=False, step_s=0.05, render=FakeRenderer(delay_s=0.1))
    job = client.post(f"{API}/jobs", json={"vod": "9000000001"}).json()
    wait_until(lambda: client.get(f"{API}/jobs/{job['job_id']}").json()["state"] == "done")

    first = client.post(f"{API}/exports", json=export_body(job["job_id"])).json()
    middle = client.post(f"{API}/highlights", json={"job_id": job["job_id"]}).json()
    last = client.post(f"{API}/exports", json=export_body(job["job_id"], end_s=140.0)).json()
    urls = [
        f"{API}/exports/{first['export_id']}",
        f"{API}/highlights/{middle['highlights_id']}",
        f"{API}/exports/{last['export_id']}",
    ]

    def all_done() -> bool:
        return all(client.get(url).json()["state"] == "done" for url in urls)

    wait_until(all_done)
    finished = [client.get(url).json() for url in urls]
    # One at a time, first in, first out: each render starts only after the one before it
    # finished, so no two ever run at once. (Polling states one URL at a time can't show this:
    # a render can finish and the next start between two requests.)
    for before, after in itertools.pairwise(finished):
        started = datetime.fromisoformat(after["started_at"])
        assert started >= datetime.fromisoformat(before["finished_at"])


# --- Highlights --------------------------------------------------------------------------------


def test_a_highlights_video_from_start_to_finish(client: TestClient) -> None:
    job = done_job(client)
    body = {
        "job_id": job["job_id"],
        "segments": [
            {"candidate_id": "c001", "start_s": 115.5, "end_s": 150.0},
            {"candidate_id": "c002", "start_s": 251.5, "end_s": 276.5},
            {"candidate_id": "c003", "start_s": 40.0, "end_s": 62.0},
        ],
        "order": "chronological",
        "captions": False,
    }

    created = client.post(f"{API}/highlights", json=body)

    assert created.status_code == 201
    video = client.get(f"{API}/highlights/{created.json()['highlights_id']}").json()
    assert video["state"] == "done"
    assert [s["candidate_id"] for s in video["segments"]] == ["c003", "c001", "c002"]
    assert video["duration_s"] == pytest.approx(81.5)
    assert video["download_url"] == f"{API}/highlights/{video['highlights_id']}/file"
    assert video["filename"] == "user_001_2026-10-12_highlights.mp4"
    file = client.get(video["download_url"])
    assert file.headers["content-disposition"] == (
        'attachment; filename="user_001_2026-10-12_highlights.mp4"'
    )


def test_colliding_exports_on_same_job_receive_unique_filenames_and_fallback_when_result_missing(
    client: TestClient, tmp_path: Path
) -> None:
    job = done_job(client)

    first = client.post(
        f"{API}/exports", json=export_body(job["job_id"], start_s=115.2, captions=False)
    ).json()
    second = client.post(
        f"{API}/exports", json=export_body(job["job_id"], start_s=115.8, captions=True)
    ).json()

    first_done = client.get(f"{API}/exports/{first['export_id']}").json()
    second_done = client.get(f"{API}/exports/{second['export_id']}").json()
    assert first_done["filename"] == "user_001_2026-10-12_Loud-audio-laughing-chat_00-01-55.mp4"
    assert second_done["filename"] == "user_001_2026-10-12_Loud-audio-laughing-chat_00-01-55_2.mp4"
    file2 = client.get(second_done["download_url"])
    assert file2.headers["content-disposition"] == (
        'attachment; filename="user_001_2026-10-12_Loud-audio-laughing-chat_00-01-55_2.mp4"'
    )

    # Removing the analysis JSON still lets finished export files download with clean names.
    (tmp_path / "artifacts" / "analysis" / f"{job['job_id']}.json").unlink()
    file1 = client.get(first_done["download_url"])
    assert file1.status_code == 200
    assert (
        "user_001_2026-10-12_Loud-audio-laughing-chat_00-01-55.mp4"
        in file1.headers["content-disposition"]
    )


def test_highlights_default_to_the_top_candidates_and_rank_order_puts_the_best_first(
    client: TestClient,
) -> None:
    job = done_job(client)

    chronological = client.post(f"{API}/highlights", json={"job_id": job["job_id"]}).json()
    by_rank = client.post(
        f"{API}/highlights", json={"job_id": job["job_id"], "order": "rank", "count": 2}
    ).json()

    assert [s["candidate_id"] for s in chronological["segments"]] == ["c003", "c001", "c002"]
    assert chronological["segments"][0] == {"candidate_id": "c003", "start_s": 40.0, "end_s": 62.0}
    assert [s["candidate_id"] for s in by_rank["segments"]] == ["c001", "c002"]


@pytest.mark.parametrize(
    ("changes", "code", "index"),
    [
        ({"segments": []}, "invalid_request", None),
        ({"count": 0}, "invalid_request", None),
        (
            {"segments": [{"candidate_id": "c001", "start_s": 0.0, "end_s": 120.0}]},
            "invalid_highlights_bounds",
            0,
        ),
        (
            {
                "segments": [
                    {"candidate_id": "c001", "start_s": 10.0 * i, "end_s": 10.0 * i + 90}
                    for i in range(11)
                ]
            },
            "invalid_highlights_bounds",
            None,
        ),
        (
            {"segments": [{"candidate_id": "c999", "start_s": 0.0, "end_s": 10.0}]},
            "candidate_not_found",
            None,
        ),
    ],
    ids=[
        "empty_segments",
        "count_0",
        "segment_over_90_s",
        "total_over_15_min",
        "unknown_candidate",
    ],
)
def test_bad_highlights_requests(
    client: TestClient, changes: dict[str, object], code: str, index: int | None
) -> None:
    job = done_job(client)

    response = client.post(f"{API}/highlights", json={"job_id": job["job_id"], **changes})

    assert error_code(response) == code
    if code == "invalid_highlights_bounds":
        assert response.json()["error"]["details"] == {
            "segment_index": index,
            "max_duration_s": 90.0,
            "max_total_duration_s": 900.0,
        }


def test_highlights_of_an_empty_result_or_without_media_are_rejected(client: TestClient) -> None:
    empty = done_job(client, "9000000006")
    no_media = done_job(client, "9000000007")

    nothing = client.post(f"{API}/highlights", json={"job_id": empty["job_id"]})
    unavailable = client.post(f"{API}/highlights", json={"job_id": no_media["job_id"]})

    assert error_code(nothing) == "invalid_highlights_bounds"
    assert nothing.json()["error"]["details"]["segment_index"] is None
    assert (unavailable.status_code, error_code(unavailable)) == (409, "media_unavailable")


def test_highlights_errors_for_unknown_ids(client: TestClient) -> None:
    status = client.get(f"{API}/highlights/hl_0000000000000000")
    file = client.get(f"{API}/highlights/hl_0000000000000000/file")

    assert error_code(status) == "highlights_not_found"
    assert error_code(file) == "highlights_not_found"


def test_the_highlights_render_gets_the_job_and_reports_its_stages(
    make_client: Callable[..., tuple[TestClient, MockService]], samples: SampleMedia
) -> None:
    """The worker passes the resolved segments and the job's (or sample's) source to the
    renderer, and the renderer's stages reach the record (#72)."""
    fake = FakeHighlightsRenderer()
    client, _ = make_client(highlights_render=fake)
    job = done_job(client)

    plain = client.post(f"{API}/highlights", json={"job_id": job["job_id"], "count": 1}).json()
    captioned = client.post(
        f"{API}/highlights",
        json={"job_id": job["job_id"], "count": 1, "captions": True, "order": "rank"},
    ).json()

    plain_request, captioned_request = fake.requests
    assert fake.stages == [
        "obtaining_media",
        "rendering",
        "obtaining_media",
        "transcribing",
        "rendering",
    ]
    assert plain_request.source == samples.preview == captioned_request.source
    assert plain_request.output.name == f"{plain['highlights_id']}.mp4"
    assert plain_request.segments == [Segment("c001", 118.0, 148.0)]  # count 1: the best
    assert plain_request.order == "chronological"
    assert not plain_request.captions
    assert captioned_request.captions
    assert captioned_request.order == "rank"
    plain = client.get(f"{API}/highlights/{plain['highlights_id']}").json()
    captioned = client.get(f"{API}/highlights/{captioned['highlights_id']}").json()
    assert plain["state"] == "done"
    assert captioned["state"] == "done"
    assert plain["duration_s"] == pytest.approx(30.0)  # the rendered file's duration
    assert captioned["duration_s"] == pytest.approx(30.0)


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (CaptioningError("no captions extra"), "captioning_failed"),
        (SourceMissingError("gone"), "media_unavailable"),
        (ExportError("boom"), "render_failed"),
        (OSError("disk"), "render_failed"),
    ],
    ids=["captions", "missing", "render", "os"],
)
def test_a_failing_highlights_render_keeps_the_contract_code(
    make_client: Callable[..., tuple[TestClient, MockService]],
    error: Exception,
    code: str,
) -> None:
    fake = FakeHighlightsRenderer(error)
    client, _ = make_client(highlights_render=fake)
    job = done_job(client)

    created = client.post(f"{API}/highlights", json={"job_id": job["job_id"]})

    video = client.get(f"{API}/highlights/{created.json()['highlights_id']}").json()
    assert video["state"] == "failed"
    assert video["error"]["code"] == code
    assert video["download_url"] is None


def test_a_done_highlights_video_carries_the_renderers_duration(
    make_client: Callable[..., tuple[TestClient, MockService]],
) -> None:
    """`duration_s` is the finished file's, published together with the done state (§ 7.2):
    the read that first sees done already carries it, never the planned total."""
    fake = FakeHighlightsRenderer(duration_s=123.4)
    client, _ = make_client(synchronous=False, highlights_render=fake)
    job = client.post(f"{API}/jobs", json={"vod": "9000000001"}).json()
    wait_until(lambda: client.get(f"{API}/jobs/{job['job_id']}").json()["state"] == "done")

    created = client.post(f"{API}/highlights", json={"job_id": job["job_id"]})

    deadline = time.monotonic() + 10.0
    while True:
        video = client.get(f"{API}/highlights/{created.json()['highlights_id']}").json()
        assert video["state"] != "failed", video["error"]
        if video["state"] == "done":
            break
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.01)
    assert video["duration_s"] == pytest.approx(123.4)  # never the planned 77 s


def test_a_highlights_video_of_a_job_without_its_local_file_is_rejected(
    client: TestClient, tmp_path: Path
) -> None:
    """A job whose video_path was deleted is rejected at creation, like an export (§ 7.1)."""
    video = tmp_path / "vod.mp4"
    video.write_bytes(b"x")
    job = client.post(f"{API}/jobs", json={"vod": "9000000001", "video_path": str(video)}).json()
    video.unlink()

    response = client.post(f"{API}/highlights", json={"job_id": job["job_id"]})

    assert (response.status_code, error_code(response)) == (409, "media_unavailable")
    assert response.json()["error"]["details"] == {"reason": "local_file_missing"}


# --- CORS --------------------------------------------------------------------------------------


# --- ZIP downloads (§ 7.6) ---------------------------------------------------------------------


class FailUncaptionedRenderer(FakeRenderer):
    """Renders captioned clips and fails uncaptioned ones, so one job has both outcomes."""

    def __call__(
        self,
        spec: ClipSpec,
        /,
        *,
        region: tuple[float, float, float, float] | None,
        transcriber: Transcriber | None,
        on_stage: Callable[[Stage], None],
    ) -> object:
        if not spec.captions:
            raise ExportError("bad layout")
        return super().__call__(spec, region=region, transcriber=transcriber, on_stage=on_stage)


def open_zip(response: httpx2.Response) -> zipfile.ZipFile:
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/zip"
    assert response.headers["cache-control"] == "no-store"
    return zipfile.ZipFile(io.BytesIO(response.content))


def test_a_clips_zip_has_each_moments_newest_finished_export_and_lists_the_rest(
    make_client: Callable[..., tuple[TestClient, MockService]], tmp_path: Path
) -> None:
    client, _ = make_client(render=FailUncaptionedRenderer())
    job_id = done_job(client)["job_id"]

    def export(**changes: object) -> str:
        response = client.post(f"{API}/exports", json=export_body(job_id, **changes))
        return response.json()["export_id"]

    export(start_s=115.2)  # c001's first trim, superseded by the next one
    newest_c001 = export(start_s=116.0)
    c002 = export(candidate_id="c002", start_s=251.5, end_s=276.5)
    failed_c003 = export(candidate_id="c003", start_s=40.0, end_s=62.0, captions=False)
    for export_id in (newest_c001, c002):
        assert client.get(f"{API}/exports/{export_id}").json()["state"] == "done"
    (tmp_path / "artifacts" / "exports" / f"{c002}.mp4").unlink()  # cleaned up by the user

    status = client.get(f"{API}/jobs/{job_id}/archives/clips")

    assert status.status_code == 200
    assert status.headers["cache-control"] == "no-store"
    assert status.json() == {
        "kind": "clips",
        "ready_count": 1,
        "omitted": [
            {"render_id": c002, "candidate_id": "c002", "rank": 2, "reason": "file_missing"},
            {"render_id": failed_c003, "candidate_id": "c003", "rank": 3, "reason": "failed"},
        ],
        "filename": "user_001_2026-10-12_clips.zip",
        "download_url": f"{API}/jobs/{job_id}/archives/clips/file",
    }

    response = client.get(status.json()["download_url"])

    assert response.headers["content-disposition"] == (
        'attachment; filename="user_001_2026-10-12_clips.zip"'
    )
    newest_name = client.get(f"{API}/exports/{newest_c001}").json()["filename"]
    with open_zip(response) as archive:
        assert archive.testzip() is None
        assert archive.namelist() == [newest_name, "missing.txt"]
        assert archive.getinfo(newest_name).compress_type == zipfile.ZIP_STORED
        assert archive.read(newest_name) == b"rendered clip"
        missing = archive.read("missing.txt").decode()
    assert f"Clip 2 ({c002}): file no longer on disk" in missing
    assert f"Clip 3 ({failed_c003}): render failed" in missing


def test_a_highlights_zip_has_every_finished_reel_under_its_download_name(
    client: TestClient,
) -> None:
    job_id = done_job(client)["job_id"]
    ids = [
        client.post(f"{API}/highlights", json={"job_id": job_id, "count": count}).json()[
            "highlights_id"
        ]
        for count in (2, 3)
    ]
    names = [client.get(f"{API}/highlights/{i}").json()["filename"] for i in ids]
    assert names == ["user_001_2026-10-12_highlights.mp4", "user_001_2026-10-12_highlights_2.mp4"]

    status = client.get(f"{API}/jobs/{job_id}/archives/highlights").json()
    response = client.get(status["download_url"])

    assert (status["ready_count"], status["omitted"]) == (2, [])
    assert status["filename"] == "user_001_2026-10-12_highlights.zip"
    with open_zip(response) as archive:
        assert archive.namelist() == names  # no missing.txt when nothing was left out
        assert {archive.read(name) for name in names} == {b"rendered highlights"}


def test_an_archive_with_nothing_finished_has_no_download(
    make_client: Callable[..., tuple[TestClient, MockService]],
) -> None:
    client, _ = make_client(render=FakeRenderer(ExportError("bad layout")))
    job_id = done_job(client)["job_id"]
    failed = client.post(f"{API}/exports", json=export_body(job_id)).json()["export_id"]

    clips = client.get(f"{API}/jobs/{job_id}/archives/clips").json()
    highlights = client.get(f"{API}/jobs/{job_id}/archives/highlights").json()
    file = client.get(f"{API}/jobs/{job_id}/archives/clips/file")

    assert (clips["ready_count"], clips["download_url"]) == (0, None)
    assert clips["omitted"] == [
        {"render_id": failed, "candidate_id": "c001", "rank": 1, "reason": "failed"}
    ]
    assert (highlights["ready_count"], highlights["omitted"]) == (0, [])
    assert highlights["download_url"] is None
    assert (file.status_code, error_code(file)) == (409, "archive_empty")


def test_archive_errors(client: TestClient) -> None:
    job_id = done_job(client)["job_id"]

    unknown_job = client.get(f"{API}/jobs/job_0000000000000000/archives/clips")
    unknown_kind = client.get(f"{API}/jobs/{job_id}/archives/everything")

    assert (unknown_job.status_code, error_code(unknown_job)) == (404, "job_not_found")
    assert (unknown_kind.status_code, error_code(unknown_kind)) == (422, "invalid_request")


@pytest.mark.parametrize("spec_version", ["2.0", "2.4"])
def test_a_client_disconnect_closes_the_zips_open_file_at_once(
    tmp_path: Path, spec_version: str
) -> None:
    """Both of Starlette's disconnect paths close the generator, so the MP4 it was reading
    can be deleted right away (Windows refuses to delete an open file)."""
    from starlette.requests import ClientDisconnect

    from autoclipper.api.app import _ClosingStreamingResponse  # pyright: ignore[reportPrivateUsage]
    from autoclipper.export.archive import CHUNK_BYTES, ArchiveMember, ArchivePlan, stream_archive

    reel = tmp_path / "hl_1.mp4"
    reel.write_bytes(b"\0" * (CHUNK_BYTES * 8))
    member = ArchiveMember(reel, "reel.mp4", "Highlights video (hl_1): file missing")
    body = stream_archive(ArchivePlan("reels.zip", (member,), (), ()))
    response = _ClosingStreamingResponse(
        body, media_type="application/zip", headers={"Cache-Control": "no-store"}
    )

    async def run() -> None:
        first_chunk = asyncio.Event()
        chunks = 0

        async def receive() -> dict[str, object]:
            await first_chunk.wait()
            return {"type": "http.disconnect"}

        async def send(message: dict[str, object]) -> None:
            nonlocal chunks
            if message["type"] != "http.response.body":
                return
            chunks += 1
            first_chunk.set()
            if spec_version == "2.4" and chunks > 1:
                raise OSError("connection reset")  # what an ASGI 2.4 server raises
            await asyncio.sleep(0)  # let the disconnect win on the 2.0 path

        scope = {"type": "http", "asgi": {"spec_version": spec_version}}
        with contextlib.suppress(ClientDisconnect):
            await response(scope, receive, send)  # type: ignore[arg-type]
        assert 0 < chunks < 8

    asyncio.run(run())

    assert inspect.getgeneratorstate(body) == inspect.GEN_CLOSED  # not just abandoned
    reel.unlink()  # would raise PermissionError on Windows if the file were still open


@pytest.mark.ffmpeg
def test_a_zip_of_real_renders_holds_playable_mp4s(
    make_client: Callable[..., tuple[TestClient, MockService]], tmp_path: Path
) -> None:
    samples = ensure_sample_media(tmp_path / "media", preview_duration_s=6.0)
    client, _ = make_client(
        samples=samples, render=_real_render(), highlights_render=render_highlights
    )
    job_id = done_job(client)["job_id"]
    for candidate_id, start_s in (("c001", 1.0), ("c002", 3.0)):
        body = export_body(
            job_id, candidate_id=candidate_id, start_s=start_s, end_s=start_s + 2.0, captions=False
        )
        assert client.post(f"{API}/exports", json=body).status_code == 201
    segments = [
        {"candidate_id": "c001", "start_s": 0.5, "end_s": 2.0},
        {"candidate_id": "c002", "start_s": 3.0, "end_s": 4.5},
    ]
    highlights = client.post(f"{API}/highlights", json={"job_id": job_id, "segments": segments})
    assert highlights.status_code == 201

    for kind, count, size in (("clips", 2, (1080, 1920)), ("highlights", 1, (1920, 1080))):
        status = client.get(f"{API}/jobs/{job_id}/archives/{kind}").json()
        assert (status["ready_count"], status["omitted"]) == (count, [])
        with open_zip(client.get(status["download_url"])) as archive:
            assert len(archive.infolist()) == count
            for info in archive.infolist():
                assert info.compress_type == zipfile.ZIP_STORED
                video = probe(Path(archive.extract(info, tmp_path / kind))).video_streams[0]
                assert (video.width, video.height) == size


# --- Settings and the output folder (§ 9.9) -----------------------------------------------------


def set_folder(client: TestClient, folder: object, **headers: str) -> httpx2.Response:
    return client.put(
        f"{API}/settings",
        json={"output_folder": None if folder is None else str(folder)},
        headers=headers,
    )


def test_the_output_folder_is_validated_saved_and_survives_a_restart(
    make_client: Callable[..., tuple[TestClient, MockService]], tmp_path: Path
) -> None:
    client, _ = make_client()
    folder = tmp_path / "My Clips" / "AutoClipper"

    before = client.get(f"{API}/settings")
    saved = set_folder(client, folder)

    assert before.json() == {"output_folder": None, "last_copy": None}
    assert before.headers["cache-control"] == "no-store"
    assert saved.status_code == 200
    assert saved.json() == {"output_folder": str(folder), "last_copy": None}
    assert folder.is_dir()  # created on save
    # A new server over the same library database still has it.
    restarted, _ = make_client()
    assert restarted.get(f"{API}/settings").json()["output_folder"] == str(folder)

    cleared = set_folder(restarted, None)
    assert cleared.json()["output_folder"] is None
    assert make_client()[0].get(f"{API}/settings").json()["output_folder"] is None


@pytest.mark.parametrize(
    ("make", "reason"),
    [
        (lambda tmp: "clips", "not_absolute"),
        (lambda tmp: tmp / "notes.txt", "not_a_directory"),
        (lambda tmp: tmp / "notes.txt" / "clips", "not_a_directory"),
    ],
    ids=["relative", "a-file", "below-a-file"],
)
def test_an_unusable_output_folder_is_a_422_that_names_no_path(
    client: TestClient, tmp_path: Path, make: Callable[[Path], object], reason: str
) -> None:
    (tmp_path / "notes.txt").write_text("not a folder")

    response = set_folder(client, make(tmp_path))

    assert response.status_code == 422
    assert error_code(response) == "invalid_output_folder"
    assert response.json()["error"]["details"] == {"reason": reason}
    assert str(tmp_path) not in response.text
    assert client.get(f"{API}/settings").json()["output_folder"] is None


def test_settings_reject_other_browser_origins_and_bad_bodies(
    client: TestClient, tmp_path: Path
) -> None:
    foreign = set_folder(client, tmp_path, origin="https://evil.example")
    local = set_folder(client, tmp_path, origin="http://localhost:3000")
    preflight = client.options(
        f"{API}/settings",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "PUT",
            "Access-Control-Request-Headers": "Content-Type",
        },
    )
    extra = client.put(f"{API}/settings", json={"output_folder": None, "other": 1})

    assert (foreign.status_code, error_code(foreign)) == (403, "origin_not_allowed")
    assert local.status_code == 200
    assert preflight.status_code == 200
    assert "PUT" in preflight.headers["access-control-allow-methods"]
    assert (extra.status_code, error_code(extra)) == (422, "invalid_request")


def test_finished_exports_and_highlights_are_copied_under_unique_download_names(
    client: TestClient, tmp_path: Path
) -> None:
    folder = tmp_path / "out"
    assert set_folder(client, folder).status_code == 200
    job_id = done_job(client)["job_id"]
    name = "user_001_2026-10-12_Loud-audio-laughing-chat_00-01-55.mp4"
    (folder / name).write_bytes(b"the user's own file")  # already in the folder

    export_id = client.post(f"{API}/exports", json=export_body(job_id)).json()["export_id"]
    export = client.get(f"{API}/exports/{export_id}").json()
    after_export = client.get(f"{API}/settings").json()["last_copy"]
    highlights_id = client.post(f"{API}/highlights", json={"job_id": job_id}).json()[
        "highlights_id"
    ]
    after_highlights = client.get(f"{API}/settings").json()["last_copy"]

    assert export["filename"] == name  # the download name is unchanged
    assert (folder / name).read_bytes() == b"the user's own file"
    copied = folder / "user_001_2026-10-12_Loud-audio-laughing-chat_00-01-55_2.mp4"
    assert copied.read_bytes() == b"rendered clip"
    assert after_export["render_id"] == export_id
    assert (after_export["kind"], after_export["state"]) == ("export", "copied")
    assert (after_export["filename"], after_export["message"]) == (copied.name, None)
    assert (folder / "user_001_2026-10-12_highlights.mp4").read_bytes() == b"rendered highlights"
    assert after_highlights["render_id"] == highlights_id
    assert after_highlights["kind"] == "highlights"
    assert sorted(p.name for p in folder.iterdir()) == sorted(
        [name, copied.name, "user_001_2026-10-12_highlights.mp4"]
    )


def test_no_folder_means_no_copies_and_a_lost_folder_fails_only_the_copy(
    client: TestClient, tmp_path: Path
) -> None:
    job_id = done_job(client)["job_id"]
    client.post(f"{API}/exports", json=export_body(job_id))
    assert client.get(f"{API}/settings").json()["last_copy"] is None

    folder = tmp_path / "usb-drive"
    set_folder(client, folder)
    shutil.rmtree(folder)  # the drive was unplugged
    export_id = client.post(f"{API}/exports", json=export_body(job_id, start_s=120.0)).json()[
        "export_id"
    ]

    assert client.get(f"{API}/exports/{export_id}").json()["state"] == "done"
    last_copy = client.get(f"{API}/settings").json()["last_copy"]
    assert (last_copy["state"], last_copy["filename"]) == ("failed", None)
    assert "output folder is missing" in last_copy["message"]
    assert str(tmp_path) not in json.dumps(last_copy)
    assert not folder.exists()  # a copy never recreates a folder that went away


def test_a_slow_copy_never_holds_up_the_render_worker(
    make_client: Callable[..., tuple[TestClient, MockService]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from autoclipper.api import service as service_module

    release = threading.Event()
    copying = threading.Event()
    real_copy = service_module.copy_into_folder

    def slow_copy(source: Path, folder: Path, filename: str, **kwargs: Any) -> Path:  # noqa: ANN401
        copying.set()
        assert release.wait(timeout=10)
        return real_copy(source, folder, filename, **kwargs)

    monkeypatch.setattr(service_module, "copy_into_folder", slow_copy)
    client, _ = make_client(synchronous=False, step_s=0.0)
    folder = tmp_path / "out"
    set_folder(client, folder)
    job_id = client.post(f"{API}/jobs", json={"vod": "9000000001"}).json()["job_id"]
    wait_until(lambda: client.get(f"{API}/jobs/{job_id}").json()["state"] == "done")

    first = client.post(f"{API}/exports", json=export_body(job_id)).json()["export_id"]
    second = client.post(f"{API}/exports", json=export_body(job_id, start_s=120.0)).json()[
        "export_id"
    ]
    assert copying.wait(timeout=10)
    # The first copy is stuck, yet the second render finishes and the API answers.
    wait_until(lambda: client.get(f"{API}/exports/{second}").json()["state"] == "done")
    assert client.get(f"{API}/health").status_code == 200
    assert list(folder.iterdir()) == []

    release.set()
    wait_until(lambda: len(list(folder.iterdir())) == 2)
    last_copy = client.get(f"{API}/settings").json()["last_copy"]
    assert last_copy["render_id"] in (first, second)


def test_clearing_the_folder_drops_copies_still_waiting(
    make_client: Callable[..., tuple[TestClient, MockService]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from autoclipper.api import service as service_module

    release = threading.Event()
    copying = threading.Event()
    real_copy = service_module.copy_into_folder

    def slow_copy(source: Path, folder: Path, filename: str, **kwargs: Any) -> Path:  # noqa: ANN401
        copying.set()
        assert release.wait(timeout=10)
        return real_copy(source, folder, filename, **kwargs)

    monkeypatch.setattr(service_module, "copy_into_folder", slow_copy)
    client, _ = make_client(synchronous=False, step_s=0.0)
    folder = tmp_path / "out"
    set_folder(client, folder)
    job_id = client.post(f"{API}/jobs", json={"vod": "9000000001"}).json()["job_id"]
    wait_until(lambda: client.get(f"{API}/jobs/{job_id}").json()["state"] == "done")
    first = client.post(f"{API}/exports", json=export_body(job_id)).json()["export_id"]
    assert copying.wait(timeout=10)
    second = client.post(f"{API}/exports", json=export_body(job_id, start_s=120.0)).json()[
        "export_id"
    ]
    wait_until(lambda: client.get(f"{API}/exports/{second}").json()["state"] == "done")

    set_folder(client, None)  # while the second copy is still waiting
    release.set()

    wait_until(lambda: client.get(f"{API}/settings").json()["last_copy"] is not None)
    time.sleep(0.2)  # give a wrongly kept second copy time to land
    assert client.get(f"{API}/settings").json()["last_copy"]["render_id"] == first
    assert [p.name for p in folder.iterdir()] == [
        client.get(f"{API}/exports/{first}").json()["filename"]
    ]


def test_shutting_down_mid_copy_leaves_no_partial_file(
    make_client: Callable[..., tuple[TestClient, MockService]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from autoclipper.api import service as service_module

    copying = threading.Event()
    real_copy = service_module.copy_into_folder

    def copy_until_stopped(
        source: Path, folder: Path, filename: str, *, stop: threading.Event
    ) -> Path:
        copying.set()
        assert stop.wait(timeout=10)  # close() asks the copy to stop
        return real_copy(source, folder, filename, stop=stop)

    monkeypatch.setattr(service_module, "copy_into_folder", copy_until_stopped)
    client, service = make_client(synchronous=False, step_s=0.0)
    folder = tmp_path / "out"
    set_folder(client, folder)
    job_id = client.post(f"{API}/jobs", json={"vod": "9000000001"}).json()["job_id"]
    wait_until(lambda: client.get(f"{API}/jobs/{job_id}").json()["state"] == "done")
    client.post(f"{API}/exports", json=export_body(job_id))
    assert copying.wait(timeout=10)

    started = time.monotonic()
    service.close()

    assert time.monotonic() - started < 3.0
    assert list(folder.iterdir()) == []  # the real copy started, saw the stop and cleaned up


def test_startup_removes_partial_copies_a_crash_left_behind(
    make_client: Callable[..., tuple[TestClient, MockService]], tmp_path: Path
) -> None:
    from autoclipper.export.output_folder import STALE_PARTIAL_AGE_S

    folder = tmp_path / "out"
    set_folder(make_client()[0], folder)
    stale = folder / ".autoclipper-crashed.part"
    stale.write_bytes(b"\0" * 1000)
    old = time.time() - STALE_PARTIAL_AGE_S - 60
    os.utime(stale, (old, old))

    make_client(synchronous=False, step_s=0.0)  # the next server start

    wait_until(lambda: not stale.exists())


def test_a_render_file_gone_before_its_copy_gets_its_own_message(
    make_client: Callable[..., tuple[TestClient, MockService]], tmp_path: Path
) -> None:
    client, service = make_client()
    folder = tmp_path / "out"
    set_folder(client, folder)
    job_id = done_job(client)["job_id"]
    export_id = client.post(f"{API}/exports", json=export_body(job_id)).json()["export_id"]
    (tmp_path / "artifacts" / "exports" / f"{export_id}.mp4").unlink()

    service._run_copy_safely((export_id, folder))  # pyright: ignore[reportPrivateUsage]

    last_copy = client.get(f"{API}/settings").json()["last_copy"]
    assert (last_copy["state"], last_copy["render_id"]) == ("failed", export_id)
    assert "no longer on disk" in last_copy["message"]
    assert "free space" not in last_copy["message"]
    assert _partials(folder) == []


def _partials(folder: Path) -> list[str]:
    return sorted(p.name for p in folder.iterdir() if p.name.endswith(".part"))


@pytest.mark.ffmpeg
def test_a_real_export_is_copied_to_the_output_folder_as_a_playable_mp4(
    make_client: Callable[..., tuple[TestClient, MockService]], tmp_path: Path
) -> None:
    samples = ensure_sample_media(tmp_path / "media", preview_duration_s=6.0)
    client, _ = make_client(samples=samples, render=_real_render())
    folder = tmp_path / "out"
    set_folder(client, folder)
    job_id = done_job(client)["job_id"]

    body = export_body(job_id, start_s=1.0, end_s=3.5, captions=False)
    export_id = client.post(f"{API}/exports", json=body).json()["export_id"]
    export = client.get(f"{API}/exports/{export_id}").json()

    copied = folder / export["filename"]
    assert (
        copied.read_bytes()
        == (tmp_path / "artifacts" / "exports" / f"{export_id}.mp4").read_bytes()
    )
    info = probe(copied)
    assert (info.video_streams[0].width, info.video_streams[0].height) == (1080, 1920)
    assert info.duration_s == pytest.approx(2.5, abs=0.05)


def test_cors_allows_the_local_frontend_only(client: TestClient) -> None:
    allowed = client.options(
        f"{API}/jobs",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "Content-Type",
        },
    )
    other = client.get(f"{API}/health", headers={"Origin": "http://evil.example"})
    simple = client.post(
        f"{API}/jobs", json={"vod": "9000000001"}, headers={"Origin": "http://127.0.0.1:3000"}
    )

    assert allowed.headers["access-control-allow-origin"] == "http://localhost:3000"
    assert "access-control-allow-origin" not in other.headers
    assert "Location" in simple.headers["access-control-expose-headers"]


# --- Sample media and the serve command --------------------------------------------------------


@pytest.mark.ffmpeg
def test_sample_media_has_the_contract_size_and_is_reused(tmp_path: Path) -> None:
    samples = ensure_sample_media(tmp_path / "media", preview_duration_s=3.0)

    preview = probe(samples.preview)
    assert preview.duration_s == pytest.approx(3.0, abs=0.2)
    assert preview.audio_streams
    before = samples.preview.stat().st_mtime_ns
    again = ensure_sample_media(tmp_path / "media", preview_duration_s=3.0)
    assert again.preview.stat().st_mtime_ns == before  # not generated again


class FixedWords:
    def __call__(self, wav: Path, /) -> list[Word]:
        return [Word("HELLO", 0.2, 0.8)]


@pytest.mark.ffmpeg
@pytest.mark.parametrize("captions", [False, True], ids=["plain", "captions"])
def test_a_real_export_of_the_sample_video(
    make_client: Callable[..., tuple[TestClient, MockService]], tmp_path: Path, captions: bool
) -> None:
    samples = ensure_sample_media(tmp_path / "media", preview_duration_s=6.0)
    # The real renderer; the transcriber is fixed so no model is needed.
    client, _ = make_client(samples=samples, render=_real_render(), transcriber=FixedWords())
    job = done_job(client)

    created = client.post(
        f"{API}/exports",
        json=export_body(job["job_id"], start_s=1.25, end_s=4.0, captions=captions),
    ).json()
    export = client.get(f"{API}/exports/{created['export_id']}").json()

    assert export["state"] == "done", export["error"]
    file = client.get(export["download_url"])
    assert export["filename"] == "user_001_2026-10-12_Loud-audio-laughing-chat_00-00-01.mp4"
    assert file.headers["content-disposition"] == f'attachment; filename="{export["filename"]}"'
    out = tmp_path / "download.mp4"
    out.write_bytes(file.content)
    info = probe(out)
    assert (info.video_streams[0].width, info.video_streams[0].height) == (1080, 1920)
    assert info.video_streams[0].codec_name == "h264"
    assert info.audio_streams[0].codec_name == "aac"
    assert info.duration_s == pytest.approx(2.75, abs=0.05)


def _real_render() -> object:
    from autoclipper.export.clip import render_clip

    return render_clip


@pytest.mark.ffmpeg
def test_a_real_highlights_video_of_the_sample_video(
    make_client: Callable[..., tuple[TestClient, MockService]], tmp_path: Path
) -> None:
    """The endpoints render a highlights video for real (#72): two short segments of the
    sample, joined at 1920x1080 with the source's framing, served for download."""
    samples = ensure_sample_media(tmp_path / "media", preview_duration_s=6.0)
    client, _ = make_client(samples=samples, highlights_render=render_highlights)
    job = done_job(client)

    created = client.post(
        f"{API}/highlights",
        json={
            "job_id": job["job_id"],
            "segments": [
                {"candidate_id": "c003", "start_s": 0.5, "end_s": 2.0},
                {"candidate_id": "c001", "start_s": 3.0, "end_s": 4.5},
            ],
            "order": "chronological",
        },
    )

    assert created.status_code == 201
    video = client.get(f"{API}/highlights/{created.json()['highlights_id']}").json()
    assert video["state"] == "done", video["error"]
    assert video["duration_s"] == pytest.approx(3.0, abs=0.1)  # the finished file's duration
    file = client.get(video["download_url"])
    assert video["filename"] == "user_001_2026-10-12_highlights.mp4"
    assert file.headers["content-disposition"] == f'attachment; filename="{video["filename"]}"'
    out = tmp_path / "download.mp4"
    out.write_bytes(file.content)
    info = probe(out)
    assert (info.video_streams[0].width, info.video_streams[0].height) == (1920, 1080)
    assert info.video_streams[0].codec_name == "h264"
    assert info.audio_streams[0].codec_name == "aac"
    assert info.duration_s == pytest.approx(3.0, abs=0.1)


def test_a_500_keeps_its_cors_headers(
    make_client: Callable[..., tuple[TestClient, MockService]], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, service = make_client()

    def broken(job_id: str) -> object:
        raise RuntimeError("boom")

    monkeypatch.setattr(service, "get_job", broken)
    response = client.get(f"{API}/jobs/job_x", headers={"Origin": "http://localhost:3000"})

    assert response.status_code == 500
    assert response.headers["access-control-allow-origin"] == "http://localhost:3000"
    assert error_code(response) == "internal_error"


def test_json_bodies_say_utf_8(client: TestClient) -> None:
    ok = client.get(f"{API}/health")
    error = client.get(f"{API}/jobs/job_x")

    assert ok.headers["content-type"] == "application/json; charset=utf-8"
    assert error.headers["content-type"] == "application/json; charset=utf-8"


@pytest.mark.parametrize(
    ("content", "message"),
    [(b'{"vod":"9000000001",}', "the body isn't valid JSON"), (b"", "the body is missing")],
)
def test_broken_bodies_name_no_bogus_fields(
    client: TestClient, content: bytes, message: str
) -> None:
    response = client.post(
        f"{API}/jobs", content=content, headers={"Content-Type": "application/json"}
    )

    assert response.json()["error"] == {
        "code": "invalid_request",
        "message": f"The request is invalid: {message}.",
        "details": {"fields": []},
    }


def test_a_trailing_slash_is_a_404_envelope_not_a_redirect(client: TestClient) -> None:
    response = client.post(f"{API}/jobs/", json={"vod": "9000000001"}, follow_redirects=False)

    assert (response.status_code, error_code(response)) == (404, "not_found")


def test_unsatisfiable_and_malformed_ranges(client: TestClient) -> None:
    job = done_job(client)
    url = client.get(f"{API}/jobs/{job['job_id']}/candidates/c001/media").json()["preview"]["url"]

    beyond = client.get(url, headers={"Range": "bytes=999999999-"})
    malformed = client.get(url, headers={"Range": "frames=1-2"})

    assert beyond.status_code == 416
    assert malformed.status_code == 400


def test_9000000007_with_a_local_file_has_media(
    client: TestClient,
    make_test_media: Callable[..., Path],
    tmp_path: Path,
    short_preview_scenario: None,
) -> None:
    video = make_test_media("vod.mp4", duration_s=3.0)
    created = client.post(f"{API}/jobs", json={"vod": "9000000007", "video_path": str(video)})

    media = client.get(f"{API}/jobs/{created.json()['job_id']}/candidates/c001/media").json()

    assert media["preview"]["status"] == "available"
    assert media["export"]["status"] == "available"


def test_a_running_job_always_has_progress(
    make_client: Callable[..., tuple[TestClient, MockService]],
) -> None:
    client, _ = make_client(synchronous=False, step_s=0.02)
    job_id = client.post(f"{API}/jobs", json={"vod": "9000000001"}).json()["job_id"]

    def check() -> bool:
        job = client.get(f"{API}/jobs/{job_id}").json()
        if job["state"] == "running":
            assert job["progress"] is not None
        return job["state"] == "done"

    wait_until(check)


def test_a_null_segments_request_dedupes_with_its_explicit_equivalent(
    make_client: Callable[..., tuple[TestClient, MockService]],
) -> None:
    client, _ = make_client(
        synchronous=False, step_s=0.05, highlights_render=FakeHighlightsRenderer(delay_s=0.5)
    )
    job_id = client.post(f"{API}/jobs", json={"vod": "9000000001"}).json()["job_id"]
    wait_until(lambda: client.get(f"{API}/jobs/{job_id}").json()["state"] == "done")
    explicit = [
        {"candidate_id": "c001", "start_s": 118.0, "end_s": 148.0},
        {"candidate_id": "c002", "start_s": 251.5, "end_s": 276.5},
        {"candidate_id": "c003", "start_s": 40.0, "end_s": 62.0},
    ]

    first = client.post(f"{API}/highlights", json={"job_id": job_id})
    second = client.post(f"{API}/highlights", json={"job_id": job_id, "segments": explicit})

    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json()["highlights_id"] == first.json()["highlights_id"]


def test_closing_drops_queued_work_and_returns_promptly(
    make_client: Callable[..., tuple[TestClient, MockService]],
) -> None:
    _, service = make_client(synchronous=False, step_s=0.2)
    from autoclipper.schemas import CreateJobRequest

    ids = [
        service.create_job(CreateJobRequest(vod=str(9000000100 + i)))[0].job_id for i in range(10)
    ]
    started = time.monotonic()

    service.close()

    assert time.monotonic() - started < 3.0
    assert service.get_job(ids[-1]).state == "queued"  # never started


@pytest.mark.ffmpeg
def test_the_real_probe_accepts_videos_and_rejects_audio_only_files(
    make_client: Callable[..., tuple[TestClient, MockService]],
    make_test_media: Callable[..., Path],
) -> None:
    client, _ = make_client(probe_video=None)
    video = make_test_media("vod.mp4")
    audio = make_test_media("tone.wav", video_size=None)

    ok = client.post(f"{API}/jobs", json={"vod": "9000000001", "video_path": str(video)})
    rejected = client.post(f"{API}/jobs", json={"vod": "9000000001", "video_path": str(audio)})

    assert ok.status_code == 201
    assert error_code(rejected) == "invalid_video_path"


def test_serve_starts_uvicorn_with_the_mock_app(
    monkeypatch: pytest.MonkeyPatch, samples: SampleMedia
) -> None:
    import uvicorn
    from typer.testing import CliRunner

    from autoclipper.api import service as service_module
    from autoclipper.cli import app as cli

    calls: list[dict[str, object]] = []
    monkeypatch.setattr(service_module, "ensure_sample_media", lambda directory: samples)

    def run(api: object, **kwargs: object) -> None:
        calls.append({"api": api, **kwargs})
        with TestClient(api):  # type: ignore[arg-type]  # runs the lifespan, stops the workers
            pass

    monkeypatch.setattr(uvicorn, "run", run)

    result = CliRunner().invoke(cli, ["serve", "--jobs", "mock", "--port", "8123"])

    assert result.exit_code == 0, result.output
    assert calls[0]["host"] == "127.0.0.1"
    assert calls[0]["port"] == 8123


def test_serve_starts_the_pipeline_app_and_names_missing_credentials(
    monkeypatch: pytest.MonkeyPatch, samples: SampleMedia
) -> None:
    import uvicorn
    from typer.testing import CliRunner

    from autoclipper.api import service as service_module
    from autoclipper.cli import app as cli

    monkeypatch.setattr(service_module, "ensure_sample_media", lambda directory: samples)
    preflight_seen: list[dict[str, Any]] = []

    def run(api: object, **kwargs: object) -> None:
        with TestClient(api) as http:  # type: ignore[arg-type]  # runs the lifespan, stops the workers
            preflight_seen.append(http.get(f"{API}/preflight").json())

    monkeypatch.setattr(uvicorn, "run", run)
    lexicon = Path(__file__).parents[2] / "configs" / "lexicon.yaml"

    result = CliRunner().invoke(cli, ["serve", "--lexicon", str(lexicon)])

    assert result.exit_code == 0, result.output
    assert "Preflight checks:" in result.output
    assert "pipeline" in result.output  # the default runner
    assert "TWITCH_CLIENT_ID" in result.output  # the missing-credentials note
    assert "(mock jobs) on http" not in result.output  # no longer branded mock-only
    assert len(preflight_seen) == 1
    assert preflight_seen[0]["status"] == "fail"
    by_name = {c["name"]: c["status"] for c in preflight_seen[0]["checks"]}
    assert by_name["twitch_credentials"] == "fail"
    assert by_name["chat_hash_salt"] == "warn"


# --- Pipeline jobs (#45) ------------------------------------------------------------------------


LEXICON_PATH = Path(__file__).parents[2] / "configs" / "lexicon.yaml"
PIPELINE_VIDEO_ID = "9000000100"  # the chat fixture's VOD


class PipelineProbe:
    """A fake `analyze_vod` that drives the service's stage callbacks like the real one.

    After each stage it reads the job record the service updates, so the test sees the
    progress the API would report while the job runs.
    """

    def __init__(self, result: AnalysisResult, error: AnalyzeError | None = None) -> None:
        self.result = result
        self.error = error
        self.calls: list[dict[str, object]] = []
        self.service: MockService | None = None

    def __call__(
        self,
        video_id: str,
        *,
        data_dir: Path,
        artifacts_dir: Path,
        tools: IngestTools,
        lexicon: Lexicon,
        video_file: Path | None,
        baseline: str,
        clock: Callable[[], datetime],
        on_stage: Callable[[str], None],
        on_warnings: Callable[[Sequence[AnalysisWarning]], None],
    ) -> AnalysisResult:
        del lexicon, clock  # unused by the fake; the call records the rest
        assert self.service is not None
        call: dict[str, object] = {
            "video_id": video_id,
            "data_dir": data_dir,
            "artifacts_dir": artifacts_dir,
            "tools": tools,
            "video_file": video_file,
            "baseline": baseline,
        }
        self.calls.append(call)
        (record,) = list(self.service._jobs.values())
        seen: list[tuple[str, int]] = []
        for index, (name, _) in enumerate(mock.JOB_STAGES, start=1):
            on_stage(name)
            assert record.progress is not None
            seen.append((record.progress.stage, record.progress.stage_index))
            if self.error is not None and index == 2:  # fail in the stage it reached
                call["progress_seen"] = seen
                raise self.error
        call["progress_seen"] = seen
        on_warnings((mock.CHAT_MISSING,))
        call["warnings_seen"] = record.warnings
        return self.result


def _scenario_result(video_id: str) -> AnalysisResult:
    """A done scenario's result, asserted non-None for the type checker."""
    scenario = mock.scenario_for(video_id)
    assert scenario.result is not None
    return scenario.result


def pipeline_client(
    make_client: Callable[..., tuple[TestClient, MockService]],
    probe: PipelineProbe,
    tmp_path: Path,
) -> tuple[TestClient, MockService]:
    client, service = make_client(
        job_runner="pipeline",
        data_dir=tmp_path / "data",
        artifacts_dir=tmp_path / "artifacts",
        lexicon=load_lexicon(LEXICON_PATH),
        tools_for=IngestTools,
        analyze=probe,
        segment_fetcher=unused_fetcher,
    )
    probe.service = service
    return client, service


def unused_fetcher(video_id: str, begin_s: float, end_s: float, /) -> VideoSegment:
    """A fetcher for tests that never touch media; a call means the test went wrong."""
    raise AssertionError(f"no segment should be fetched here ({video_id} {begin_s}-{end_s})")


def test_the_health_endpoint_reports_the_pipeline_runner(
    make_client: Callable[..., tuple[TestClient, MockService]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("autoclipper.api.service.captions_available", lambda: False)
    probe = PipelineProbe(_scenario_result("9000000001"))
    client, _ = pipeline_client(make_client, probe, tmp_path)

    response = client.get(f"{API}/health")

    assert response.json() == {
        "status": "ok",
        "schema_version": "1.1",
        "job_runner": "pipeline",
        "captions_available": False,
    }


def test_a_pipeline_job_reports_each_stage_and_finishes(
    make_client: Callable[..., tuple[TestClient, MockService]], tmp_path: Path
) -> None:
    result = _scenario_result("9000000001").model_copy(update={"warnings": (mock.CHAT_MISSING,)})
    probe = PipelineProbe(result)
    client, _ = pipeline_client(make_client, probe, tmp_path)

    job = done_job(client, PIPELINE_VIDEO_ID)

    assert job["state"] == "done"
    (call,) = probe.calls
    assert call["video_id"] == PIPELINE_VIDEO_ID
    assert call["data_dir"] == tmp_path / "data"
    assert call["artifacts_dir"] == tmp_path / "artifacts"
    assert call["video_file"] is None
    assert call["baseline"] == "chat_loudness"
    assert isinstance(call["tools"], IngestTools)  # the service builds the tools per job
    assert call["progress_seen"] == [
        (name, index) for index, (name, _) in enumerate(mock.JOB_STAGES, start=1)
    ]
    assert call["warnings_seen"] == (mock.CHAT_MISSING,)
    assert job["progress"] is None
    assert job["error"] is None
    assert [warning["code"] for warning in job["warnings"]] == ["chat_missing"]
    served = client.get(f"{API}/jobs/{job['job_id']}/result").json()
    assert served == json.loads(result.model_dump_json())  # both as the wire format


def test_a_pipeline_failure_keeps_the_contract_code_and_its_last_stage(
    make_client: Callable[..., tuple[TestClient, MockService]], tmp_path: Path
) -> None:
    expired = AnalyzeError(
        "vod_unavailable", "Twitch has no published video with this ID (it may have expired)"
    )
    probe = PipelineProbe(_scenario_result("9000000001"), error=expired)
    client, _ = pipeline_client(make_client, probe, tmp_path)

    job = done_job(client, PIPELINE_VIDEO_ID)

    assert job["state"] == "failed"
    assert job["error"] == {
        "code": "vod_unavailable",
        "message": "Twitch has no published video with this ID (it may have expired)",
        "details": None,
    }
    assert job["progress"] is not None  # failed keeps the last value (§ 3.4)
    assert job["progress"]["stage"] == "fetching_clips"
    assert job["progress"]["stage_index"] == 2


def test_the_pipeline_receives_the_jobs_video_path(
    make_client: Callable[..., tuple[TestClient, MockService]], tmp_path: Path
) -> None:
    probe = PipelineProbe(_scenario_result("9000000001"))
    client, _ = pipeline_client(make_client, probe, tmp_path)
    video = tmp_path / "vod.mp4"
    video.write_bytes(b"x")

    created = client.post(f"{API}/jobs", json={"vod": PIPELINE_VIDEO_ID, "video_path": str(video)})

    assert created.status_code == 201
    (call,) = probe.calls
    assert call["video_file"] == video


def test_pipeline_jobs_need_their_configuration(samples: SampleMedia, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="lexicon"):
        MockService(
            samples=samples,
            output_dir=tmp_path / "artifacts",
            step_s=0.0,
            synchronous=True,
            job_runner="pipeline",
            data_dir=tmp_path / "data",
            artifacts_dir=tmp_path / "artifacts",
            tools_for=IngestTools,
        )


# The fakes for the real-pipeline test: a scripted Helix, the chat fixture as the tool's
# output, and a downloader that copies a generated audio file into place.


class FakeHelix:
    """Answers the token, Get Users, Get Videos and Get Clips requests.

    `vod_type` plays the video's kind: `archive` (a past broadcast, analyzable) or anything
    else (e.g. `highlight`), which the pipeline rejects as `vod_unsupported`.
    """

    def __init__(
        self,
        vod_type: str = "archive",
        *,
        user_login: str = "streamer_000",
        user_display_name: str = "Streamer_000",
    ) -> None:
        self.vod_type = vod_type
        self.user_login = user_login
        self.user_display_name = user_display_name

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "id.twitch.tv":
            return httpx.Response(
                200,
                json={"access_token": "fake-token", "expires_in": 3600, "token_type": "bearer"},
            )
        if request.url.path == "/helix/users":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "9000000000",
                            "login": self.user_login,
                            "display_name": self.user_display_name,
                            "broadcaster_type": "partner",
                        }
                    ]
                },
            )
        if request.url.path == "/helix/videos":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": PIPELINE_VIDEO_ID,
                            "stream_id": "9200000100",
                            "user_id": "9000000000",
                            "user_login": "streamer_000",
                            "user_name": "Streamer_000",
                            "title": "Synthetic test stream",
                            "description": "",
                            "created_at": "2026-10-01T18:00:00Z",
                            "published_at": "2026-10-01T18:00:00Z",
                            "url": f"https://www.twitch.tv/videos/{PIPELINE_VIDEO_ID}",
                            "thumbnail_url": "",
                            "viewable": "public",
                            "view_count": 10,
                            "language": "en",
                            "type": self.vod_type,
                            "duration": "1h0m0s",
                            "muted_segments": [{"duration": 30, "offset": 120}],
                        }
                    ],
                    "pagination": {},
                },
            )
        assert request.url.path == "/helix/clips"
        clips = [
            {
                "id": f"SyntheticClip{index:03d}",
                "broadcaster_id": "9000000000",
                "creator_id": f"{9300000000 + index}",
                "video_id": PIPELINE_VIDEO_ID,
                "game_id": "509658",
                "language": "en",
                "title": f"synthetic clip {index}",
                "view_count": 10 * index,
                "created_at": "2026-10-01T18:01:00Z",
                "duration": 30.0,
                "vod_offset": 60 * index,
                "is_featured": False,
            }
            for index in (1, 2)
        ]
        return httpx.Response(200, json={"data": clips, "pagination": {}})


class FakeChatTool:
    """Plays TwitchDownloaderCLI: writes the chat fixture to --output."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, args: Sequence[str], *, timeout_s: float | None = None) -> "CommandResult":
        del timeout_s  # the fake plays a successful run; the bound is not its business
        self.calls += 1
        Path(args[args.index("--output") + 1]).write_bytes(CHAT_FIXTURE.read_bytes())
        return CommandResult(0, BANNER, "")


class FakeFileDownloader:
    """Copies a generated audio file into place like yt-dlp."""

    def __init__(self, source: Path) -> None:
        self.source = source

    def __call__(self, request: DownloadRequest, /) -> DownloadedMedia:
        path = request.output_dir / f"{request.stem}.m4a"
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self.source, path)
        return DownloadedMedia(path, 3.0)


CHAT_FIXTURE = Path(__file__).parents[1] / "ingest" / "fixtures" / "chat_replay.json"
BANNER = f"TwitchDownloaderCLI {PINNED_TOOL_VERSION} Copyright (c) lay295 and contributors\n"


class FakeSegmentFetcher:
    """Plays the segment fetcher (#46): copies a generated video per requested range.

    `gate`, when set, parks the fetch until the test releases it, so a `preparing` response
    is observed deterministically; `gate_span` narrows that to one range, so a render's own
    fetch of a different span proceeds. `duration_s` is the "probed" length the fake reports;
    None means the requested length.
    """

    def __init__(
        self,
        source: Path,
        output_dir: Path,
        *,
        duration_s: float | None = None,
        error: Exception | None = None,
        gate: threading.Event | None = None,
        gate_span: tuple[float, float] | None = None,
    ) -> None:
        self.source = source
        self.output_dir = output_dir
        self.duration_s = duration_s
        self.error = error
        self.gate = gate
        self.gate_span = gate_span
        self.calls: list[tuple[str, float, float]] = []

    def __call__(self, video_id: str, begin_s: float, end_s: float, /) -> VideoSegment:
        self.calls.append((video_id, begin_s, end_s))
        if self.gate is not None and (self.gate_span is None or (begin_s, end_s) == self.gate_span):
            self.gate.wait(timeout=10.0)
        if self.error is not None:
            raise self.error
        directory = self.output_dir / video_id
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"segment-{begin_s:g}-{end_s:g}.mp4"
        shutil.copyfile(self.source, path)
        played_s = self.duration_s if self.duration_s is not None else end_s - begin_s
        return VideoSegment(path, begin_s, begin_s + played_s)


@pytest.mark.ffmpeg
@pytest.mark.parametrize("silent_upload", [False, True])
def test_a_real_pipeline_job_from_the_api(
    make_client: Callable[..., tuple[TestClient, MockService]],
    make_test_media: Callable[..., Path],
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    silent_upload: bool,
) -> None:
    """The worker runs the real `analyze_vod` against fake Twitch and chat tools, end to
    end: one POST produces a genuinely computed result with the § 3.5 warnings."""
    chat_binary = tmp_path / "bin" / "TwitchDownloaderCLI.exe"
    chat_binary.parent.mkdir(parents=True)
    chat_binary.write_bytes(b"")
    chat_binary.chmod(0o755)  # shutil.which requires the execute bit on POSIX
    audio = make_test_media("source.m4a", duration_s=3.0, video_size=None)
    credentials = TwitchCredentials(SecretStr("fake-client-id"), SecretStr("fake-client-secret"))
    clock = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)

    def tools_for() -> IngestTools:
        http = httpx.Client(transport=httpx.MockTransport(FakeHelix()))
        return IngestTools(
            helix=HelixClient(credentials, http_client=http, clock=lambda: clock),
            salt=SecretStr("fake-salt-5b1e"),
            chat_runner=FakeChatTool(),
            chat_binary=chat_binary,
            downloader=FakeFileDownloader(audio),
        )

    client, service = make_client(
        job_runner="pipeline",
        data_dir=tmp_path / "data",
        artifacts_dir=tmp_path / "artifacts",
        lexicon=load_lexicon(LEXICON_PATH),
        tools_for=tools_for,
        segment_fetcher=unused_fetcher,
        probe_video=None,
    )  # analyze: the real `analyze_vod`

    upload_id = None
    if silent_upload:
        video = tmp_path / "silent.mp4"
        run_command(
            ffmpeg_command(
                "-f",
                "lavfi",
                "-i",
                "color=size=32x32:rate=1:duration=3600",
                "-an",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                video,
            ),
            timeout_s=30.0,
        )
        upload = client.post(f"{API}/uploads?filename=silent.mp4", content=video.read_bytes())
        assert upload.status_code == 201, upload.text
        upload_id = upload.json()["upload_id"]
        created = client.post(
            f"{API}/jobs", json={"vod": PIPELINE_VIDEO_ID, "upload_id": upload_id}
        )
        assert created.status_code == 201, created.text
        job = client.get(f"{API}/jobs/{created.json()['job_id']}").json()
    else:
        job = done_job(client, PIPELINE_VIDEO_ID)

    assert job["state"] == "done", job["error"]
    result_response = client.get(f"{API}/jobs/{job['job_id']}/result")
    assert result_response.status_code == 200, result_response.text
    served = result_response.json()
    if upload_id is not None:
        assert [warning["code"] for warning in job["warnings"]] == [
            "audio_missing",
            "audio_partly_muted",
        ]
        assert f"{service.uploads_dir / f'{upload_id}.mp4'} has no audio stream" in caplog.text
        assert upload_id not in json.dumps(job["warnings"])
        assert upload_id not in result_response.text
    else:
        assert [warning["code"] for warning in job["warnings"]] == ["audio_partly_muted"]
    assert served["warnings"] == job["warnings"]
    assert served["vod"]["video_id"] == PIPELINE_VIDEO_ID
    assert served["vod"]["broadcaster_display_name"] == "Streamer_000"
    assert served["scorer"] == {
        "kind": "heuristic",
        "name": "chat_spike" if silent_upload else "chat_loudness",
        "run_id": None,
    }
    assert served["n_viewer_clips_total"] == 2
    assert served["candidates"], "the planted signals should rank"
    # The pipeline also wrote the result file the highlights command reads (#71).
    written = json.loads(
        (tmp_path / "artifacts" / "analysis" / f"{PIPELINE_VIDEO_ID}.json").read_text(
            encoding="utf-8"
        )
    )
    assert served == written


def test_a_non_archive_vod_fails_where_it_was_rejected(
    make_client: Callable[..., tuple[TestClient, MockService]], tmp_path: Path
) -> None:
    """A highlight isn't an archive: the job fails with `vod_unsupported` while its
    progress still says `resolving_vod` (§ 3.4), and nothing was downloaded (§ 3.5)."""
    credentials = TwitchCredentials(SecretStr("fake-client-id"), SecretStr("fake-client-secret"))
    clock = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)

    def refuse_chat(args: Sequence[str], *, timeout_s: float | None = None) -> CommandResult:
        raise AssertionError("the chat step should never run for a rejected VOD")

    def refuse_download(request: DownloadRequest, /) -> DownloadedMedia:
        raise AssertionError("the audio step should never run for a rejected VOD")

    def tools_for() -> IngestTools:
        http = httpx.Client(transport=httpx.MockTransport(FakeHelix(vod_type="highlight")))
        return IngestTools(
            helix=HelixClient(credentials, http_client=http, clock=lambda: clock),
            salt=SecretStr("fake-salt-5b1e"),
            chat_runner=refuse_chat,
            downloader=refuse_download,
        )

    client, _ = make_client(
        job_runner="pipeline",
        data_dir=tmp_path / "data",
        artifacts_dir=tmp_path / "artifacts",
        lexicon=load_lexicon(LEXICON_PATH),
        tools_for=tools_for,
        segment_fetcher=unused_fetcher,
    )  # analyze: the real `analyze_vod`

    job = done_job(client, PIPELINE_VIDEO_ID)

    assert job["state"] == "failed"
    assert job["error"]["code"] == "vod_unsupported"
    assert job["progress"] is not None  # failed keeps the last value (§ 3.4)
    assert job["progress"]["stage"] == "resolving_vod"
    assert job["progress"]["stage_index"] == 1
    assert job["warnings"] == []


@pytest.mark.ffmpeg
def test_a_video_mismatch_api_error_omits_local_file_path_while_logging_it(
    make_client: Callable[..., tuple[TestClient, MockService]],
    make_test_media: Callable[..., Path],
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """`Job.error.message` never leaks local filesystem paths (api-contract.md § 1), while server
    logs keep the full path for diagnostics."""
    import logging

    short_video = make_test_media("secret-folder/wrong-length.mp4", duration_s=5.0).resolve()
    credentials = TwitchCredentials(SecretStr("fake-client-id"), SecretStr("fake-client-secret"))
    clock = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)

    def tools_for() -> IngestTools:
        http = httpx.Client(transport=httpx.MockTransport(FakeHelix()))
        return IngestTools(
            helix=HelixClient(credentials, http_client=http, clock=lambda: clock),
            salt=SecretStr("fake-salt-5b1e"),
        )

    client, _ = make_client(
        job_runner="pipeline",
        data_dir=tmp_path / "data",
        artifacts_dir=tmp_path / "artifacts",
        lexicon=load_lexicon(LEXICON_PATH),
        tools_for=tools_for,
        segment_fetcher=unused_fetcher,
    )
    caplog.set_level(logging.WARNING)

    created = client.post(
        f"{API}/jobs", json={"vod": PIPELINE_VIDEO_ID, "video_path": str(short_video)}
    )
    assert created.status_code == 201, created.text
    job = client.get(f"{API}/jobs/{created.json()['job_id']}").json()

    assert job["state"] == "failed"
    assert job["error"]["code"] == "video_mismatch"
    assert str(short_video) not in job["error"]["message"]
    assert "secret-folder" not in job["error"]["message"]
    assert short_video.name not in job["error"]["message"]
    assert str(short_video) in caplog.text


def test_an_expired_vod_fails_where_the_audio_stopped_and_keeps_found_warnings(
    make_client: Callable[..., tuple[TestClient, MockService]], tmp_path: Path
) -> None:
    """A refused media download fails the job at `downloading_audio` (§ 3.4), and the
    earlier chat failure is already on the job as a warning found so far (§ 3.5)."""
    chat_binary = tmp_path / "bin" / "TwitchDownloaderCLI.exe"
    chat_binary.parent.mkdir(parents=True)
    chat_binary.write_bytes(b"")
    chat_binary.chmod(0o755)  # shutil.which requires the execute bit on POSIX
    credentials = TwitchCredentials(SecretStr("fake-client-id"), SecretStr("fake-client-secret"))
    clock = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)

    def failing_chat(args: Sequence[str], *, timeout_s: float | None = None) -> CommandResult:
        del timeout_s  # the fake plays a failing tool; the bound is not its business
        return CommandResult(1, BANNER, "TwitchDownloaderCLI: the video does not exist")

    def refused_download(request: DownloadRequest, /) -> DownloadedMedia:
        del request  # the refusal below is the point, not the request
        raise VodAccessRestrictedError(PIPELINE_VIDEO_ID, "the video is not publicly accessible")

    def tools_for() -> IngestTools:
        http = httpx.Client(transport=httpx.MockTransport(FakeHelix()))
        return IngestTools(
            helix=HelixClient(credentials, http_client=http, clock=lambda: clock),
            salt=SecretStr("fake-salt-5b1e"),
            chat_runner=failing_chat,
            chat_binary=chat_binary,
            downloader=refused_download,
        )

    client, _ = make_client(
        job_runner="pipeline",
        data_dir=tmp_path / "data",
        artifacts_dir=tmp_path / "artifacts",
        lexicon=load_lexicon(LEXICON_PATH),
        tools_for=tools_for,
        segment_fetcher=unused_fetcher,
    )  # analyze: the real `analyze_vod`

    job = done_job(client, PIPELINE_VIDEO_ID)

    assert job["state"] == "failed"
    assert job["error"]["code"] == "vod_unavailable"
    assert job["progress"] is not None  # failed keeps the last value (§ 3.4)
    assert job["progress"]["stage"] == "downloading_audio"
    assert job["progress"]["stage_index"] == 4
    assert [warning["code"] for warning in job["warnings"]] == ["chat_missing", "audio_missing"]

    # The same request again runs against the cached catalog: the refused audio step is
    # skipped (it publishes nothing itself), so the classification must still deliver
    # `audio_missing` before the run fails (§ 3.5's "found so far").
    again = done_job(client, PIPELINE_VIDEO_ID)

    assert again["state"] == "failed"
    assert again["error"]["code"] == "vod_unavailable"
    assert again["progress"] is not None
    assert again["progress"]["stage"] == "downloading_audio"
    assert again["progress"]["stage_index"] == 4
    assert [warning["code"] for warning in again["warnings"]] == ["chat_missing", "audio_missing"]


# --- Downloaded video media (#46) ----------------------------------------------------------------


def segment_client(
    make_client: Callable[..., tuple[TestClient, MockService]],
    tmp_path: Path,
    fetcher: FakeSegmentFetcher,
    **kwargs: object,
) -> tuple[TestClient, MockService]:
    """A pipeline client whose jobs produce the first scenario's result, with a fake fetch."""
    probe = PipelineProbe(_scenario_result("9000000001"))
    client, service = make_client(
        job_runner="pipeline",
        data_dir=tmp_path / "data",
        artifacts_dir=tmp_path / "artifacts",
        lexicon=load_lexicon(LEXICON_PATH),
        tools_for=IngestTools,
        analyze=probe,
        segment_fetcher=fetcher,
        **kwargs,
    )
    probe.service = service
    return client, service


def wait_until_media(client: TestClient, job_id: str, candidate_id: str) -> dict[str, Any]:
    """Poll a candidate's media until it is no longer `preparing`."""
    deadline = time.monotonic() + 10.0
    media = client.get(f"{API}/jobs/{job_id}/candidates/{candidate_id}/media").json()
    while media["preview"]["status"] == "preparing":
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.01)
        media = client.get(f"{API}/jobs/{job_id}/candidates/{candidate_id}/media").json()
    return media


def test_a_pipeline_jobs_media_prepares_then_serves_the_segment(
    make_client: Callable[..., tuple[TestClient, MockService]],
    make_test_media: Callable[..., Path],
    tmp_path: Path,
) -> None:
    """A pipeline job without a video file downloads a segment around the candidate (#46):
    the first request reports `preparing`, later ones the served file and its coverage."""
    gate = threading.Event()
    video = make_test_media("vod.mp4", duration_s=90.0, video_size=(64, 36), frame_rate=5)
    fetcher = FakeSegmentFetcher(video, tmp_path / "raw" / "video", gate=gate)
    client, _ = segment_client(make_client, tmp_path, fetcher, synchronous=False, step_s=0.05)
    job = client.post(f"{API}/jobs", json={"vod": "9000000001"}).json()
    wait_until(lambda: client.get(f"{API}/jobs/{job['job_id']}").json()["state"] == "done")

    first = client.get(f"{API}/jobs/{job['job_id']}/candidates/c001/media").json()
    assert first["preview"]["status"] == "preparing"
    assert first["preview"]["message"] == "Fetching the video for this moment…"
    assert first["export"]["status"] == "available"  # a render fetches its own media (§ 5)
    assert first["export"]["min_start_s"] == 88.0  # the candidate (118-148) with its margin

    gate.set()
    media = wait_until_media(client, job["job_id"], "c001")
    assert media["preview"]["status"] == "available"
    assert media["preview"]["kind"] == "video_file"
    assert media["preview"]["media_start_s"] == 88.0
    assert media["preview"]["media_end_s"] == 178.0  # the reported probed coverage
    assert media["export"]["max_end_s"] == 178.0
    served = client.get(str(media["preview"]["url"]))
    assert served.status_code == 200
    assert served.headers["content-type"] == "video/mp4"
    assert served.content == video.read_bytes()
    (video_id, begin, end) = fetcher.calls[0]
    assert (video_id, begin, end) == ("9000000001", 88.0, 178.0)


def test_a_segment_fetch_failure_reports_the_contract_reasons(
    make_client: Callable[..., tuple[TestClient, MockService]],
    tmp_path: Path,
) -> None:
    """A refused video download is `source_unavailable` (§ 5.2): the media says why, and
    creating an export or a highlights video answers 409 `media_unavailable`."""
    video = tmp_path / "vod.mp4"
    video.write_bytes(b"never fetched")
    fetcher = FakeSegmentFetcher(
        video,
        tmp_path / "raw" / "video",
        error=VodAccessRestrictedError("9000000001", "the VOD is subscriber-only"),
    )
    client, _ = segment_client(make_client, tmp_path, fetcher)
    job = done_job(client)

    media = client.get(f"{API}/jobs/{job['job_id']}/candidates/c001/media").json()
    assert media["preview"]["status"] == "unavailable"
    assert media["preview"]["reason"] == "source_unavailable"
    assert media["export"]["status"] == "unavailable"
    assert media["export"]["layouts"] == []

    export = client.post(f"{API}/exports", json=export_body(job["job_id"]))
    highlights = client.post(f"{API}/highlights", json={"job_id": job["job_id"]})
    assert (export.status_code, error_code(export)) == (409, "media_unavailable")
    assert (highlights.status_code, error_code(highlights)) == (409, "media_unavailable")
    assert export.json()["error"]["details"] == {"reason": "source_unavailable"}


def test_an_export_cuts_from_the_downloaded_segment_with_shifted_times(
    make_client: Callable[..., tuple[TestClient, MockService]],
    make_test_media: Callable[..., Path],
    tmp_path: Path,
) -> None:
    """The render cuts the trim from the segment file, shifted into the file's own time, and
    a later export inside the same segment fetches nothing (#46)."""
    video = make_test_media("vod.mp4", duration_s=20.0)
    fetcher = FakeSegmentFetcher(video, tmp_path / "raw" / "video")
    renderer = FakeRenderer()
    client, _ = segment_client(make_client, tmp_path, fetcher, render=renderer)
    job = done_job(client)

    made = client.post(
        f"{API}/exports",
        json=export_body(job["job_id"], start_s=120.0, end_s=130.0),
    )
    again = client.post(
        f"{API}/exports",
        json=export_body(job["job_id"], start_s=125.0, end_s=135.0),
    )

    assert made.status_code == again.status_code == 201
    assert len(renderer.specs) == 2
    (spec,) = renderer.specs[:1]
    assert spec.source == fetcher.output_dir / "9000000001" / "segment-90-160.mp4"
    assert (spec.interval.start_s, spec.interval.end_s) == (30.0, 40.0)  # trim - segment start
    assert len(fetcher.calls) == 1  # the second trim fits the same segment
    assert fetcher.calls[0][1:] == (90.0, 160.0)  # the trim's span: 120-30 to 130+30


@pytest.mark.ffmpeg
def test_a_real_export_renders_from_a_fetched_segment(
    make_client: Callable[..., tuple[TestClient, MockService]],
    make_test_media: Callable[..., Path],
    tmp_path: Path,
) -> None:
    """End to end (#46): the segment is cut for real, so the trim lands on the right seconds
    of the right file and the finished clip is a playable 9:16 MP4."""
    video = make_test_media("vod.mp4", duration_s=40.0, video_size=(640, 360))
    fetcher = FakeSegmentFetcher(video, tmp_path / "raw" / "video")
    client, _ = segment_client(make_client, tmp_path, fetcher, render=_real_render())
    job = done_job(client)

    created = client.post(
        f"{API}/exports",
        json=export_body(job["job_id"], start_s=120.0, end_s=122.0, captions=False),
    ).json()

    export = client.get(f"{API}/exports/{created['export_id']}").json()
    assert export["state"] == "done", export["error"]
    file = client.get(export["download_url"])
    out = tmp_path / "download.mp4"
    out.write_bytes(file.content)
    info = probe(out)
    assert (info.video_streams[0].width, info.video_streams[0].height) == (1080, 1920)
    assert info.duration_s == pytest.approx(2.0, abs=0.05)


def test_a_highlights_video_fetches_one_span_and_shifts_all_segments(
    make_client: Callable[..., tuple[TestClient, MockService]],
    make_test_media: Callable[..., Path],
    tmp_path: Path,
) -> None:
    """The highlights video downloads one span covering every moment and shifts each cut
    into that file's own time (#46)."""
    video = make_test_media("vod.mp4", duration_s=20.0)
    fetcher = FakeSegmentFetcher(video, tmp_path / "raw" / "video")
    renderer = FakeHighlightsRenderer()
    client, _ = segment_client(make_client, tmp_path, fetcher, highlights_render=renderer)
    job = done_job(client)

    created = client.post(
        f"{API}/highlights",
        json={
            "job_id": job["job_id"],
            "segments": [
                {"candidate_id": "c001", "start_s": 118.0, "end_s": 148.0},
                {"candidate_id": "c003", "start_s": 40.0, "end_s": 62.0},
            ],
            "order": "chronological",
        },
    )

    assert created.status_code == 201
    (request,) = renderer.requests
    assert request.source == fetcher.output_dir / "9000000001" / "segment-10-178.mp4"
    assert [(s.candidate_id, s.start_s, s.end_s) for s in request.segments] == [
        ("c003", 30.0, 52.0),  # 40-62 shifted by the span's begin (10)
        ("c001", 108.0, 138.0),  # 118-148 shifted by 10
    ]
    video_done = client.get(f"{API}/highlights/{created.json()['highlights_id']}").json()
    assert video_done["state"] == "done", video_done["error"]


def test_highlights_fetches_per_moment_segments_and_merges_overlapping_spans(
    make_client: Callable[..., tuple[TestClient, MockService]],
    make_test_media: Callable[..., Path],
    tmp_path: Path,
) -> None:
    """Highlights fetch separate segments for distant moments instead of downloading the whole
    span between the first and last moment, while merging overlapping padded spans and keeping
    the requested segment order."""
    video = make_test_media("vod.mp4", duration_s=20.0)
    fetcher = FakeSegmentFetcher(video, tmp_path / "raw" / "video")
    renderer = FakeHighlightsRenderer()
    client, _ = segment_client(make_client, tmp_path, fetcher, highlights_render=renderer)
    job = done_job(client)

    # c003 (40-62 -> span 10-92) and c001 (118-148 -> span 88-178) overlap -> one fetch 10-178.
    # c002 (251.5-276.5 -> span 221.5-306.5) is separate -> its own fetch 221.5-306.5.
    # Rank order puts c001, c002, c003 to confirm ordering is preserved across merged groups.
    created = client.post(
        f"{API}/highlights",
        json={
            "job_id": job["job_id"],
            "segments": [
                {"candidate_id": "c001", "start_s": 118.0, "end_s": 148.0},
                {"candidate_id": "c002", "start_s": 251.5, "end_s": 276.5},
                {"candidate_id": "c003", "start_s": 40.0, "end_s": 62.0},
            ],
            "order": "rank",
        },
    )

    assert created.status_code == 201
    assert fetcher.calls == [
        ("9000000001", 10.0, 178.0),
        ("9000000001", 221.5, 306.5),
    ]
    (request,) = renderer.requests
    merged_file = fetcher.output_dir / "9000000001" / "segment-10-178.mp4"
    distant_file = fetcher.output_dir / "9000000001" / "segment-221.5-306.5.mp4"
    assert [(s.candidate_id, s.start_s, s.end_s, s.source) for s in request.segments] == [
        ("c001", 108.0, 138.0, merged_file),
        ("c002", 30.0, 55.0, distant_file),
        ("c003", 30.0, 52.0, merged_file),
    ]
    done = client.get(f"{API}/highlights/{created.json()['highlights_id']}").json()
    assert done["state"] == "done", done["error"]


def test_highlights_reuses_ready_segments_and_releases_leases_on_failure(
    make_client: Callable[..., tuple[TestClient, MockService]],
    make_test_media: Callable[..., Path],
    tmp_path: Path,
) -> None:
    """Highlights reuse already-downloaded candidate segments without re-fetching, and release
    every acquired lease if a later segment fetch fails."""
    from autoclipper.ingest.media import DownloadNetworkError

    video = make_test_media("vod.mp4", duration_s=90.0, video_size=(64, 36), frame_rate=5)
    fetcher = FakeSegmentFetcher(video, tmp_path / "raw" / "video")
    renderer = FakeHighlightsRenderer()
    client, service = segment_client(make_client, tmp_path, fetcher, highlights_render=renderer)
    job = done_job(client)

    # Preview c001 first so its segment (88-178) is already ready in service state.
    media = client.get(f"{API}/jobs/{job['job_id']}/candidates/c001/media").json()
    assert media["preview"]["status"] == "available"
    assert len(fetcher.calls) == 1

    # Highlights with c001 + c002 fetches only c002 (221.5-306.5), reusing c001's ready file.
    created = client.post(
        f"{API}/highlights",
        json={
            "job_id": job["job_id"],
            "segments": [
                {"candidate_id": "c001", "start_s": 118.0, "end_s": 148.0},
                {"candidate_id": "c002", "start_s": 251.5, "end_s": 276.5},
            ],
            "order": "chronological",
        },
    )
    assert created.status_code == 201
    assert fetcher.calls == [
        ("9000000001", 88.0, 178.0),
        ("9000000001", 221.5, 306.5),
    ]
    (request,) = renderer.requests
    assert request.segments[0].source == fetcher.output_dir / "9000000001" / "segment-88-178.mp4"
    assert request.segments[1].source == (
        fetcher.output_dir / "9000000001" / "segment-221.5-306.5.mp4"
    )
    assert not service._active_segment_paths

    # If a later fetch fails, the lease acquired on the reused c001 segment is still released.
    fetcher.error = DownloadNetworkError("9000000001", "connection reset")
    failed_req = client.post(
        f"{API}/highlights",
        json={
            "job_id": job["job_id"],
            "segments": [
                {"candidate_id": "c001", "start_s": 118.0, "end_s": 148.0},
                {"candidate_id": "c003", "start_s": 40.0, "end_s": 62.0},
            ],
            "order": "chronological",
        },
    )
    assert failed_req.status_code == 201
    failed = client.get(f"{API}/highlights/{failed_req.json()['highlights_id']}").json()
    assert failed["state"] == "failed"
    assert failed["error"]["details"] == {"reason": "fetch_failed"}
    assert not service._active_segment_paths


def test_highlights_refetches_deleted_cached_segment(
    make_client: Callable[..., tuple[TestClient, MockService]],
    make_test_media: Callable[..., Path],
    tmp_path: Path,
) -> None:
    """If a previously downloaded segment is deleted from disk while the server runs,
    highlights skip the missing path and re-fetch instead of failing with local_file_missing."""
    video = make_test_media("vod.mp4", duration_s=90.0, video_size=(64, 36), frame_rate=5)
    raw_dir = tmp_path / "raw" / "video"
    fetcher = FakeSegmentFetcher(video, raw_dir)
    renderer = FakeHighlightsRenderer()
    client, _ = segment_client(make_client, tmp_path, fetcher, highlights_render=renderer)
    job = done_job(client)

    media = client.get(f"{API}/jobs/{job['job_id']}/candidates/c001/media").json()
    assert media["preview"]["status"] == "available"
    cached_file = raw_dir / "9000000001" / "segment-88-178.mp4"
    cached_file.unlink()

    created = client.post(
        f"{API}/highlights",
        json={
            "job_id": job["job_id"],
            "segments": [{"candidate_id": "c001", "start_s": 118.0, "end_s": 148.0}],
            "order": "chronological",
        },
    )
    assert created.status_code == 201
    done = client.get(f"{API}/highlights/{created.json()['highlights_id']}").json()
    assert done["state"] == "done", done["error"]
    assert len(fetcher.calls) == 2
    assert cached_file.is_file()


@pytest.mark.ffmpeg
def test_highlights_skips_cached_segment_when_audio_ends_before_trim(
    make_client: Callable[..., tuple[TestClient, MockService]],
    make_test_media: Callable[..., Path],
    tmp_path: Path,
) -> None:
    """When a cached segment's audio stops before its video (and before the requested trim),
    highlights fetch a fresh padded span instead of failing inside render_highlights."""
    short_audio = tmp_path / "short-audio.mp4"
    run_command(
        ffmpeg_command(
            "-y",
            *("-f", "lavfi", "-i", "testsrc2=size=320x180:rate=25:duration=5"),
            *("-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=4.2"),
            *("-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac"),
            short_audio,
        ),
        timeout_s=120.0,
    )
    full_audio = make_test_media("full.mp4", duration_s=10.0)
    raw_dir = tmp_path / "raw" / "video"
    base = _scenario_result("9000000001")
    result = base.model_copy(
        update={
            "vod": base.vod.model_copy(update={"duration_s": 100.0}),
            "candidates": (base.candidates[0].model_copy(update={"start_s": 1.0, "end_s": 3.5}),),
        }
    )

    calls: list[tuple[str, float, float]] = []

    def fetcher(video_id: str, begin_s: float, end_s: float, /) -> VideoSegment:
        calls.append((video_id, begin_s, end_s))
        directory = raw_dir / video_id
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"segment-{round(begin_s)}-{round(end_s)}.mp4"
        if len(calls) == 1:
            shutil.copyfile(short_audio, path)
            return VideoSegment(path, 0.0, probe(path).duration_s)
        shutil.copyfile(full_audio, path)
        return VideoSegment(path, 0.0, probe(path).duration_s)

    probe_runner = PipelineProbe(result)
    client, service = make_client(
        job_runner="pipeline",
        data_dir=tmp_path / "data",
        artifacts_dir=tmp_path / "artifacts",
        lexicon=load_lexicon(LEXICON_PATH),
        tools_for=IngestTools,
        analyze=probe_runner,
        segment_fetcher=fetcher,
        highlights_render=render_highlights,
    )
    probe_runner.service = service
    job = done_job(client)

    # First preview caches short_audio (0..5 s container, audio ends at 4.2 s).
    media = client.get(f"{API}/jobs/{job['job_id']}/candidates/c001/media").json()
    assert media["preview"]["status"] == "available"
    assert len(calls) == 1

    # Highlights trim 1.0..4.8 falls inside 0..5.0 container duration, but past 4.2 s audio end:
    # must reject cache reuse and fetch a fresh padded span (call #2) so rendering succeeds.
    created = client.post(
        f"{API}/highlights",
        json={
            "job_id": job["job_id"],
            "segments": [{"candidate_id": "c001", "start_s": 1.0, "end_s": 4.8}],
            "order": "chronological",
        },
    )
    assert created.status_code == 201
    done = client.get(f"{API}/highlights/{created.json()['highlights_id']}").json()
    assert done["state"] == "done", done["error"]
    assert len(calls) == 2


def test_a_jobs_own_video_file_is_served_for_preview(
    client: TestClient,
    make_test_media: Callable[..., Path],
    tmp_path: Path,
    short_preview_scenario: None,
) -> None:
    """A job with a `video_path` previews that file, not the sample (§ 5: the server reads it
    and downloads no video)."""
    video = make_test_media("my vod.mp4", duration_s=3.0)
    job = client.post(f"{API}/jobs", json={"vod": "9000000001", "video_path": str(video)}).json()

    media = client.get(f"{API}/jobs/{job['job_id']}/candidates/c001/media").json()

    assert media["preview"]["status"] == "available"
    assert media["preview"]["media_start_s"] == 0.0
    served = client.get(str(media["preview"]["url"]))
    assert served.status_code == 200
    assert served.headers["content-type"] == "video/mp4"
    # Already playable, so the file itself is served without a copy.
    assert served.content == video.read_bytes()


@pytest.mark.ffmpeg
def test_a_jobs_own_video_file_that_cant_play_is_served_as_a_playable_copy(
    make_client: Callable[..., tuple[TestClient, MockService]],
    tmp_path: Path,
    short_preview_scenario: None,
) -> None:
    """The preview URL must be playable in a browser (§ 5.1): a file that FFmpeg reads but a
    browser can't (MPEG-4 in an AVI container) is served as a prepared H.264/AAC MP4, while
    renders keep cutting the original (#46)."""
    source = tmp_path / "old capture.avi"
    run_command(
        ffmpeg_command(
            "-y",
            *("-f", "lavfi", "-i", "testsrc2=size=320x180:rate=10:duration=3"),
            *("-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=3"),
            *("-c:v", "mpeg4", "-c:a", "mp3", "-shortest", source),
        ),
        timeout_s=60,
    )
    client, _ = make_client(probe_video=lambda path: path.suffix in {".mp4", ".avi"})
    job = client.post(f"{API}/jobs", json={"vod": "9000000001", "video_path": str(source)}).json()

    media = client.get(f"{API}/jobs/{job['job_id']}/candidates/c001/media").json()

    assert media["preview"]["status"] == "available"
    served = client.get(str(media["preview"]["url"]))
    out = tmp_path / "served.mp4"
    out.write_bytes(served.content)
    info = probe(out)
    assert info.video_streams[0].codec_name == "h264"
    assert info.audio_streams[0].codec_name == "aac"
    assert info.duration_s == pytest.approx(3.0, abs=0.2)


def test_a_late_preview_failure_never_disables_usable_media(
    make_client: Callable[..., tuple[TestClient, MockService]],
    make_test_media: Callable[..., Path],
    tmp_path: Path,
) -> None:
    """A render's segment survives the preview's own fetch failing afterwards (#46): usable
    media is never overwritten by a late failure."""
    video = make_test_media("vod.mp4", duration_s=70.0, video_size=(64, 36), frame_rate=5)
    gate = threading.Event()
    planned = FakeSegmentFetcher(
        video, tmp_path / "raw" / "video", gate=gate, gate_span=(88.0, 178.0)
    )
    renderer = FakeRenderer()
    client, service = segment_client(
        make_client, tmp_path, planned, render=renderer, synchronous=False, step_s=0.05
    )
    job = client.post(f"{API}/jobs", json={"vod": "9000000001"}).json()
    wait_until(lambda: client.get(f"{API}/jobs/{job['job_id']}").json()["state"] == "done")

    # The preview's planned-span fetch parks (the worker sleeps on the gate).
    client.get(f"{API}/jobs/{job['job_id']}/candidates/c001/media").json()
    # A render meanwhile fetches its own (different) span and installs usable media.
    made = client.post(
        f"{API}/exports", json=export_body(job["job_id"], start_s=120.0, end_s=130.0)
    )
    assert made.status_code == 201
    wait_until(
        lambda: client.get(f"{API}/exports/{made.json()['export_id']}").json()["state"] == "done"
    )

    # The parked preview fetch now fails; the render's segment must survive. `_fetches_done`
    # is the explicit completion signal: the background fetch has finished AND applied its
    # state update before we assert.
    planned.error = VodAccessRestrictedError("9000000001", "subscriber-only")
    gate.set()
    wait_until(lambda: service._fetches_done >= 1)

    media = wait_until_media(client, job["job_id"], "c001")
    assert media["preview"]["status"] == "available"
    assert media["preview"]["media_end_s"] == pytest.approx(160.0)  # the render's span
    assert media["export"]["max_end_s"] == pytest.approx(160.0)


def test_a_trims_segment_never_replaces_the_candidates_preview(
    make_client: Callable[..., tuple[TestClient, MockService]],
    make_test_media: Callable[..., Path],
    tmp_path: Path,
) -> None:
    """A trim whose span doesn't cover the candidate renders fine but never becomes the
    candidate's preview (#46, § 5: coverage must contain the candidate's own bounds)."""
    video = make_test_media("vod.mp4", duration_s=90.0, video_size=(64, 36), frame_rate=5)
    gate = threading.Event()
    fetcher = FakeSegmentFetcher(
        video, tmp_path / "raw" / "video", gate=gate, gate_span=(88.0, 178.0)
    )
    renderer = FakeRenderer()
    client, _ = segment_client(
        make_client, tmp_path, fetcher, render=renderer, synchronous=False, step_s=0.05
    )
    job = client.post(f"{API}/jobs", json={"vod": "9000000001"}).json()
    wait_until(lambda: client.get(f"{API}/jobs/{job['job_id']}").json()["state"] == "done")

    # The preview's planned fetch (88-178) parks on the gate.
    client.get(f"{API}/jobs/{job['job_id']}/candidates/c001/media").json()
    # A trim at the very start of the advertised bounds: its span (58-120) excludes the
    # candidate's last 28 seconds, so it may not become the preview.
    made = client.post(f"{API}/exports", json=export_body(job["job_id"], start_s=88.0, end_s=90.0))
    assert made.status_code == 201
    wait_until(
        lambda: client.get(f"{API}/exports/{made.json()['export_id']}").json()["state"] == "done"
    )
    (spec,) = renderer.specs
    assert (spec.interval.start_s, spec.interval.end_s) == (30.0, 32.0)  # shifted by 58

    # The preview still reports its planned span, not the trim's narrow one.
    media = client.get(f"{API}/jobs/{job['job_id']}/candidates/c001/media").json()
    assert media["preview"]["status"] == "preparing"
    assert media["export"]["min_start_s"] == 88.0

    # The planned fetch lands: coverage keeps containing the candidate.
    gate.set()
    media = wait_until_media(client, job["job_id"], "c001")
    assert media["preview"]["status"] == "available"
    assert media["preview"]["media_start_s"] == 88.0
    assert media["preview"]["media_end_s"] == pytest.approx(178.0)
    assert media["preview"]["media_start_s"] <= 118.0 < 148.0 <= media["preview"]["media_end_s"]


@pytest.mark.ffmpeg
def test_a_non_420_h264_preview_is_transcoded_to_yuv420p(
    make_client: Callable[..., tuple[TestClient, MockService]],
    tmp_path: Path,
    short_preview_scenario: None,
) -> None:
    """An H.264 MP4 with 4:4:4 chroma is transcoded to yuv420p for browser playback (#46)."""
    source = tmp_path / "chroma444.mp4"
    run_command(
        ffmpeg_command(
            "-y",
            *("-f", "lavfi", "-i", "testsrc2=size=320x180:rate=10:duration=2"),
            *("-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=2"),
            *("-c:v", "libx264", "-pix_fmt", "yuv444p", "-c:a", "aac", "-shortest", source),
        ),
        timeout_s=60,
    )
    assert probe(source).video_streams[0].pix_fmt == "yuv444p"
    client, _ = make_client(probe_video=lambda path: path.suffix == ".mp4")
    job = client.post(f"{API}/jobs", json={"vod": "9000000001", "video_path": str(source)}).json()

    media = client.get(f"{API}/jobs/{job['job_id']}/candidates/c001/media").json()

    assert media["preview"]["status"] == "available"
    served = client.get(str(media["preview"]["url"]))
    out = tmp_path / "served.mp4"
    out.write_bytes(served.content)
    info = probe(out)
    assert info.video_streams[0].codec_name == "h264"
    assert info.video_streams[0].pix_fmt == "yuv420p"
    assert info.audio_streams[0].codec_name == "aac"


def test_a_segment_whose_preview_cant_be_prepared_still_exports(
    make_client: Callable[..., tuple[TestClient, MockService]],
    tmp_path: Path,
) -> None:
    """When preparing a browser-playable copy of a segment fails, preview is unavailable
    instead of serving unverified bytes, while exports remain available (#46)."""
    unreadable = tmp_path / "not-a-video.mp4"
    unreadable.write_bytes(b"corrupt")
    fetcher = FakeSegmentFetcher(unreadable, tmp_path / "raw" / "video")
    renderer = FakeRenderer()
    client, _ = segment_client(make_client, tmp_path, fetcher, render=renderer)
    job = done_job(client)

    media = client.get(f"{API}/jobs/{job['job_id']}/candidates/c001/media").json()
    assert media["preview"]["status"] == "unavailable"
    assert media["preview"]["reason"] == "fetch_failed"
    assert media["preview"]["url"] is None
    assert media["export"]["status"] == "available"

    made = client.post(f"{API}/exports", json=export_body(job["job_id"]))
    assert made.status_code == 201
    assert client.get(f"{API}/exports/{made.json()['export_id']}").json()["state"] == "done"


def test_a_downloaded_segments_failed_preview_preparation_is_retryable(
    make_client: Callable[..., tuple[TestClient, MockService]],
    make_test_media: Callable[..., Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If a segment downloads fine but preparing its preview fails transiently, a later media
    request retries preview preparation without re-downloading or losing export support."""
    from autoclipper.api import service as service_mod

    video = make_test_media("vod.mp4", duration_s=90.0, video_size=(64, 36), frame_rate=5)
    fetcher = FakeSegmentFetcher(video, tmp_path / "raw" / "video")
    real_copy = service_mod._playable_copy
    attempts = 0

    def flaky_copy(source: Path, target: Path) -> Path:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("temporary disk full")
        return real_copy(source, target)

    monkeypatch.setattr(service_mod, "_playable_copy", flaky_copy)
    client, _ = segment_client(make_client, tmp_path, fetcher)
    job = done_job(client)

    first = client.get(f"{API}/jobs/{job['job_id']}/candidates/c001/media").json()
    assert first["preview"]["status"] == "unavailable"
    assert first["preview"]["reason"] == "fetch_failed"
    assert first["export"]["status"] == "available"
    assert len(fetcher.calls) == 1

    # Retrying recovers the preview without downloading the segment a second time.
    retried = client.get(f"{API}/jobs/{job['job_id']}/candidates/c001/media").json()
    assert retried["preview"]["status"] == "available"
    assert len(fetcher.calls) == 1
    assert attempts == 2


def test_a_transient_segment_fetch_failure_is_retryable(
    make_client: Callable[..., tuple[TestClient, MockService]],
    make_test_media: Callable[..., Path],
    tmp_path: Path,
) -> None:
    """Transient `fetch_failed` errors aren't cached forever: retrying media or export fetches
    again and succeeds once the network recovers (#46 follow-up)."""
    from autoclipper.ingest.media import DownloadNetworkError

    video = make_test_media("vod.mp4", duration_s=90.0, video_size=(64, 36), frame_rate=5)
    fetcher = FakeSegmentFetcher(
        video,
        tmp_path / "raw" / "video",
        error=DownloadNetworkError("9000000001", "connection reset"),
    )
    renderer = FakeRenderer()
    client, _ = segment_client(make_client, tmp_path, fetcher, render=renderer)
    job = done_job(client)

    first = client.get(f"{API}/jobs/{job['job_id']}/candidates/c001/media").json()
    assert first["preview"]["status"] == "unavailable"
    assert first["preview"]["reason"] == "fetch_failed"
    assert first["export"]["status"] == "available"

    # The transient error clears: the very next media request retries instead of staying stuck.
    fetcher.error = None
    retried = client.get(f"{API}/jobs/{job['job_id']}/candidates/c001/media").json()
    assert retried["preview"]["status"] == "available"
    assert len(fetcher.calls) == 2

    # Exports also retry after a transient failure even without re-polling media first.
    fetcher.error = DownloadNetworkError("9000000001", "temporary blip")
    first_c2 = client.get(f"{API}/jobs/{job['job_id']}/candidates/c002/media").json()
    assert first_c2["preview"]["reason"] == "fetch_failed"
    fetcher.error = None
    made = client.post(
        f"{API}/exports",
        json=export_body(job["job_id"], candidate_id="c002", start_s=255.0, end_s=270.0),
    )
    assert made.status_code == 201
    assert client.get(f"{API}/exports/{made.json()['export_id']}").json()["state"] == "done"


def test_a_short_segment_becomes_retryable_failure_and_invalidates_file(
    make_client: Callable[..., tuple[TestClient, MockService]],
    make_test_media: Callable[..., Path],
    tmp_path: Path,
) -> None:
    """A downloaded segment shorter than the candidate doesn't leave preview stuck in
    `preparing`: it reports retryable `fetch_failed` and removes the short cached file."""
    video = make_test_media("vod.mp4", duration_s=90.0, video_size=(64, 36), frame_rate=5)
    raw_dir = tmp_path / "raw" / "video"
    # Planned span for c001 (118-148) is 88-178; a 50 s file ends at 138 < 148.
    fetcher = FakeSegmentFetcher(video, raw_dir, duration_s=50.0)
    client, _ = segment_client(make_client, tmp_path, fetcher)
    job = done_job(client)

    first = client.get(f"{API}/jobs/{job['job_id']}/candidates/c001/media").json()
    assert first["preview"]["status"] == "unavailable"
    assert first["preview"]["reason"] == "fetch_failed"
    assert first["export"]["status"] == "available"
    short_file = raw_dir / "9000000001" / "segment-88-178.mp4"
    assert not short_file.exists()

    # Retrying after the short file was invalidated downloads a fresh full-length segment.
    fetcher.duration_s = None
    retried = client.get(f"{API}/jobs/{job['job_id']}/candidates/c001/media").json()
    assert retried["preview"]["status"] == "available"
    assert retried["preview"]["media_start_s"] == pytest.approx(88.0)
    assert retried["preview"]["media_end_s"] == pytest.approx(178.0)
    assert short_file.is_file()
    assert len(fetcher.calls) == 2


def test_a_short_segment_for_render_fails_with_fetch_failed_and_invalidates_file(
    make_client: Callable[..., tuple[TestClient, MockService]],
    make_test_media: Callable[..., Path],
    tmp_path: Path,
) -> None:
    """A render whose downloaded segment doesn't cover its trim fails with retryable
    `fetch_failed` and deletes the short file so the next attempt re-downloads cleanly."""
    video = make_test_media("vod.mp4", duration_s=20.0)
    raw_dir = tmp_path / "raw" / "video"
    # Trim 118-148 fetches 88-178; 50 s stops at 138 < 148.
    fetcher = FakeSegmentFetcher(video, raw_dir, duration_s=50.0)
    renderer = FakeRenderer()
    client, _ = segment_client(make_client, tmp_path, fetcher, render=renderer)
    job = done_job(client)

    first = client.post(
        f"{API}/exports", json=export_body(job["job_id"], start_s=118.0, end_s=148.0)
    )
    assert first.status_code == 201
    failed = client.get(f"{API}/exports/{first.json()['export_id']}").json()
    assert failed["state"] == "failed"
    assert failed["error"]["code"] == "media_unavailable"
    assert failed["error"]["details"] == {"reason": "fetch_failed"}
    short_file = raw_dir / "9000000001" / "segment-88-178.mp4"
    assert not short_file.exists()
    assert renderer.specs == []

    # Retrying once full footage arrives succeeds.
    fetcher.duration_s = None
    second = client.post(
        f"{API}/exports", json=export_body(job["job_id"], start_s=118.0, end_s=148.0)
    )
    assert second.status_code == 201
    done = client.get(f"{API}/exports/{second.json()['export_id']}").json()
    assert done["state"] == "done"
    assert len(fetcher.calls) == 2


def test_store_segment_fail_if_short_never_leaves_preparing_stuck(
    make_client: Callable[..., tuple[TestClient, MockService]],
    make_test_media: Callable[..., Path],
    tmp_path: Path,
) -> None:
    """Defense in depth: `_store_segment(..., fail_if_short=True)` converts a short ready
    state into retryable `fetch_failed` and removes the short file, while never overwriting
    already-ready usable media."""
    from autoclipper.api.service import _SegmentState

    video = make_test_media("vod.mp4", duration_s=20.0)
    fetcher = FakeSegmentFetcher(video, tmp_path / "raw" / "video")
    client, service = segment_client(make_client, tmp_path, fetcher)
    job_data = done_job(client)
    job = service._jobs[job_data["job_id"]]
    candidate = service._candidate(job, "c001")
    key = (job.job_id, "c001")

    short_file = tmp_path / "short.mp4"
    short_file.write_bytes(b"short")
    service._segments[key] = _SegmentState("preparing", 88.0, 178.0)
    service._store_segment(
        job,
        candidate,
        _SegmentState(
            "ready",
            planned_begin_s=88.0,
            planned_end_s=178.0,
            path=short_file,
            begin_s=88.0,
            end_s=138.0,
        ),
        fail_if_short=True,
    )
    assert service._segments[key].status == "failed"
    assert service._segments[key].reason == "fetch_failed"
    assert not short_file.exists()

    # An existing ready segment is never overwritten by a late short segment.
    good_file = tmp_path / "good.mp4"
    good_file.write_bytes(b"good")
    late_short = tmp_path / "late-short.mp4"
    late_short.write_bytes(b"short")
    service._segments[key] = _SegmentState(
        "ready",
        planned_begin_s=88.0,
        planned_end_s=178.0,
        path=good_file,
        begin_s=88.0,
        end_s=178.0,
    )
    service._store_segment(
        job,
        candidate,
        _SegmentState(
            "ready",
            planned_begin_s=88.0,
            planned_end_s=178.0,
            path=late_short,
            begin_s=88.0,
            end_s=138.0,
        ),
        fail_if_short=True,
    )
    assert service._segments[key].status == "ready"
    assert service._segments[key].path == good_file
    assert not late_short.exists()


def test_render_keeps_one_frame_end_tolerance_on_downloaded_segment(
    make_client: Callable[..., tuple[TestClient, MockService]],
    make_test_media: Callable[..., Path],
    tmp_path: Path,
) -> None:
    """Exports keep the renderer's one-frame tolerance at the end of a downloaded segment."""
    video = make_test_media("vod.mp4", duration_s=20.0)
    raw_dir = tmp_path / "raw" / "video"
    # Planned span for trim 118-148 is 88-178; 59.98 s ends at 147.98 (20 ms before 148.0).
    fetcher = FakeSegmentFetcher(video, raw_dir, duration_s=59.98)
    renderer = FakeRenderer()
    client, _ = segment_client(make_client, tmp_path, fetcher, render=renderer)
    job = done_job(client)

    made = client.post(
        f"{API}/exports", json=export_body(job["job_id"], start_s=118.0, end_s=148.0)
    )
    assert made.status_code == 201
    done = client.get(f"{API}/exports/{made.json()['export_id']}").json()
    assert done["state"] == "done"
    assert (raw_dir / "9000000001" / "segment-88-178.mp4").is_file()


def test_preview_invalidation_defers_while_parallel_export_uses_segment(
    make_client: Callable[..., tuple[TestClient, MockService]],
    make_test_media: Callable[..., Path],
    tmp_path: Path,
) -> None:
    """When a running export uses a segment that is too short for the full candidate preview,
    the preview fails retryably without deleting the file until the export finishes."""
    from autoclipper.export.clip import SourceMissingError

    short_video = make_test_media("short.mp4", duration_s=4.0)
    base = _scenario_result("9000000001")
    result = base.model_copy(
        update={
            "vod": base.vod.model_copy(update={"duration_s": 5.0}),
            "candidates": (base.candidates[0].model_copy(update={"start_s": 1.0, "end_s": 4.3}),),
        }
    )
    raw_dir = tmp_path / "raw" / "video"
    # Both export trim 1.0-3.9 and preview 1.0-4.3 resolve to span 0-5; 4.0 s covers the trim
    # but not the full candidate.
    fetcher = FakeSegmentFetcher(short_video, raw_dir, duration_s=4.0)
    entered = threading.Event()
    resume = threading.Event()
    fake_renderer = FakeRenderer()

    def gated_render(
        spec: ClipSpec,
        /,
        *,
        region: tuple[float, float, float, float] | None,
        transcriber: Transcriber | None,
        on_stage: Callable[[Stage], None],
    ) -> object:
        entered.set()
        assert resume.wait(timeout=10.0)
        if not spec.source.is_file():
            raise SourceMissingError("segment deleted while rendering")
        return fake_renderer(spec, region=region, transcriber=transcriber, on_stage=on_stage)

    client, service = make_client(
        job_runner="pipeline",
        data_dir=tmp_path / "data",
        artifacts_dir=tmp_path / "artifacts",
        lexicon=load_lexicon(LEXICON_PATH),
        tools_for=IngestTools,
        analyze=PipelineProbe(result),
        segment_fetcher=fetcher,
        render=gated_render,
        synchronous=False,
        step_s=0.01,
    )
    probe_ref = service.analyze
    assert isinstance(probe_ref, PipelineProbe)
    probe_ref.service = service

    job = client.post(f"{API}/jobs", json={"vod": "9000000001"}).json()
    wait_until(lambda: client.get(f"{API}/jobs/{job['job_id']}").json()["state"] == "done")

    made = client.post(
        f"{API}/exports",
        json=export_body(job["job_id"], start_s=1.0, end_s=3.9, captions=False),
    )
    assert made.status_code == 201
    assert entered.wait(timeout=5.0)
    try:
        client.get(f"{API}/jobs/{job['job_id']}/candidates/c001/media")
        wait_until(lambda: service._fetches_done >= 1)
        # File must still exist while the export holds its render lease.
        assert (raw_dir / "9000000001" / "segment-0-5.mp4").is_file()
    finally:
        resume.set()

    export_id = made.json()["export_id"]
    wait_until(
        lambda: client.get(f"{API}/exports/{export_id}").json()["state"] in ("done", "failed")
    )
    assert client.get(f"{API}/exports/{export_id}").json()["state"] == "done"
    # Once the export releases its lease, the deferred invalidation removes the short file.
    wait_until(lambda: not (raw_dir / "9000000001" / "segment-0-5.mp4").exists())


def test_short_cache_entry_held_by_ready_preview_is_detached_for_other_candidate_retry(
    make_client: Callable[..., tuple[TestClient, MockService]],
    make_test_media: Callable[..., Path],
    tmp_path: Path,
) -> None:
    """When two candidates share a cached range and the short file covers only the first,
    rejecting it for the second detaches the first's file so retrying the second fetches fresh."""
    short_video = make_test_media("short.mp4", duration_s=4.0)
    full_video = make_test_media("full.mp4", duration_s=5.0)
    base = _scenario_result("9000000001")
    result = base.model_copy(
        update={
            "vod": base.vod.model_copy(update={"duration_s": 5.0}),
            "candidates": (
                base.candidates[0].model_copy(
                    update={"candidate_id": "c001", "rank": 1, "start_s": 1.0, "end_s": 3.9}
                ),
                base.candidates[1].model_copy(
                    update={"candidate_id": "c002", "rank": 2, "start_s": 1.0, "end_s": 4.3}
                ),
            ),
        }
    )
    raw_dir = tmp_path / "raw" / "video"
    downloads = 0
    current_source = short_video
    current_duration = 4.0

    def caching_fetcher(video_id: str, begin_s: float, end_s: float, /) -> VideoSegment:
        nonlocal downloads
        directory = raw_dir / video_id
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"segment-{round(begin_s)}-{round(end_s)}.mp4"
        if not path.exists():
            downloads += 1
            shutil.copyfile(current_source, path)
            duration = current_duration
        else:
            duration = 4.0 if path.stat().st_size == short_video.stat().st_size else 5.0
        return VideoSegment(path, float(round(begin_s)), round(begin_s) + duration)

    probe_runner = PipelineProbe(result)
    renderer = FakeRenderer()
    client, service = make_client(
        job_runner="pipeline",
        data_dir=tmp_path / "data",
        artifacts_dir=tmp_path / "artifacts",
        lexicon=load_lexicon(LEXICON_PATH),
        tools_for=IngestTools,
        analyze=probe_runner,
        segment_fetcher=caching_fetcher,
        render=renderer,
    )
    probe_runner.service = service
    job = done_job(client)

    first = client.get(f"{API}/jobs/{job['job_id']}/candidates/c001/media").json()
    assert first["preview"]["status"] == "available"
    assert downloads == 1

    # c002 hits the same 4 s cached file, rejects it, and detaches c001's copy out of the cache.
    second = client.get(f"{API}/jobs/{job['job_id']}/candidates/c002/media").json()
    assert second["preview"]["status"] == "unavailable"
    assert second["preview"]["reason"] == "fetch_failed"
    assert not (raw_dir / "9000000001" / "segment-0-5.mp4").exists()

    # c001's published preview URL still serves 206 Partial Content from the detached file.
    preview_range = client.get(str(first["preview"]["url"]), headers={"Range": "bytes=0-15"})
    assert preview_range.status_code == 206
    assert preview_range.content == short_video.read_bytes()[:16]

    # c001 remains usable and exportable through its detached pinned file.
    c001_export = client.post(
        f"{API}/exports",
        json=export_body(job["job_id"], candidate_id="c001", start_s=1.0, end_s=3.5),
    )
    assert c001_export.status_code == 201
    assert client.get(f"{API}/exports/{c001_export.json()['export_id']}").json()["state"] == "done"

    # Retrying c002 downloads fresh 5 s footage and succeeds without changing c001's preview.
    current_source = full_video
    current_duration = 5.0
    retried = client.get(f"{API}/jobs/{job['job_id']}/candidates/c002/media").json()
    assert retried["preview"]["status"] == "available"
    assert downloads == 2
    after_refill = client.get(str(first["preview"]["url"]), headers={"Range": "bytes=0-15"})
    assert after_refill.status_code == 206
    assert after_refill.content == short_video.read_bytes()[:16]

    # Closing the service reclaims detached files.
    pinned = list((raw_dir / "9000000001").glob("*.pinned-*.mp4"))
    assert len(pinned) == 1
    service.close()
    assert not pinned[0].exists()


def test_preview_preparation_leases_segment_against_parallel_export_invalidation(
    make_client: Callable[..., tuple[TestClient, MockService]],
    make_test_media: Callable[..., Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A preview fetch holds a lease on its segment through preview preparation and publication
    so a parallel export rejecting the same range cannot unlink the file underneath it."""
    from autoclipper.api import service as service_mod

    short_video = make_test_media("short.mp4", duration_s=4.0)
    base = _scenario_result("9000000001")
    result = base.model_copy(
        update={
            "vod": base.vod.model_copy(update={"duration_s": 5.0}),
            "candidates": (base.candidates[0].model_copy(update={"start_s": 1.0, "end_s": 3.9}),),
        }
    )
    raw_dir = tmp_path / "raw" / "video"
    fetcher = FakeSegmentFetcher(short_video, raw_dir, duration_s=4.0)
    entered = threading.Event()
    resume = threading.Event()
    real_copy = service_mod._playable_copy

    def gated_copy(source: Path, target: Path) -> Path:
        entered.set()
        assert resume.wait(timeout=10.0)
        return real_copy(source, target)

    monkeypatch.setattr(service_mod, "_playable_copy", gated_copy)
    probe_runner = PipelineProbe(result)
    client, service = make_client(
        job_runner="pipeline",
        data_dir=tmp_path / "data",
        artifacts_dir=tmp_path / "artifacts",
        lexicon=load_lexicon(LEXICON_PATH),
        tools_for=IngestTools,
        analyze=probe_runner,
        segment_fetcher=fetcher,
        synchronous=False,
        step_s=0.01,
    )
    probe_runner.service = service

    job = client.post(f"{API}/jobs", json={"vod": "9000000001"}).json()
    wait_until(lambda: client.get(f"{API}/jobs/{job['job_id']}").json()["state"] == "done")

    media_url = f"{API}/jobs/{job['job_id']}/candidates/c001/media"
    assert client.get(media_url).json()["preview"]["status"] == "preparing"
    assert entered.wait(timeout=5.0)
    try:
        made = client.post(
            f"{API}/exports",
            json=export_body(job["job_id"], start_s=1.0, end_s=4.3, captions=False),
        )
        assert made.status_code == 201
        export_url = f"{API}/exports/{made.json()['export_id']}"
        wait_until(lambda: client.get(export_url).json()["state"] == "failed")
        assert client.get(export_url).json()["error"]["details"] == {"reason": "fetch_failed"}
        # Still leased by preview preparation, so the file hasn't been unlinked yet.
        assert (raw_dir / "9000000001" / "segment-0-5.mp4").is_file()
    finally:
        resume.set()

    wait_until(lambda: service._fetches_done >= 1)
    ready = wait_until_media(client, job["job_id"], "c001")
    assert ready["preview"]["status"] == "available"
    assert (
        client.get(str(ready["preview"]["url"]), headers={"Range": "bytes=0-15"}).status_code == 206
    )
    # Cache slot was cleared via deferred detachment so retries fetch fresh.
    assert not (raw_dir / "9000000001" / "segment-0-5.mp4").exists()


def test_local_video_preview_preparation_runs_in_background(
    make_client: Callable[..., tuple[TestClient, MockService]],
    make_test_media: Callable[..., Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    short_preview_scenario: None,
) -> None:
    """Preparing a browser-playable copy of a job's local `video_path` runs on the background
    worker so the HTTP media request returns `preparing` immediately instead of blocking."""
    from autoclipper.api import service as service_mod

    video = make_test_media("local.mp4", duration_s=3.0)
    gate = threading.Event()
    started = threading.Event()
    real_copy = service_mod._playable_copy

    def gated_copy(source: Path, target: Path) -> Path:
        started.set()
        gate.wait(timeout=10.0)
        return real_copy(source, target)

    monkeypatch.setattr(service_mod, "_playable_copy", gated_copy)
    client, _ = make_client(synchronous=False, step_s=0.01)
    job = client.post(f"{API}/jobs", json={"vod": "9000000001", "video_path": str(video)}).json()
    wait_until(lambda: client.get(f"{API}/jobs/{job['job_id']}").json()["state"] == "done")

    first = client.get(f"{API}/jobs/{job['job_id']}/candidates/c001/media").json()
    assert first["preview"]["status"] == "preparing"
    assert first["export"]["status"] == "available"
    assert started.wait(timeout=5.0)

    gate.set()
    ready = wait_until_media(client, job["job_id"], "c001")
    assert ready["preview"]["status"] == "available"
    served = client.get(str(ready["preview"]["url"]))
    assert served.status_code == 200
    assert served.content == video.read_bytes()


def test_stacked_layout_is_offered_and_rendered_when_facecam_configured(
    make_client: Callable[..., tuple[TestClient, MockService]],
) -> None:
    """When `export_facecam_region` is set, media advertises `stacked` and exports pass a
    `StackedLayout` to the renderer (#68)."""
    renderer = FakeRenderer()
    client, _ = make_client(
        render=renderer,
        export_facecam_region=(0.02, 0.04, 0.22, 0.28),
    )
    job = done_job(client)

    media = client.get(f"{API}/jobs/{job['job_id']}/candidates/c001/media").json()
    assert "stacked" in media["export"]["layouts"]

    made = client.post(
        f"{API}/exports",
        json=export_body(job["job_id"], layout="stacked", captions=False),
    )
    assert made.status_code == 201
    assert client.get(f"{API}/exports/{made.json()['export_id']}").json()["state"] == "done"
    assert len(renderer.specs) == 1
    assert renderer.specs[0].layout == "stacked"
    assert renderer.specs[0].stacked_layout is not None
    assert renderer.specs[0].stacked_layout.facecam.x == pytest.approx(0.02)


def test_stacked_layout_resolves_channel_preset_by_broadcaster_id_and_login(
    make_client: Callable[..., tuple[TestClient, MockService]],
    tmp_path: Path,
) -> None:
    """Channel presets match by broadcaster_id or canonical login from broadcasters.parquet
    even when display_name differs or falls back to the broadcaster ID (#68)."""
    import polars as pl

    from autoclipper.export.reframe import stacked_layout_from_region

    data_dir = tmp_path / "data"
    (data_dir / "processed").mkdir(parents=True)
    # Mock VOD 9000000001 has broadcaster_id "9100000001" and display_name "user_001".
    pl.DataFrame({"broadcaster_id": ["9100000001"], "login": ["jynxzi"]}).write_parquet(
        data_dir / "processed" / "broadcasters.parquet"
    )
    preset = stacked_layout_from_region((0.02, 0.04, 0.22, 0.28))
    renderer = FakeRenderer()
    client, _ = make_client(
        render=renderer,
        data_dir=data_dir,
        channel_layouts={"jynxzi": preset},
    )
    job = done_job(client)

    media = client.get(f"{API}/jobs/{job['job_id']}/candidates/c001/media").json()
    assert "stacked" in media["export"]["layouts"]

    made = client.post(
        f"{API}/exports",
        json=export_body(job["job_id"], layout="stacked", captions=False),
    )
    assert made.status_code == 201
    assert renderer.specs[0].stacked_layout == preset


@pytest.mark.parametrize(
    ("stdout", "stderr"),
    [
        pytest.param(
            b"[ERROR] - Invalid VOD, deleted/expired VOD possibly?\n",
            b"",
            id="deleted-vod-stdout",
        ),
        pytest.param(
            b"",
            b"Unhandled exception. System.NullReferenceException: Insufficient access to VOD, "
            b"OAuth may be required.\n   at TwitchDownloaderCore.VideoDownloader."
            b"GetQualityPlaylist()\n",
            id="twitchdownloader-restricted-vod",
        ),
        pytest.param(
            b"",
            b"System.Net.Http.HttpRequestException: Response status code does not indicate "
            b"success: 403 (Forbidden).\n",
            id="subscriber-only-stderr",
        ),
    ],
)
def test_production_segment_downloader_permanent_refusals_report_source_unavailable(
    make_client: Callable[..., tuple[TestClient, MockService]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stdout: bytes,
    stderr: bytes,
) -> None:
    """Permanent Twitch refusals through the real `download_video_segment` adapter report
    `source_unavailable` (not retryable `fetch_failed`) and block exports and highlights."""
    import os
    import sys

    from autoclipper.ffmpeg import CommandResult as FFCommandResult
    from autoclipper.ffmpeg import FFmpegCommand
    from autoclipper.ingest.media import download_video_segment, raw_video_dir

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    binary = bin_dir / (
        "TwitchDownloaderCLI.exe" if sys.platform == "win32" else "TwitchDownloaderCLI"
    )
    binary.write_bytes(b"")
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ["PATH"])

    def refused_runner(command: FFmpegCommand, /, *, timeout_s: float) -> FFCommandResult:
        del command, timeout_s
        return FFCommandResult(1, stdout=stdout, stderr=stderr)

    data_dir = tmp_path / "data"

    def production_fetcher(video_id: str, begin_s: float, end_s: float) -> VideoSegment:
        return download_video_segment(
            video_id,
            begin_s=begin_s,
            end_s=end_s,
            output_dir=raw_video_dir(data_dir) / video_id,
            runner=refused_runner,
        )

    probe_runner = PipelineProbe(_scenario_result("9000000001"))
    client, service = make_client(
        job_runner="pipeline",
        data_dir=data_dir,
        artifacts_dir=tmp_path / "artifacts",
        lexicon=load_lexicon(LEXICON_PATH),
        tools_for=IngestTools,
        analyze=probe_runner,
        segment_fetcher=production_fetcher,
    )
    probe_runner.service = service
    job = done_job(client)

    media = client.get(f"{API}/jobs/{job['job_id']}/candidates/c001/media").json()
    assert media["preview"]["status"] == "unavailable"
    assert media["preview"]["reason"] == "source_unavailable"
    assert media["export"]["status"] == "unavailable"
    assert media["export"]["reason"] == "source_unavailable"
    assert media["export"]["layouts"] == []

    export = client.post(f"{API}/exports", json=export_body(job["job_id"]))
    highlights = client.post(f"{API}/highlights", json={"job_id": job["job_id"]})
    assert (export.status_code, error_code(export)) == (409, "media_unavailable")
    assert (highlights.status_code, error_code(highlights)) == (409, "media_unavailable")
    assert export.json()["error"]["details"] == {"reason": "source_unavailable"}


def test_production_segment_downloader_export_refusal_marks_candidate_source_unavailable(
    make_client: Callable[..., tuple[TestClient, MockService]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When an export hits a permanent refusal before `/media` was fetched, the export fails
    with `source_unavailable` and subsequent `/media` reflects the permanent refusal."""
    import os
    import sys

    from autoclipper.ffmpeg import CommandResult as FFCommandResult
    from autoclipper.ffmpeg import FFmpegCommand
    from autoclipper.ingest.media import download_video_segment, raw_video_dir

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    binary = bin_dir / (
        "TwitchDownloaderCLI.exe" if sys.platform == "win32" else "TwitchDownloaderCLI"
    )
    binary.write_bytes(b"")
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ["PATH"])

    def refused_runner(command: FFmpegCommand, /, *, timeout_s: float) -> FFCommandResult:
        del command, timeout_s
        return FFCommandResult(
            1,
            stdout=b"[ERROR] - Unable to get video playlist. You may need an OAuth token if this "
            b"is a sub-only VOD.\n",
            stderr=b"",
        )

    data_dir = tmp_path / "data"

    def production_fetcher(video_id: str, begin_s: float, end_s: float) -> VideoSegment:
        return download_video_segment(
            video_id,
            begin_s=begin_s,
            end_s=end_s,
            output_dir=raw_video_dir(data_dir) / video_id,
            runner=refused_runner,
        )

    probe_runner = PipelineProbe(_scenario_result("9000000001"))
    client, service = make_client(
        job_runner="pipeline",
        data_dir=data_dir,
        artifacts_dir=tmp_path / "artifacts",
        lexicon=load_lexicon(LEXICON_PATH),
        tools_for=IngestTools,
        analyze=probe_runner,
        segment_fetcher=production_fetcher,
    )
    probe_runner.service = service
    job = done_job(client)

    created = client.post(f"{API}/exports", json=export_body(job["job_id"])).json()
    failed = client.get(f"{API}/exports/{created['export_id']}").json()
    assert failed["state"] == "failed"
    assert failed["error"]["code"] == "media_unavailable"
    assert failed["error"]["details"] == {"reason": "source_unavailable"}

    media = client.get(f"{API}/jobs/{job['job_id']}/candidates/c001/media").json()
    assert media["preview"]["status"] == "unavailable"
    assert media["preview"]["reason"] == "source_unavailable"
    assert media["export"]["status"] == "unavailable"


def test_missing_twitchdownloader_keeps_actionable_install_message_and_stays_retryable(
    make_client: Callable[..., tuple[TestClient, MockService]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When TwitchDownloaderCLI is missing on PATH, the production downloader reports
    `fetch_failed` (never `source_unavailable`) with actionable install guidance."""
    from autoclipper.ingest.media import download_video_segment, raw_video_dir

    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    monkeypatch.setenv("PATH", str(empty_bin))
    data_dir = tmp_path / "data"

    def production_fetcher(video_id: str, begin_s: float, end_s: float) -> VideoSegment:
        return download_video_segment(
            video_id,
            begin_s=begin_s,
            end_s=end_s,
            output_dir=raw_video_dir(data_dir) / video_id,
        )

    probe_runner = PipelineProbe(_scenario_result("9000000001"))
    client, service = make_client(
        job_runner="pipeline",
        data_dir=data_dir,
        artifacts_dir=tmp_path / "artifacts",
        lexicon=load_lexicon(LEXICON_PATH),
        tools_for=IngestTools,
        analyze=probe_runner,
        segment_fetcher=production_fetcher,
    )
    probe_runner.service = service
    job = done_job(client)

    media = client.get(f"{API}/jobs/{job['job_id']}/candidates/c001/media").json()
    assert media["preview"]["status"] == "unavailable"
    assert media["preview"]["reason"] == "fetch_failed"
    assert "TwitchDownloaderCLI" in media["preview"]["message"]
    assert "docs/development/setup.md" in media["preview"]["message"]
    assert str(tmp_path) not in media["preview"]["message"]
    assert media["export"]["status"] == "available"

    created = client.post(f"{API}/exports", json=export_body(job["job_id"])).json()
    failed = client.get(f"{API}/exports/{created['export_id']}").json()
    assert failed["state"] == "failed"
    assert failed["error"]["code"] == "media_unavailable"
    assert failed["error"]["details"] == {"reason": "fetch_failed"}
    assert "TwitchDownloaderCLI" in failed["error"]["message"]
    assert "docs/development/setup.md" in failed["error"]["message"]


def test_late_retryable_failure_does_not_overwrite_recorded_source_unavailable(
    make_client: Callable[..., tuple[TestClient, MockService]],
    tmp_path: Path,
) -> None:
    """Once a candidate is recorded as permanently `source_unavailable`, a late retryable
    `fetch_failed` from a concurrent fetch must not downgrade or evict it."""
    from autoclipper.api.service import _SegmentState

    fetcher = FakeSegmentFetcher(tmp_path / "unused.mp4", tmp_path / "raw" / "video")
    client, service = segment_client(make_client, tmp_path, fetcher)
    job = done_job(client)
    job_record = service._jobs[job["job_id"]]
    candidate = service._candidate(job_record, "c001")

    service._store_segment(
        job_record,
        candidate,
        _SegmentState("failed", 88.0, 178.0, reason="source_unavailable"),
    )
    service._store_segment(
        job_record,
        candidate,
        _SegmentState("failed", 88.0, 178.0, reason="fetch_failed"),
    )

    media = client.get(f"{API}/jobs/{job['job_id']}/candidates/c001/media").json()
    assert media["preview"]["reason"] == "source_unavailable"
    assert media["export"]["status"] == "unavailable"
    # Second request still sees source_unavailable (was not evicted as fetch_failed).
    again = client.get(f"{API}/jobs/{job['job_id']}/candidates/c001/media").json()
    assert again["preview"]["reason"] == "source_unavailable"


@pytest.mark.ffmpeg
def test_stacked_layout_resolves_on_fresh_data_dir_when_display_name_differs_from_login(
    make_client: Callable[..., tuple[TestClient, MockService]],
    make_test_media: Callable[..., Path],
    tmp_path: Path,
) -> None:
    """On a fresh data_dir with no pre-existing broadcasters.parquet, pipeline analysis saves
    the canonical Twitch login so a login-keyed stacked preset still resolves when display_name
    differs beyond case (e.g. login `caseoh_`, display name `CaseOh`)."""
    import sys

    from autoclipper.export.reframe import stacked_layout_from_region

    audio_source = make_test_media("source.m4a", duration_s=3.0, video_size=None)
    segment_video = make_test_media("segment.mp4", duration_s=90.0)
    binary_name = "TwitchDownloaderCLI.exe" if sys.platform == "win32" else "TwitchDownloaderCLI"
    chat_binary = tmp_path / "bin" / binary_name
    chat_binary.parent.mkdir(parents=True)
    chat_binary.write_bytes(b"")
    chat_binary.chmod(0o755)
    chat_fixture = Path(__file__).parents[1] / "ingest" / "fixtures" / "chat_replay.json"
    chat_payload = chat_fixture.read_bytes()
    credentials = TwitchCredentials(SecretStr("fake-client-id"), SecretStr("fake-client-secret"))
    clock = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)

    def fake_chat(args: Sequence[str], *, timeout_s: float | None = None) -> CommandResult:
        del timeout_s
        Path(args[list(args).index("--output") + 1]).write_bytes(chat_payload)
        return CommandResult(0, BANNER, "")

    def fake_download(request: DownloadRequest, /) -> DownloadedMedia:
        path = request.output_dir / f"{request.stem}.m4a"
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(audio_source, path)
        return DownloadedMedia(path, 3.0)

    def tools_for() -> IngestTools:
        http = httpx.Client(
            transport=httpx.MockTransport(
                FakeHelix(user_login="caseoh_", user_display_name="CaseOh")
            )
        )
        return IngestTools(
            helix=HelixClient(credentials, http_client=http, clock=lambda: clock),
            salt=SecretStr("fake-salt-5b1e"),
            chat_runner=fake_chat,
            chat_binary=chat_binary,
            downloader=fake_download,
            clock=lambda: clock,
        )

    data_dir = tmp_path / "fresh-data"
    preset = stacked_layout_from_region((0.76, 0.04, 0.22, 0.28))
    renderer = FakeRenderer()
    fetcher = FakeSegmentFetcher(segment_video, output_dir=tmp_path / "raw" / "video")
    client, _ = make_client(
        job_runner="pipeline",
        data_dir=data_dir,
        artifacts_dir=tmp_path / "artifacts",
        lexicon=load_lexicon(LEXICON_PATH),
        tools_for=tools_for,
        render=renderer,
        segment_fetcher=fetcher,
        channel_layouts={"caseoh_": preset},
    )

    assert not (data_dir / "processed" / "broadcasters.parquet").exists()
    job = done_job(client, PIPELINE_VIDEO_ID)
    assert job["state"] == "done", job["error"]
    result = client.get(f"{API}/jobs/{job['job_id']}/result").json()
    assert result["vod"]["broadcaster_display_name"] == "CaseOh"
    candidate = result["candidates"][0]

    media = wait_until_media(client, job["job_id"], candidate["candidate_id"])
    assert "stacked" in media["export"]["layouts"]

    made = client.post(
        f"{API}/exports",
        json={
            "job_id": job["job_id"],
            "candidate_id": candidate["candidate_id"],
            "start_s": candidate["start_s"],
            "end_s": min(candidate["end_s"], candidate["start_s"] + 10.0),
            "layout": "stacked",
            "captions": False,
        },
    )
    assert made.status_code == 201, made.text
    assert client.get(f"{API}/exports/{made.json()['export_id']}").json()["state"] == "done"
    assert renderer.specs[0].stacked_layout == preset


def _make_forward_jump_ts(tmp_path: Path, *, audio_codec: str = "aac") -> Path:
    """Two 2 s MPEG-TS segments separated by a 13 s gap on the analysis timeline."""
    part0 = tmp_path / f"part0-{audio_codec}.ts"
    part1 = tmp_path / f"part1-{audio_codec}.ts"
    for path, hz, offset in ((part0, 440, 0.0), (part1, 880, 15.0)):
        video = "color=black:size=320x180:rate=25:duration=2"
        if offset:
            video += ",geq=lum='100+2*N':cb=128:cr=128"
        run_command(
            ffmpeg_command(
                "-y",
                *("-f", "lavfi", "-i", video),
                *("-f", "lavfi", "-i", f"sine=frequency={hz}:sample_rate=48000:duration=2"),
                *("-c:v", "libx264", "-preset", "ultrafast", "-g", "50", "-pix_fmt", "yuv420p"),
                *("-c:a", audio_codec, "-output_ts_offset", f"{offset}", "-f", "mpegts", path),
            ),
            timeout_s=60,
        )
    joined = tmp_path / f"forward-jump-{audio_codec}.ts"
    joined.write_bytes(part0.read_bytes() + part1.read_bytes())
    return joined


def _frame_mean_gray(video: Path, at_s: float, tmp_path: Path) -> float:
    import numpy as np

    raw = tmp_path / f"{video.stem}-gray-{at_s}.raw"
    run_command(
        ffmpeg_command(
            "-y",
            *("-ss", f"{at_s}", "-i", video, "-frames:v", "1"),
            *("-vf", "format=gray", "-f", "rawvideo", raw),
        ),
        timeout_s=60,
    )
    return float(np.frombuffer(raw.read_bytes(), dtype=np.uint8).mean())


def _audio_slice_dominant_hz(
    video: Path, start_s: float, duration_s: float, tmp_path: Path
) -> tuple[float, float]:
    import wave

    import numpy as np

    wav = tmp_path / f"{video.stem}-{start_s}.wav"
    run_command(
        ffmpeg_command(
            "-y",
            *("-ss", f"{start_s}", "-i", video, "-t", f"{duration_s}"),
            *("-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", wav),
        ),
        timeout_s=60,
    )
    with wave.open(str(wav), "rb") as audio:
        pcm = np.frombuffer(audio.readframes(audio.getnframes()), dtype=np.int16)
    samples = pcm.astype(np.float64) / 32768.0
    peak = float(np.abs(samples).max()) if len(samples) else 0.0
    spectrum = np.abs(np.fft.rfft(samples * np.hanning(len(samples))))
    hz = float(np.fft.rfftfreq(len(samples), 1 / 16_000)[int(spectrum.argmax())])
    return hz, peak


@pytest.mark.ffmpeg
@pytest.mark.parametrize("audio_codec", ["aac", "mp2"])
def test_local_mpegts_preview_matches_export_timeline_across_forward_jump(
    make_client: Callable[..., tuple[TestClient, MockService]],
    tmp_path: Path,
    audio_codec: str,
    short_preview_scenario: None,
) -> None:
    """Remuxed (aac) and transcoded (mp2) previews preserve >10 s forward timestamp jumps like
    exports and report the probed timeline duration instead of collapsing or guessing."""
    from autoclipper.export.clip import ClipSpec, render_clip
    from autoclipper.export.cut import Interval
    from autoclipper.export.encode import EncodeSettings

    source = _make_forward_jump_ts(tmp_path, audio_codec=audio_codec)
    client, _ = make_client(
        render=render_clip,
        probe_video=lambda path: path.suffix in {".mp4", ".ts"},
    )
    job = client.post(f"{API}/jobs", json={"vod": "9000000001", "video_path": str(source)}).json()

    media = client.get(f"{API}/jobs/{job['job_id']}/candidates/c001/media").json()
    assert media["preview"]["status"] == "available"
    assert media["preview"]["media_start_s"] == 0.0
    assert media["preview"]["media_end_s"] == pytest.approx(17.0, abs=0.25)

    served = client.get(str(media["preview"]["url"]))
    assert served.status_code == 200
    preview = tmp_path / f"preview-{audio_codec}.mp4"
    preview.write_bytes(served.content)
    assert probe(preview).duration_s == pytest.approx(17.0, abs=0.25)

    # Preview footage after the jump must match the frame at the same position in a real cut.
    assert _frame_mean_gray(preview, 1.0, tmp_path) < 50
    assert _audio_slice_dominant_hz(preview, 0.5, 1.0, tmp_path)[0] == pytest.approx(440, abs=20)
    preview_frame = _frame_mean_gray(preview, 16.0, tmp_path)
    assert 140 < preview_frame < 180
    assert _frame_mean_gray(preview, 16.3, tmp_path) > preview_frame + 10
    assert _audio_slice_dominant_hz(preview, 15.5, 1.0, tmp_path)[0] == pytest.approx(880, abs=20)
    if audio_codec == "mp2":
        # Transcoded audio fills the forward jump with silence instead of pulling 880 Hz earlier.
        assert _audio_slice_dominant_hz(preview, 5.0, 4.0, tmp_path)[1] < 0.01

    exported = render_clip(
        ClipSpec(source, Interval(15.5, 16.5), tmp_path / f"export-{audio_codec}.mp4"),
        settings=EncodeSettings(preset="ultrafast"),
    )
    assert _frame_mean_gray(exported.output, 0.5, tmp_path) == pytest.approx(preview_frame, abs=3)
    assert _audio_slice_dominant_hz(exported.output, 0.0, 1.0, tmp_path)[0] == pytest.approx(
        880, abs=20
    )


@pytest.mark.ffmpeg
def test_local_video_preview_reports_probed_usable_coverage_instead_of_vod_duration(
    client: TestClient, make_test_media: Callable[..., Path], short_preview_scenario: None
) -> None:
    """When a local video file covers less than `vod.duration_s`, preview coverage reports the
    file's probed usable span rather than blindly advertising `vod.duration_s`."""
    video = make_test_media("short-local.mp4", duration_s=3.0)
    job = client.post(f"{API}/jobs", json={"vod": "9000000001", "video_path": str(video)}).json()

    media = client.get(f"{API}/jobs/{job['job_id']}/candidates/c001/media").json()
    assert media["preview"]["status"] == "available"
    assert media["preview"]["media_start_s"] == 0.0
    assert media["preview"]["media_end_s"] == pytest.approx(3.0, abs=0.1)
    uncovered = client.get(f"{API}/jobs/{job['job_id']}/candidates/c002/media").json()
    assert uncovered["preview"]["status"] == "unavailable"
    assert uncovered["preview"]["reason"] == "fetch_failed"
    assert "doesn't cover this moment" in uncovered["preview"]["message"]
    assert uncovered["export"]["status"] == "available"


@pytest.mark.ffmpeg
def test_segment_preview_preserves_forward_jump_timeline_and_probed_coverage(
    make_client: Callable[..., tuple[TestClient, MockService]],
    tmp_path: Path,
) -> None:
    """A downloaded segment remuxed for preview keeps >10 s forward jumps on the same timeline
    as cuts and reports probed usable coverage."""
    source = _make_forward_jump_ts(tmp_path, audio_codec="aac")
    base = _scenario_result("9000000001")
    result = base.model_copy(
        update={
            "vod": base.vod.model_copy(update={"duration_s": 100.0}),
            "candidates": (base.candidates[0].model_copy(update={"start_s": 25.5, "end_s": 26.5}),),
        }
    )

    def ts_fetcher(video_id: str, begin_s: float, end_s: float, /) -> VideoSegment:
        del video_id, begin_s, end_s
        return VideoSegment(source, 10.0, 40.0)

    probe_runner = PipelineProbe(result)
    client, service = make_client(
        job_runner="pipeline",
        data_dir=tmp_path / "data",
        artifacts_dir=tmp_path / "artifacts",
        lexicon=load_lexicon(LEXICON_PATH),
        tools_for=IngestTools,
        analyze=probe_runner,
        segment_fetcher=ts_fetcher,
    )
    probe_runner.service = service
    job = done_job(client)

    media = client.get(f"{API}/jobs/{job['job_id']}/candidates/c001/media").json()
    assert media["preview"]["status"] == "available"
    assert media["preview"]["media_start_s"] == 10.0
    assert media["preview"]["media_end_s"] == pytest.approx(27.0, abs=0.25)

    served = client.get(str(media["preview"]["url"]))
    assert served.status_code == 200
    preview = tmp_path / "segment-preview.mp4"
    preview.write_bytes(served.content)
    assert 140 < _frame_mean_gray(preview, 16.0, tmp_path) < 180
    assert _audio_slice_dominant_hz(preview, 15.5, 1.0, tmp_path)[0] == pytest.approx(880, abs=20)


@pytest.mark.ffmpeg
def test_segment_preview_that_misses_the_candidate_says_so_and_keeps_export(
    make_client: Callable[..., tuple[TestClient, MockService]],
    tmp_path: Path,
) -> None:
    """A downloaded segment whose measured coverage ends before the candidate reports
    `fetch_failed` with the "doesn't cover" message (not "couldn't be prepared"), and export
    stays available (#176)."""
    source = _make_forward_jump_ts(tmp_path, audio_codec="aac")  # 17 s of media
    base = _scenario_result("9000000001")
    result = base.model_copy(
        update={
            "vod": base.vod.model_copy(update={"duration_s": 100.0}),
            "candidates": (base.candidates[0].model_copy(update={"start_s": 28.0, "end_s": 29.0}),),
        }
    )

    def ts_fetcher(video_id: str, begin_s: float, end_s: float, /) -> VideoSegment:
        del video_id, begin_s, end_s
        # The segment claims 10-40 s of VOD time, but the file's media ends at about 27 s.
        return VideoSegment(source, 10.0, 40.0)

    probe_runner = PipelineProbe(result)
    client, service = make_client(
        job_runner="pipeline",
        data_dir=tmp_path / "data",
        artifacts_dir=tmp_path / "artifacts",
        lexicon=load_lexicon(LEXICON_PATH),
        tools_for=IngestTools,
        analyze=probe_runner,
        segment_fetcher=ts_fetcher,
    )
    probe_runner.service = service
    job = done_job(client)

    media = client.get(f"{API}/jobs/{job['job_id']}/candidates/c001/media").json()
    assert media["preview"]["status"] == "unavailable"
    assert media["preview"]["reason"] == "fetch_failed"
    assert "doesn't cover this whole moment" in media["preview"]["message"]
    assert "couldn't be prepared" not in media["preview"]["message"]
    assert media["export"]["status"] == "available"


def test_serve_passes_the_configured_title_polisher_to_pipeline_jobs(
    monkeypatch: pytest.MonkeyPatch, samples: SampleMedia
) -> None:
    """With AUTOCLIPPER_TITLE_LLM_* set, the API's analysis runs with the polisher (#65);
    without them it runs plain `analyze_vod`."""
    import functools

    import uvicorn
    from typer.testing import CliRunner

    from autoclipper.analyze import analyze_vod
    from autoclipper.api import service as service_module
    from autoclipper.cli import app as cli
    from autoclipper.config import get_settings

    monkeypatch.setattr(service_module, "ensure_sample_media", lambda directory: samples)
    seen: list[object] = []
    real_service = service_module.MockService

    def capture(**kwargs: object) -> MockService:
        seen.append(kwargs.get("analyze", analyze_vod))
        return real_service(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(service_module, "MockService", capture)

    def run(api: object, **kwargs: object) -> None:
        with TestClient(api):  # type: ignore[arg-type]  # runs the lifespan, stops the workers
            pass

    monkeypatch.setattr(uvicorn, "run", run)
    lexicon = Path(__file__).parents[2] / "configs" / "lexicon.yaml"

    assert CliRunner().invoke(cli, ["serve", "--lexicon", str(lexicon)]).exit_code == 0
    monkeypatch.setenv("AUTOCLIPPER_TITLE_LLM_BASE_URL", "http://127.0.0.1:9/v1")
    monkeypatch.setenv("AUTOCLIPPER_TITLE_LLM_MODEL", "local")
    get_settings.cache_clear()
    assert CliRunner().invoke(cli, ["serve", "--lexicon", str(lexicon)]).exit_code == 0

    plain, polished = seen
    assert plain is analyze_vod
    assert isinstance(polished, functools.partial)
    assert polished.func is analyze_vod
    assert polished.keywords["title_polisher"].model == "local"
