"""Tests for the mock backend against docs/architecture/api-contract.md (FastAPI test client)."""

import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from autoclipper.api.app import create_app
from autoclipper.api.service import MockService, SampleMedia, ensure_sample_media
from autoclipper.ffmpeg import probe

API = "/api/v1"


@pytest.fixture
def samples(tmp_path: Path) -> SampleMedia:
    """Stand-in sample files: the routes only serve bytes, they never decode them."""
    paths = []
    for name in ("preview.mp4", "clip.mp4", "highlights.mp4"):
        path = tmp_path / "samples" / name
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(name.encode() * 1000)
        paths.append(path)
    return SampleMedia(*paths)


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


def done_job(client: TestClient, vod: str = "9000000001") -> dict[str, object]:
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
        "layouts": ["center_crop", "configured_region"],
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


def test_a_running_export_dedupes_and_its_file_isnt_ready(
    make_client: Callable[..., tuple[TestClient, MockService]],
) -> None:
    client, _ = make_client(synchronous=False, step_s=0.05)
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
def test_sample_media_has_the_contract_sizes_and_is_reused(tmp_path: Path) -> None:
    samples = ensure_sample_media(tmp_path / "media", preview_duration_s=3.0)

    preview, clip, highlights = (
        probe(p) for p in (samples.preview, samples.clip, samples.highlights)
    )
    assert preview.duration_s == pytest.approx(3.0, abs=0.2)
    assert (clip.video_streams[0].width, clip.video_streams[0].height) == (1080, 1920)
    assert (highlights.video_streams[0].width, highlights.video_streams[0].height) == (1920, 1080)
    assert clip.audio_streams
    before = samples.clip.stat().st_mtime_ns
    again = ensure_sample_media(tmp_path / "media", preview_duration_s=3.0)
    assert again.clip.stat().st_mtime_ns == before  # not generated again


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
    client, _ = make_client(synchronous=False, step_s=0.05)
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
        with TestClient(api):  # type: ignore[arg-type]  # runs the lifespan, which stops the workers
            pass

    monkeypatch.setattr(uvicorn, "run", run)

    result = CliRunner().invoke(cli, ["serve", "--port", "8123"])

    assert result.exit_code == 0, result.output
    assert calls[0]["host"] == "127.0.0.1"
    assert calls[0]["port"] == 8123
