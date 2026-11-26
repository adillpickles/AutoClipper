"""Tests for the mock backend against docs/architecture/api-contract.md (FastAPI test client)."""

import itertools
import json
import shutil
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
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
    assert result["schema_version"] == "1.0"
    assert result["vod"]["video_id"] == "9000000001"
    assert result["scorer"] == {"kind": "mock", "name": "mock", "run_id": None}
    assert [c["source"] for c in result["candidates"]] == ["both", "discovered", "viewer_clips"]
    assert result["candidates"][0]["top_signals"] == ["chat_msg_rate_z", "audio_rms_max_z"]
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
        }
    ]


def test_scenario_9000000003_is_done_with_a_chat_missing_warning(client: TestClient) -> None:
    job = done_job(client, "9000000003")

    result = client.get(f"{API}/jobs/{job['job_id']}/result").json()

    assert [w["code"] for w in job["warnings"]] == ["chat_missing"]  # type: ignore[union-attr]
    assert result["warnings"] == job["warnings"]
    assert result["vod"]["duration_s"] == 300.0
    assert [c["source"] for c in result["candidates"]] == ["discovered", "viewer_clips"]


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
    method = client.delete(f"{API}/jobs/job_0000000000000000")

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
    assert (done["start_s"], done["end_s"], done["captions"]) == (115.5, 150.0, True)
    file = client.get(done["download_url"])
    assert file.status_code == 200
    assert file.headers["content-type"] == "video/mp4"
    assert file.headers["content-disposition"] == (
        'attachment; filename="autoclipper_9000000001_115.5-150.0.mp4"'
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
    [(100.0, 200.0), (-1.0, 30.0), (340.0, 361.0), (50.0, 50.0), (60.0, 40.0)],
    ids=["over_90_s", "before_start", "past_end", "empty", "reversed"],
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
    file = client.get(video["download_url"])
    assert file.headers["content-disposition"] == (
        'attachment; filename="autoclipper_9000000001_highlights.mp4"'
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
    client: TestClient, make_test_media: Callable[..., Path], tmp_path: Path
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

    def __init__(self, vod_type: str = "archive") -> None:
        self.vod_type = vod_type

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
                            "login": "streamer_000",
                            "display_name": "Streamer_000",
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
def test_a_real_pipeline_job_from_the_api(
    make_client: Callable[..., tuple[TestClient, MockService]],
    make_test_media: Callable[..., Path],
    tmp_path: Path,
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

    client, _ = make_client(
        job_runner="pipeline",
        data_dir=tmp_path / "data",
        artifacts_dir=tmp_path / "artifacts",
        lexicon=load_lexicon(LEXICON_PATH),
        tools_for=tools_for,
        segment_fetcher=unused_fetcher,
    )  # analyze: the real `analyze_vod`

    job = done_job(client, PIPELINE_VIDEO_ID)

    assert job["state"] == "done", job["error"]
    assert [warning["code"] for warning in job["warnings"]] == ["audio_partly_muted"]
    served = client.get(f"{API}/jobs/{job['job_id']}/result").json()
    assert served["vod"]["video_id"] == PIPELINE_VIDEO_ID
    assert served["vod"]["broadcaster_display_name"] == "Streamer_000"
    assert served["scorer"] == {"kind": "heuristic", "name": "chat_loudness", "run_id": None}
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
    video = make_test_media("vod.mp4", duration_s=20.0)
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

    video = make_test_media("vod.mp4", duration_s=20.0)
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
    video = make_test_media("vod.mp4", duration_s=20.0)
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
    client: TestClient, make_test_media: Callable[..., Path], tmp_path: Path
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
    make_client: Callable[..., tuple[TestClient, MockService]], tmp_path: Path
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
    video = make_test_media("vod.mp4", duration_s=40.0)
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
    video = make_test_media("vod.mp4", duration_s=40.0)
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
    make_client: Callable[..., tuple[TestClient, MockService]], tmp_path: Path
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

    video = make_test_media("vod.mp4", duration_s=20.0)
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

    video = make_test_media("vod.mp4", duration_s=20.0)
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
    video = make_test_media("vod.mp4", duration_s=20.0)
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
