"""Tests for the mock backend against docs/architecture/api-contract.md (FastAPI test client)."""

import itertools
import json
import shutil
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
from autoclipper.ffmpeg import FFmpegExitError, ffmpeg_command, probe
from autoclipper.ingest.chat import PINNED_TOOL_VERSION, CommandResult
from autoclipper.ingest.media import DownloadedMedia, DownloadRequest, VodAccessRestrictedError
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


def test_health_reports_the_mock_runner(client: TestClient) -> None:
    response = client.get(f"{API}/health")

    assert response.json() == {"status": "ok", "schema_version": "1.0", "job_runner": "mock"}


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


def test_9000000007_with_a_local_file_has_media(client: TestClient, tmp_path: Path) -> None:
    video = tmp_path / "vod.mp4"
    video.write_bytes(b"x")
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

    def run(api: object, **kwargs: object) -> None:
        with TestClient(api):  # type: ignore[arg-type]  # runs the lifespan, stops the workers
            pass

    monkeypatch.setattr(uvicorn, "run", run)
    lexicon = Path(__file__).parents[2] / "configs" / "lexicon.yaml"

    result = CliRunner().invoke(cli, ["serve", "--lexicon", str(lexicon)])

    assert result.exit_code == 0, result.output
    assert "pipeline" in result.output  # the default runner
    assert "TWITCH_CLIENT_ID" in result.output  # the missing-credentials note
    assert "(mock jobs) on http" not in result.output  # no longer branded mock-only


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
    )
    probe.service = service
    return client, service


def test_the_health_endpoint_reports_the_pipeline_runner(
    make_client: Callable[..., tuple[TestClient, MockService]], tmp_path: Path
) -> None:
    probe = PipelineProbe(_scenario_result("9000000001"))
    client, _ = pipeline_client(make_client, probe, tmp_path)

    response = client.get(f"{API}/health")

    assert response.json() == {
        "status": "ok",
        "schema_version": "1.0",
        "job_runner": "pipeline",
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
