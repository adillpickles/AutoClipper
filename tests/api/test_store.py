"""Tests for the SQLite job store, restart recovery, listing and deletion."""

import dataclasses
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from autoclipper.analysis import AnalysisResult, AnalysisWarning, Candidate, ScorerInfo, VodSummary
from autoclipper.api.app import create_app
from autoclipper.api.service import MockService, SampleMedia
from autoclipper.api.store import (
    DEFAULT_DB_NAME,
    DEFAULT_MOCK_DB_NAME,
    JobStore,
    StoredJobRecord,
    StoredRenderRecord,
    read_result_json,
    write_result_json,
)
from autoclipper.export.captions import Transcriber
from autoclipper.export.clip import ClipSpec
from autoclipper.export.highlights import HighlightsRequest, HighlightsResult
from autoclipper.features.chat import Lexicon, load_lexicon
from autoclipper.ingest.vod import IngestTools
from autoclipper.schemas import ApiError, CreateExportRequest, HighlightsSegment, Progress

API = "/api/v1"


class _FakeClock:
    def __init__(self, start: datetime | None = None, step_s: float = 1.0) -> None:
        self._current = start or datetime(2026, 11, 1, 12, 0, 0, tzinfo=UTC)
        self._step = timedelta(seconds=step_s)

    def __call__(self) -> datetime:
        now = self._current
        self._current += self._step
        return now


class _FakeClipRenderer:
    def __call__(
        self,
        spec: ClipSpec,
        /,
        *,
        region: tuple[float, float, float, float] | None,
        transcriber: Transcriber | None,
        on_stage: object,
    ) -> object:
        del region, transcriber
        if callable(on_stage):
            on_stage("rendering")
        spec.output.parent.mkdir(parents=True, exist_ok=True)
        spec.output.write_bytes(b"rendered-clip-bytes")
        return None


class _FakeHighlightsRenderer:
    def __call__(
        self,
        request: HighlightsRequest,
        /,
        *,
        transcriber: Transcriber | None,
        on_stage: object,
    ) -> HighlightsResult:
        del transcriber
        if callable(on_stage):
            on_stage("rendering")
        request.output.parent.mkdir(parents=True, exist_ok=True)
        request.output.write_bytes(b"rendered-highlights-bytes")
        return HighlightsResult(
            output=request.output,
            segments=tuple(request.segments),
            order=request.order,
            captions=request.captions,
            duration_s=sum(s.end_s - s.start_s for s in request.segments),
            width=1920,
            height=1080,
            video_codec="h264",
            audio_codec="aac",
        )


def _sample_media(tmp_path: Path) -> SampleMedia:
    path = tmp_path / "samples" / "preview.mp4"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_bytes(b"preview-bytes" * 64)
    return SampleMedia(path)


def _make_service(
    tmp_path: Path,
    *,
    synchronous: bool = True,
    step_s: float = 0.0,
    clock: _FakeClock | None = None,
    **kwargs: object,
) -> MockService:
    options: dict[str, object] = {
        "samples": _sample_media(tmp_path),
        "output_dir": tmp_path / "artifacts",
        "step_s": step_s,
        "synchronous": synchronous,
        "probe_video": lambda p: p.suffix == ".mp4",
        "render": _FakeClipRenderer(),
        "highlights_render": _FakeHighlightsRenderer(),
    }
    if clock is not None:
        options["clock"] = clock
    options.update(kwargs)
    return MockService(**options)  # type: ignore[arg-type]


def _sample_job(
    job_id: str,
    *,
    seq: int = 1,
    video_id: str = "9000000001",
    state: str = "done",
    result_path: Path | None = None,
) -> StoredJobRecord:
    now = datetime(2026, 11, 1, 12, 0, 0, tzinfo=UTC) + timedelta(seconds=seq)
    return StoredJobRecord(
        job_id=job_id,
        seq=seq,
        video_id=video_id,
        video_path=None,
        state=state,  # type: ignore[arg-type]
        title=f"Stream {seq}",
        broadcaster_id="9100000001",
        broadcaster_display_name="streamer_one",
        duration_s=360.0,
        thumbnail_url=None,
        candidate_count=3 if state == "done" else None,
        result_path=result_path,
        progress=(
            Progress(
                stage="downloading_audio",
                stage_index=4,
                stage_count=6,
                stage_fraction=0.5,
                message="Downloading audio",
            )
            if state == "running"
            else None
        ),
        warnings=(AnalysisWarning(code="chat_missing", message="No chat"),),
        error=(ApiError(code="vod_unavailable", message="Gone") if state == "failed" else None),
        created_at=now,
        started_at=now if state != "queued" else None,
        updated_at=now,
        finished_at=now if state in ("done", "failed") else None,
    )


# --- JobStore unit tests -----------------------------------------------------------------------


def test_store_round_trips_jobs_and_renders(tmp_path: Path) -> None:
    db_path = tmp_path / "artifacts" / DEFAULT_DB_NAME
    store = JobStore(db_path)
    result_path = tmp_path / "artifacts" / "analysis" / "job_1.json"
    job = _sample_job("job_1", seq=1, result_path=result_path)
    export_req = CreateExportRequest(
        job_id="job_1",
        candidate_id="c001",
        start_s=10.0,
        end_s=35.0,
        layout="center_crop",
        captions=True,
    )
    export_render = StoredRenderRecord(
        render_id="exp_1",
        job_id="job_1",
        kind="export",
        video_id="9000000001",
        captions=True,
        request=export_req,
        segments=(),
        order="chronological",
        state="done",
        progress=None,
        error=None,
        file=tmp_path / "artifacts" / "exports" / "exp_1.mp4",
        filename="user_001_2026-10-12_Clip_00-00-10.mp4",
        duration_s=25.0,
        created_at=job.created_at,
        started_at=job.created_at,
        updated_at=job.created_at,
        finished_at=job.created_at,
    )
    hl_render = StoredRenderRecord(
        render_id="hl_1",
        job_id="job_1",
        kind="highlights",
        video_id="9000000001",
        captions=False,
        request=None,
        segments=(HighlightsSegment(candidate_id="c001", start_s=10.0, end_s=35.0),),
        order="rank",
        state="done",
        progress=None,
        error=None,
        file=tmp_path / "artifacts" / "highlights" / "hl_1.mp4",
        filename="user_001_2026-10-12_highlights.mp4",
        duration_s=25.0,
        created_at=job.created_at,
        started_at=job.created_at,
        updated_at=job.created_at,
        finished_at=job.created_at,
    )
    store.upsert_job(job)
    store.upsert_render(export_render)
    store.upsert_render(hl_render)
    store.close()

    reopened = JobStore(db_path)
    try:
        jobs, renders = reopened.load_all()
        assert jobs == [job]
        assert renders == [export_render, hl_render]
    finally:
        reopened.close()


def test_store_adds_layout_key_to_a_renders_table_from_before_191(tmp_path: Path) -> None:
    db_path = tmp_path / "artifacts" / DEFAULT_DB_NAME
    job = _sample_job("job_1", seq=1)
    request = CreateExportRequest(
        job_id="job_1",
        candidate_id="c001",
        start_s=10.0,
        end_s=35.0,
        layout="stacked",
        captions=False,
    )
    render = StoredRenderRecord(
        render_id="exp_1",
        job_id="job_1",
        kind="export",
        video_id="9000000001",
        captions=False,
        request=request,
        segments=(),
        order="chronological",
        state="done",
        progress=None,
        error=None,
        file=None,
        duration_s=25.0,
        created_at=job.created_at,
        started_at=job.created_at,
        updated_at=job.created_at,
        finished_at=job.created_at,
        layout_key="stacked:v2:None:None",
    )
    store = JobStore(db_path)
    store.upsert_job(job)
    store.upsert_render(render)
    store.close()
    # Recreate the renders table as it was before #191: same schema version, no layout_key.
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("ALTER TABLE renders DROP COLUMN layout_key")
        conn.commit()
    finally:
        conn.close()

    migrated = JobStore(db_path)
    try:
        _, renders = migrated.load_all()
        assert renders == [dataclasses.replace(render, layout_key=None)]
        migrated.upsert_render(render)
        assert migrated.load_all()[1] == [render]
    finally:
        migrated.close()


def test_an_identical_stacked_export_is_reused_after_a_restart(tmp_path: Path) -> None:
    region = (0.0, 0.6, 0.3, 0.4)
    body = {
        "candidate_id": "c001",
        "start_s": 118.0,
        "end_s": 148.0,
        "layout": "stacked",
        "captions": False,
    }
    with TestClient(create_app(_make_service(tmp_path, export_facecam_region=region))) as client:
        job_id = client.post(f"{API}/jobs", json={"vod": "9000000001"}).json()["job_id"]
        first = client.post(f"{API}/exports", json={"job_id": job_id, **body})
        assert first.status_code == 201

    # The layout fingerprint is saved with the render (#191), so a restart still reuses it.
    with TestClient(create_app(_make_service(tmp_path, export_facecam_region=region))) as client:
        again = client.post(f"{API}/exports", json={"job_id": job_id, **body})
        assert again.status_code == 200
        assert again.json()["export_id"] == first.json()["export_id"]

    # A different facecam region after the restart renders again.
    moved = (0.7, 0.6, 0.3, 0.4)
    with TestClient(create_app(_make_service(tmp_path, export_facecam_region=moved))) as client:
        changed = client.post(f"{API}/exports", json={"job_id": job_id, **body})
        assert changed.status_code == 201
        assert changed.json()["export_id"] != first.json()["export_id"]


def test_store_adds_upload_id_to_a_jobs_table_from_before_uploads(tmp_path: Path) -> None:
    db_path = tmp_path / "artifacts" / DEFAULT_DB_NAME
    job = _sample_job("job_1", seq=1)
    store = JobStore(db_path)
    store.upsert_job(job)
    store.close()
    # Recreate the jobs table as it was before #165: same schema version, no upload_id column.
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("ALTER TABLE jobs DROP COLUMN upload_id")
        conn.commit()
        assert "upload_id" not in {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
    finally:
        conn.close()

    migrated = JobStore(db_path)
    try:
        jobs, _ = migrated.load_all()
        assert jobs == [job]
        assert jobs[0].upload_id is None
        uploaded = dataclasses.replace(
            _sample_job("job_2", seq=2), upload_id="upl_0123456789abcdef"
        )
        migrated.upsert_job(uploaded)
    finally:
        migrated.close()

    reopened = JobStore(db_path)
    try:
        jobs, _ = reopened.load_all()
        assert {stored.job_id: stored.upload_id for stored in jobs} == {
            "job_1": None,
            "job_2": "upl_0123456789abcdef",
        }
    finally:
        reopened.close()


def test_store_recovers_from_corrupt_and_partial_database_files(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    db_path = tmp_path / "artifacts" / DEFAULT_DB_NAME
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.write_bytes(b"definitely not a sqlite database header!!!")

    with caplog.at_level("WARNING"):
        store = JobStore(db_path)
    try:
        jobs, renders = store.load_all()
        assert jobs == []
        assert renders == []
        store.upsert_job(_sample_job("job_after_recovery"))
        assert len(store.load_all()[0]) == 1
    finally:
        store.close()

    quarantined = list(db_path.parent.glob(f"{DEFAULT_DB_NAME}.corrupt-*"))
    assert len(quarantined) == 1
    assert any("job store is corrupt" in rec.message for rec in caplog.records)

    # Also recover when schema is partial (e.g. missing renders table)
    partial_path = tmp_path / "partial" / DEFAULT_DB_NAME
    partial_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(partial_path)
    conn.execute("CREATE TABLE jobs (job_id TEXT PRIMARY KEY)")
    conn.execute("PRAGMA user_version = 1")
    conn.commit()
    conn.close()

    recovered = JobStore(partial_path)
    try:
        recovered.upsert_job(_sample_job("job_clean"))
        assert [j.job_id for j in recovered.load_all()[0]] == ["job_clean"]
    finally:
        recovered.close()


def test_store_skips_corrupt_rows_without_crashing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    db_path = tmp_path / "artifacts" / DEFAULT_DB_NAME
    store = JobStore(db_path)
    good = _sample_job("job_good", seq=1)
    bad = _sample_job("job_bad", seq=2)
    store.upsert_job(good)
    store.upsert_job(bad)
    store.close()

    # Corrupt row #2's warnings_json directly in SQLite.
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE jobs SET warnings_json = '{not valid json' WHERE job_id = 'job_bad'")
    conn.commit()
    conn.close()

    reopened = JobStore(db_path)
    try:
        with caplog.at_level("WARNING"):
            jobs, _ = reopened.load_all()
        assert [j.job_id for j in jobs] == ["job_good"]
        assert any("skipping corrupt job row" in rec.message for rec in caplog.records)
    finally:
        reopened.close()


def test_store_handles_concurrent_writes_across_threads(tmp_path: Path) -> None:
    db_path = tmp_path / "artifacts" / DEFAULT_DB_NAME
    store = JobStore(db_path)
    errors: list[Exception] = []

    def worker(worker_idx: int) -> None:
        try:
            for item in range(25):
                seq = worker_idx * 100 + item + 1
                store.upsert_job(_sample_job(f"job_{worker_idx}_{item}", seq=seq))
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    store.close()

    assert errors == []
    reopened = JobStore(db_path)
    try:
        jobs, _ = reopened.load_all()
        assert len(jobs) == 100
    finally:
        reopened.close()


def test_result_json_read_returns_none_on_missing_or_corrupt(tmp_path: Path) -> None:
    missing = tmp_path / "analysis" / "nope.json"
    assert read_result_json(missing) is None

    bad = tmp_path / "analysis" / "bad.json"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("{corrupt", encoding="utf-8")
    assert read_result_json(bad) is None


# --- Restart persistence & API lifecycle tests -------------------------------------------------


def test_restart_restores_done_jobs_results_exports_and_highlights(tmp_path: Path) -> None:
    clock = _FakeClock()
    service1 = _make_service(tmp_path, clock=clock)
    with TestClient(create_app(service1)) as client1:
        created = client1.post(f"{API}/jobs", json={"vod": "9000000001"})
        assert created.status_code == 201
        job_id = created.json()["job_id"]
        result_before = client1.get(f"{API}/jobs/{job_id}/result").json()

        exp = client1.post(
            f"{API}/exports",
            json={
                "job_id": job_id,
                "candidate_id": "c001",
                "start_s": 118.0,
                "end_s": 148.0,
                "layout": "center_crop",
                "captions": False,
            },
        )
        assert exp.status_code == 201
        export_id = exp.json()["export_id"]

        hl = client1.post(
            f"{API}/highlights",
            json={"job_id": job_id, "order": "chronological", "captions": False},
        )
        assert hl.status_code == 201
        highlights_id = hl.json()["highlights_id"]

    # Simulate a full server restart against the same artifacts directory.
    service2 = _make_service(tmp_path, clock=clock)
    with TestClient(create_app(service2)) as client2:
        job_after = client2.get(f"{API}/jobs/{job_id}")
        assert job_after.status_code == 200
        assert job_after.json()["state"] == "done"

        result_after = client2.get(f"{API}/jobs/{job_id}/result")
        assert result_after.status_code == 200
        assert result_after.json() == result_before

        exp_after = client2.get(f"{API}/exports/{export_id}")
        assert exp_after.status_code == 200
        assert exp_after.json()["state"] == "done"
        exp_file = client2.get(f"{API}/exports/{export_id}/file")
        assert exp_file.status_code == 200
        assert exp_file.content == b"rendered-clip-bytes"

        hl_after = client2.get(f"{API}/highlights/{highlights_id}")
        assert hl_after.status_code == 200
        assert hl_after.json()["state"] == "done"
        hl_file = client2.get(f"{API}/highlights/{highlights_id}/file")
        assert hl_file.status_code == 200
        assert hl_file.content == b"rendered-highlights-bytes"

        listed = client2.get(f"{API}/jobs")
        assert listed.status_code == 200
        body = listed.json()
        assert body["total"] == 1
        item = body["items"][0]
        assert item["job_id"] == job_id
        assert item["title"] == "Synthetic stream 1"
        assert item["broadcaster_display_name"] == "user_001"
        assert item["duration_s"] == 360.0
        assert item["candidate_count"] == 3
        assert item["result_available"] is True
        assert item["export_count"] == 1
        assert item["missing_export_count"] == 0
        assert item["highlights_count"] == 1
        assert item["missing_highlights_count"] == 0
        assert len(item["exports"]) == 1
        assert item["exports"][0]["render_id"] == export_id
        assert item["exports"][0]["file_available"] is True
        assert item["exports"][0]["download_url"] == f"/api/v1/exports/{export_id}/file"
        assert len(item["highlights"]) == 1
        assert item["highlights"][0]["render_id"] == highlights_id
        assert item["highlights"][0]["file_available"] is True

        # New exports on a pre-restart job also work.
        exp2 = client2.post(
            f"{API}/exports",
            json={
                "job_id": job_id,
                "candidate_id": "c002",
                "start_s": 251.5,
                "end_s": 276.5,
                "layout": "center_crop",
                "captions": False,
            },
        )
        assert exp2.status_code == 201


def test_restart_marks_queued_and_running_jobs_and_renders_as_interrupted(tmp_path: Path) -> None:
    db_path = tmp_path / "artifacts" / DEFAULT_MOCK_DB_NAME
    store = JobStore(db_path)
    done_result_path = tmp_path / "artifacts" / "analysis" / "job_done.json"
    done_result = AnalysisResult(
        generated_at=datetime(2026, 11, 1, 12, 0, 0, tzinfo=UTC),
        vod=VodSummary(
            video_id="9000000001",
            title="Done stream",
            broadcaster_id="9100000001",
            broadcaster_display_name="user_001",
            duration_s=300.0,
            created_at=datetime(2026, 11, 1, 10, 0, 0, tzinfo=UTC),
        ),
        scorer=ScorerInfo(kind="mock", name="mock", run_id=None),
        n_viewer_clips_total=0,
        candidates=(
            Candidate(
                candidate_id="c001",
                rank=1,
                score=2.0,
                start_s=10.0,
                end_s=30.0,
                source="discovered",
                n_viewer_clips=0,
                top_signals=("chat_msg_rate_z",),
            ),
        ),
    )
    write_result_json(done_result_path, done_result)

    job_done = _sample_job("job_done", seq=1, state="done", result_path=done_result_path)
    job_running = _sample_job("job_running", seq=2, video_id="9000000002", state="running")
    job_queued = _sample_job("job_queued", seq=3, video_id="9000000003", state="queued")
    store.upsert_job(job_done)
    store.upsert_job(job_running)
    store.upsert_job(job_queued)

    now = datetime(2026, 11, 1, 12, 5, 0, tzinfo=UTC)
    store.upsert_render(
        StoredRenderRecord(
            render_id="exp_running",
            job_id="job_done",
            kind="export",
            video_id="9000000001",
            captions=False,
            request=CreateExportRequest(
                job_id="job_done",
                candidate_id="c001",
                start_s=10.0,
                end_s=30.0,
                layout="center_crop",
                captions=False,
            ),
            segments=(),
            order="chronological",
            state="running",
            progress=Progress(
                stage="rendering",
                stage_index=3,
                stage_count=3,
                stage_fraction=None,
                message="Rendering the clip",
            ),
            error=None,
            file=None,
            duration_s=None,
            created_at=now,
            started_at=now,
            updated_at=now,
            finished_at=None,
        )
    )
    store.upsert_render(
        StoredRenderRecord(
            render_id="hl_queued",
            job_id="job_done",
            kind="highlights",
            video_id="9000000001",
            captions=False,
            request=None,
            segments=(HighlightsSegment(candidate_id="c001", start_s=10.0, end_s=30.0),),
            order="chronological",
            state="queued",
            progress=None,
            error=None,
            file=None,
            duration_s=20.0,
            created_at=now,
            started_at=None,
            updated_at=now,
            finished_at=None,
        )
    )
    store.close()

    service = _make_service(tmp_path)
    with TestClient(create_app(service)) as client:
        for job_id in ("job_running", "job_queued"):
            resp = client.get(f"{API}/jobs/{job_id}").json()
            assert resp["state"] == "failed"
            assert resp["error"]["code"] == "interrupted"
            assert "again" in resp["error"]["message"].lower()
            assert resp["finished_at"] is not None

        exp = client.get(f"{API}/exports/exp_running").json()
        assert exp["state"] == "failed"
        assert exp["error"]["code"] == "interrupted"
        assert exp["download_url"] is None

        hl = client.get(f"{API}/highlights/hl_queued").json()
        assert hl["state"] == "failed"
        assert hl["error"]["code"] == "interrupted"
        assert hl["download_url"] is None

        # Retrying the interrupted VOD creates a new job instead of deduplicating.
        retry = client.post(f"{API}/jobs", json={"vod": "9000000002"})
        assert retry.status_code == 201
        assert retry.json()["job_id"] != "job_running"


def test_missing_result_and_export_files_are_reported_as_gone_without_crashing(
    tmp_path: Path,
) -> None:
    service1 = _make_service(tmp_path)
    with TestClient(create_app(service1)) as client1:
        job_id = client1.post(f"{API}/jobs", json={"vod": "9000000001"}).json()["job_id"]
        export_id = client1.post(
            f"{API}/exports",
            json={
                "job_id": job_id,
                "candidate_id": "c001",
                "start_s": 118.0,
                "end_s": 148.0,
                "layout": "center_crop",
                "captions": False,
            },
        ).json()["export_id"]
        highlights_id = client1.post(
            f"{API}/highlights",
            json={"job_id": job_id, "order": "chronological", "captions": False},
        ).json()["highlights_id"]

    # Delete the result JSON, export MP4, and highlights MP4 on disk before restart.
    (tmp_path / "artifacts" / "analysis" / f"{job_id}.json").unlink()
    (tmp_path / "artifacts" / "exports" / f"{export_id}.mp4").unlink()
    (tmp_path / "artifacts" / "highlights" / f"{highlights_id}.mp4").unlink()

    service2 = _make_service(tmp_path)
    with TestClient(create_app(service2)) as client2:
        job = client2.get(f"{API}/jobs/{job_id}")
        assert job.status_code == 200
        assert job.json()["state"] == "done"

        result = client2.get(f"{API}/jobs/{job_id}/result")
        assert result.status_code == 404
        assert result.json()["error"]["code"] == "result_file_missing"

        media = client2.get(f"{API}/jobs/{job_id}/candidates/c001/media")
        assert media.status_code == 404
        assert media.json()["error"]["code"] == "result_file_missing"

        exp_file = client2.get(f"{API}/exports/{export_id}/file")
        assert exp_file.status_code == 404
        assert exp_file.json()["error"]["code"] == "export_file_missing"

        hl_file = client2.get(f"{API}/highlights/{highlights_id}/file")
        assert hl_file.status_code == 404
        assert hl_file.json()["error"]["code"] == "highlights_file_missing"

        listed = client2.get(f"{API}/jobs").json()
        assert listed["total"] == 1
        item = listed["items"][0]
        assert item["result_available"] is False
        assert item["export_count"] == 0
        assert item["missing_export_count"] == 1
        assert item["highlights_count"] == 0
        assert item["missing_highlights_count"] == 1
        assert item["exports"][0]["file_available"] is False
        assert item["exports"][0]["download_url"] is None
        assert item["highlights"][0]["file_available"] is False
        assert item["highlights"][0]["download_url"] is None


def test_list_jobs_paginates_newest_first_and_filters_by_query_and_state(tmp_path: Path) -> None:
    clock = _FakeClock()
    service = _make_service(tmp_path, clock=clock)
    with TestClient(create_app(service)) as client:
        assert client.get(f"{API}/jobs").json() == {
            "items": [],
            "total": 0,
            "limit": 20,
            "offset": 0,
            "has_more": False,
            "next_cursor": None,
        }

        j1 = client.post(f"{API}/jobs", json={"vod": "9000000001"}).json()["job_id"]
        j2 = client.post(f"{API}/jobs", json={"vod": "9000000003"}).json()["job_id"]
        j3 = client.post(f"{API}/jobs", json={"vod": "9000000004"}).json()["job_id"]  # fails

        page1 = client.get(f"{API}/jobs", params={"limit": 2, "offset": 0})
        assert page1.status_code == 200
        assert page1.headers["cache-control"] == "no-store"
        body1 = page1.json()
        assert [item["job_id"] for item in body1["items"]] == [j3, j2]
        assert body1["total"] == 3
        assert body1["has_more"] is True

        page2 = client.get(f"{API}/jobs", params={"limit": 2, "offset": 2}).json()
        assert [item["job_id"] for item in page2["items"]] == [j1]
        assert page2["total"] == 3
        assert page2["has_more"] is False

        only_failed = client.get(f"{API}/jobs", params={"state": "failed"}).json()
        assert [item["job_id"] for item in only_failed["items"]] == [j3]
        assert only_failed["items"][0]["error"]["code"] == "vod_unavailable"

        by_title = client.get(f"{API}/jobs", params={"q": "stream 3"}).json()
        assert [item["job_id"] for item in by_title["items"]] == [j2]

        by_channel = client.get(f"{API}/jobs", params={"q": "USER_001"}).json()
        assert [item["job_id"] for item in by_channel["items"]] == [j2, j1]

        by_video_id = client.get(f"{API}/jobs", params={"q": "9000000004"}).json()
        assert [item["job_id"] for item in by_video_id["items"]] == [j3]

        bad_limit = client.get(f"{API}/jobs", params={"limit": 0})
        assert bad_limit.status_code == 422
        assert bad_limit.json()["error"]["code"] == "invalid_request"

        bad_param = client.get(f"{API}/jobs", params={"unknown": "1"})
        assert bad_param.status_code == 422
        assert bad_param.json()["error"]["code"] == "invalid_request"


@pytest.mark.parametrize("delete_index", [0, 19])
def test_list_jobs_cursor_survives_deleting_a_loaded_job(tmp_path: Path, delete_index: int) -> None:
    # Identical timestamps make creation sequence the pagination tie-breaker.
    service = _make_service(tmp_path, clock=_FakeClock(step_s=0.0))
    with TestClient(create_app(service)) as client:
        # Distinct VODs: analyzing the same VOD again reuses its job (#186).
        jobs = [
            client.post(f"{API}/jobs", json={"vod": f"91000000{index:02d}"}).json()["job_id"]
            for index in range(25)
        ][::-1]
        first = client.get(f"{API}/jobs", params={"q": "stream 1", "state": "done"}).json()
        assert [job["job_id"] for job in first["items"]] == jobs[:20]
        before = first["next_cursor"]
        assert client.delete(f"{API}/jobs/{jobs[delete_index]}").status_code == 200

        second = client.get(
            f"{API}/jobs", params={"before": before, "q": "stream 1", "state": "done"}
        ).json()
        assert [job["job_id"] for job in second["items"]] == jobs[20:]
        assert second["total"] == 24
        assert second["has_more"] is False
        assert second["next_cursor"] is None

        for params in ({"before": "invalid"}, {"before": before, "offset": 20}):
            invalid = client.get(f"{API}/jobs", params=params)
            assert invalid.status_code == 422
            assert invalid.json()["error"]["code"] == "invalid_request"


def test_the_library_collapses_duplicate_analyses_left_by_older_versions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    export = {
        "candidate_id": "c001",
        "start_s": 118.0,
        "end_s": 148.0,
        "layout": "center_crop",
        "captions": False,
    }
    # Before #186 every analysis of a VOD was a new job: simulate that library.
    with monkeypatch.context() as patch:
        patch.setattr(MockService, "_reusable_job", lambda self, key: None)
        with TestClient(create_app(_make_service(tmp_path))) as client:
            jobs = [
                client.post(f"{API}/jobs", json={"vod": "9000000001"}).json()["job_id"]
                for _ in range(3)
            ]
            for job_id in jobs[:2]:
                created = client.post(f"{API}/exports", json={"job_id": job_id, **export})
                assert created.status_code == 201
            other = client.post(f"{API}/jobs", json={"vod": "9000000003"}).json()["job_id"]

    # After the upgrade and a restart, the Library lists each VOD once without deleting anything.
    with TestClient(create_app(_make_service(tmp_path))) as client:
        listed = client.get(f"{API}/jobs").json()
        assert [item["job_id"] for item in listed["items"]] == [other, jobs[2]]
        assert listed["total"] == 2
        assert (
            client.get(f"{API}/jobs", params={"state": "done", "q": "9000000001"}).json()["total"]
            == 1
        )
        assert client.get(f"{API}/jobs/{jobs[0]}").status_code == 200
        assert client.get(f"{API}/jobs/{jobs[0]}/result").status_code == 200

        # Analyzing the VOD again reuses the newest finished job.
        again = client.post(f"{API}/jobs", json={"vod": "9000000001"})
        assert (again.status_code, again.json()["job_id"]) == (200, jobs[2])

        # Deleting the shown row reveals the next one, with its own exports.
        assert client.delete(f"{API}/jobs/{jobs[2]}").status_code == 200
        listed = client.get(f"{API}/jobs").json()
        assert [item["job_id"] for item in listed["items"]] == [other, jobs[1]]
        assert listed["items"][1]["export_count"] == 1


def test_list_jobs_includes_progress_while_running_and_clears_when_done(tmp_path: Path) -> None:
    started = threading.Event()
    release = threading.Event()

    def pause_sleep(self: MockService) -> None:
        del self
        started.set()
        release.wait(timeout=5)

    service = _make_service(tmp_path, synchronous=False, step_s=0.01)
    original_sleep = MockService._sleep
    MockService._sleep = pause_sleep  # type: ignore[method-assign]
    try:
        with TestClient(create_app(service)) as client:
            job_id = client.post(f"{API}/jobs", json={"vod": "9000000001"}).json()["job_id"]
            assert started.wait(timeout=5)

            running = client.get(f"{API}/jobs").json()["items"][0]
            assert running["job_id"] == job_id
            assert running["state"] == "running"
            assert running["progress"] == {
                "stage": "resolving_vod",
                "stage_index": 1,
                "stage_count": 6,
                "stage_fraction": None,
                "message": "Looking up the VOD",
            }
            status = client.post(f"{API}/jobs/status", json={"job_ids": [job_id, "missing"]})
            assert status.status_code == 200
            assert status.headers["cache-control"] == "no-store"
            assert status.json() == [running]

            MockService._sleep = original_sleep  # type: ignore[method-assign]
            release.set()
            for _ in range(100):
                item = client.get(f"{API}/jobs").json()["items"][0]
                if item["state"] == "done":
                    assert item["progress"] is None
                    assert item["candidate_count"] == 3
                    assert client.post(
                        f"{API}/jobs/status", json={"job_ids": [job_id, job_id]}
                    ).json() == [item]
                    break
                threading.Event().wait(0.02)
            else:
                raise AssertionError("job did not finish")
    finally:
        MockService._sleep = original_sleep  # type: ignore[method-assign]
        release.set()


def test_job_status_checks_render_files_only_for_requested_jobs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checked: list[str] = []
    is_file = Path.is_file

    def track_files(path: Path) -> bool:
        if path.parent == tmp_path / "artifacts" / "exports":
            checked.append(path.stem)
        return is_file(path)

    with TestClient(create_app(_make_service(tmp_path))) as client:
        job_ids: list[str] = []
        export_ids: list[str] = []
        for index in range(3):
            vod = f"91000000{index:02d}"
            job_id = client.post(f"{API}/jobs", json={"vod": vod}).json()["job_id"]
            job_ids.append(job_id)
            export = client.post(
                f"{API}/exports",
                json={
                    "job_id": job_id,
                    "candidate_id": "c001",
                    "start_s": 118.0,
                    "end_s": 148.0,
                    "layout": "center_crop",
                    "captions": False,
                },
            )
            assert export.status_code == 201
            export_ids.append(export.json()["export_id"])

        monkeypatch.setattr(Path, "is_file", track_files)
        response = client.post(f"{API}/jobs/status", json={"job_ids": [job_ids[1]]})
        assert response.status_code == 200
        assert [job["job_id"] for job in response.json()] == [job_ids[1]]
        render = response.json()[0]["exports"][0]
        assert render["render_id"] == export_ids[1]
        assert render["file_available"] is True
        assert checked == [export_ids[1]]


def test_delete_job_removes_record_and_optionally_deletes_exports(tmp_path: Path) -> None:
    service1 = _make_service(tmp_path)
    with TestClient(create_app(service1)) as client1:
        j_keep_files = client1.post(f"{API}/jobs", json={"vod": "9000000001"}).json()["job_id"]
        exp_keep = client1.post(
            f"{API}/exports",
            json={
                "job_id": j_keep_files,
                "candidate_id": "c001",
                "start_s": 118.0,
                "end_s": 148.0,
                "layout": "center_crop",
                "captions": False,
            },
        ).json()["export_id"]
        exp_keep_path = tmp_path / "artifacts" / "exports" / f"{exp_keep}.mp4"
        res_keep_path = tmp_path / "artifacts" / "analysis" / f"{j_keep_files}.json"
        assert exp_keep_path.is_file()
        assert res_keep_path.is_file()

        deleted1 = client1.delete(f"{API}/jobs/{j_keep_files}")
        assert deleted1.status_code == 200
        assert deleted1.json() == {
            "job_id": j_keep_files,
            "deleted": True,
            "deleted_files": 0,
        }
        assert not res_keep_path.exists()
        assert exp_keep_path.is_file()  # kept unless delete_exports=true
        assert client1.get(f"{API}/jobs/{j_keep_files}").status_code == 404
        assert client1.get(f"{API}/exports/{exp_keep}").status_code == 404

        j_wipe_files = client1.post(f"{API}/jobs", json={"vod": "9000000002"}).json()["job_id"]
        exp_wipe = client1.post(
            f"{API}/exports",
            json={
                "job_id": j_wipe_files,
                "candidate_id": "c001",
                "start_s": 12.0,
                "end_s": 40.0,
                "layout": "center_crop",
                "captions": False,
            },
        ).json()["export_id"]
        hl_wipe = client1.post(
            f"{API}/highlights",
            json={"job_id": j_wipe_files, "order": "chronological", "captions": False},
        ).json()["highlights_id"]
        exp_wipe_path = tmp_path / "artifacts" / "exports" / f"{exp_wipe}.mp4"
        hl_wipe_path = tmp_path / "artifacts" / "highlights" / f"{hl_wipe}.mp4"
        assert exp_wipe_path.is_file()
        assert hl_wipe_path.is_file()

        deleted2 = client1.delete(f"{API}/jobs/{j_wipe_files}", params={"delete_exports": "true"})
        assert deleted2.status_code == 200
        assert deleted2.json() == {
            "job_id": j_wipe_files,
            "deleted": True,
            "deleted_files": 2,
        }
        assert not exp_wipe_path.exists()
        assert not hl_wipe_path.exists()

        missing = client1.delete(f"{API}/jobs/job_0000000000000000")
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "job_not_found"

    # Deletion persists across restart.
    service2 = _make_service(tmp_path)
    with TestClient(create_app(service2)) as client2:
        assert client2.get(f"{API}/jobs").json()["total"] == 0


def test_delete_rejects_in_progress_job_and_cors_allows_delete(tmp_path: Path) -> None:
    started = threading.Event()
    release = threading.Event()

    def slow_analyze(
        video_id: str,
        *,
        data_dir: Path,
        artifacts_dir: Path,
        tools: IngestTools,
        lexicon: Lexicon,
        video_file: Path | None,
        baseline: str,
        clock: object,
        on_stage: object,
        on_warnings: object,
    ) -> AnalysisResult:
        del data_dir, artifacts_dir, tools, lexicon, video_file, baseline, clock, on_stage
        del on_warnings
        started.set()
        release.wait(timeout=5)
        return AnalysisResult(
            generated_at=datetime(2026, 11, 1, 12, 0, 0, tzinfo=UTC),
            vod=VodSummary(
                video_id=video_id,
                title="Pipeline stream",
                broadcaster_id="9100000001",
                broadcaster_display_name="pipeline_user",
                duration_s=120.0,
                created_at=datetime(2026, 11, 1, 10, 0, 0, tzinfo=UTC),
            ),
            scorer=ScorerInfo(kind="heuristic", name="chat_loudness", run_id=None),
            n_viewer_clips_total=0,
            candidates=(),
        )

    service = _make_service(
        tmp_path,
        synchronous=False,
        job_runner="pipeline",
        data_dir=tmp_path / "data",
        artifacts_dir=tmp_path / "artifacts",
        lexicon=load_lexicon(Path(__file__).resolve().parents[2] / "configs" / "lexicon.yaml"),
        tools_for=lambda: IngestTools(),
        segment_fetcher=lambda _vid, _b, _e: (_ for _ in ()).throw(AssertionError("unused")),
        analyze=slow_analyze,
    )
    with TestClient(create_app(service)) as client:
        preflight = client.options(
            f"{API}/jobs/job_123",
            headers={
                "Origin": "http://localhost:3000",
                "Access-Control-Request-Method": "DELETE",
            },
        )
        assert preflight.status_code == 200
        assert "DELETE" in preflight.headers.get("access-control-allow-methods", "")

        job_id = client.post(f"{API}/jobs", json={"vod": "9000000010"}).json()["job_id"]
        assert started.wait(timeout=5)
        busy = client.delete(f"{API}/jobs/{job_id}")
        assert busy.status_code == 409
        assert busy.json()["error"]["code"] == "job_in_progress"
        release.set()

        for _ in range(100):
            if client.get(f"{API}/jobs/{job_id}").json()["state"] == "done":
                break
            threading.Event().wait(0.02)

        assert client.delete(f"{API}/jobs/{job_id}").status_code == 200


def test_runner_stores_stay_isolated_and_shared_store_never_swaps_media(tmp_path: Path) -> None:
    def fake_analyze(
        video_id: str,
        *,
        data_dir: Path,
        artifacts_dir: Path,
        tools: IngestTools,
        lexicon: Lexicon,
        video_file: Path | None,
        baseline: str,
        clock: object,
        on_stage: object,
        on_warnings: object,
    ) -> AnalysisResult:
        del data_dir, artifacts_dir, tools, lexicon, video_file, baseline, clock, on_stage
        del on_warnings
        return AnalysisResult(
            generated_at=datetime(2026, 11, 1, 12, 0, 0, tzinfo=UTC),
            vod=VodSummary(
                video_id=video_id,
                title="Real pipeline VOD",
                broadcaster_id="9100000001",
                broadcaster_display_name="real_streamer",
                duration_s=180.0,
                created_at=datetime(2026, 11, 1, 10, 0, 0, tzinfo=UTC),
            ),
            scorer=ScorerInfo(kind="heuristic", name="chat_loudness", run_id=None),
            n_viewer_clips_total=0,
            candidates=(
                Candidate(
                    candidate_id="c001",
                    rank=1,
                    score=2.5,
                    start_s=10.0,
                    end_s=35.0,
                    source="discovered",
                    n_viewer_clips=0,
                    top_signals=("chat_msg_rate_z",),
                ),
            ),
        )

    pipeline_service = _make_service(
        tmp_path,
        job_runner="pipeline",
        data_dir=tmp_path / "data",
        artifacts_dir=tmp_path / "artifacts",
        lexicon=load_lexicon(Path(__file__).resolve().parents[2] / "configs" / "lexicon.yaml"),
        tools_for=lambda: IngestTools(),
        segment_fetcher=lambda _vid, _b, _e: (_ for _ in ()).throw(AssertionError("unused")),
        analyze=fake_analyze,
    )
    with TestClient(create_app(pipeline_service)) as pipe_client:
        pipe_job_id = pipe_client.post(f"{API}/jobs", json={"vod": "9000000001"}).json()["job_id"]

    # Default mock runner uses its own store (`library-mock.db`) and does not see pipeline jobs.
    mock_service = _make_service(tmp_path, job_runner="mock")
    with TestClient(create_app(mock_service)) as mock_client:
        assert mock_client.get(f"{API}/jobs").json()["total"] == 0

    # Even if pointed explicitly at the pipeline store, mock mode refuses to serve sample media
    # for a pipeline job.
    shared_mock = _make_service(
        tmp_path,
        job_runner="mock",
        store_path=tmp_path / "artifacts" / DEFAULT_DB_NAME,
    )
    with TestClient(create_app(shared_mock)) as shared_client:
        media = shared_client.get(f"{API}/jobs/{pipe_job_id}/candidates/c001/media").json()
        assert media["preview"]["status"] == "unavailable"
        assert media["export"]["status"] == "unavailable"


def test_relative_output_dir_restores_results_and_exports_across_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    rel_out = Path("relative-artifacts")
    service1 = _make_service(tmp_path, output_dir=rel_out)
    with TestClient(create_app(service1)) as client1:
        job_id = client1.post(f"{API}/jobs", json={"vod": "9000000001"}).json()["job_id"]
        export_id = client1.post(
            f"{API}/exports",
            json={
                "job_id": job_id,
                "candidate_id": "c001",
                "start_s": 118.0,
                "end_s": 148.0,
                "layout": "center_crop",
                "captions": False,
            },
        ).json()["export_id"]

    service2 = _make_service(tmp_path, output_dir=rel_out)
    with TestClient(create_app(service2)) as client2:
        assert client2.get(f"{API}/jobs/{job_id}/result").status_code == 200
        assert client2.get(f"{API}/exports/{export_id}/file").status_code == 200


def test_non_finite_metadata_row_is_skipped_on_load_instead_of_500(tmp_path: Path) -> None:
    service1 = _make_service(tmp_path)
    with TestClient(create_app(service1)) as client1:
        bad_id = client1.post(f"{API}/jobs", json={"vod": "9000000001"}).json()["job_id"]
        good_id = client1.post(f"{API}/jobs", json={"vod": "9000000002"}).json()["job_id"]

    conn = sqlite3.connect(tmp_path / "artifacts" / DEFAULT_MOCK_DB_NAME)
    with conn:
        conn.execute("UPDATE jobs SET duration_s = ? WHERE job_id = ?", (float("inf"), bad_id))
    conn.close()

    service2 = _make_service(tmp_path)
    with TestClient(create_app(service2)) as client2:
        listed = client2.get(f"{API}/jobs")
        assert listed.status_code == 200
        assert [item["job_id"] for item in listed.json()["items"]] == [good_id]


def test_transient_operational_error_does_not_quarantine_healthy_db(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "artifacts" / DEFAULT_DB_NAME
    store = JobStore(db_path)
    store.upsert_job(_sample_job("job_healthy"))
    store.close()

    original_open = JobStore._open_and_verify

    def fail_locked(self: JobStore) -> sqlite3.Connection:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(JobStore, "_open_and_verify", fail_locked)
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        JobStore(db_path)

    # Original file is still intact and unquarantined.
    assert db_path.is_file()
    assert list(db_path.parent.glob(f"{DEFAULT_DB_NAME}.corrupt-*")) == []

    monkeypatch.setattr(JobStore, "_open_and_verify", original_open)
    reopened = JobStore(db_path)
    try:
        assert [j.job_id for j in reopened.load_all()[0]] == ["job_healthy"]
    finally:
        reopened.close()


def test_store_write_failure_does_not_leave_orphan_job_or_kill_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 1) Create-time persistence failure rolls back before entering `_jobs`.
    service1 = _make_service(tmp_path)
    with TestClient(create_app(service1), raise_server_exceptions=False) as client1:
        original_upsert = service1._store.upsert_job
        monkeypatch.setattr(
            service1._store,
            "upsert_job",
            lambda _rec: (_ for _ in ()).throw(sqlite3.OperationalError("disk full")),
        )
        failed = client1.post(f"{API}/jobs", json={"vod": "9000000001"})
        assert failed.status_code == 500

        monkeypatch.setattr(service1._store, "upsert_job", original_upsert)
        retried = client1.post(f"{API}/jobs", json={"vod": "9000000001"})
        assert retried.status_code == 201
        assert client1.get(f"{API}/jobs/{retried.json()['job_id']}").json()["state"] == "done"

    # 2) Worker stays alive if failure-state persistence also raises.
    started = threading.Event()
    release = threading.Event()

    def pause_sleep(_self: MockService) -> None:
        started.set()
        release.wait(timeout=5)

    service2 = _make_service(tmp_path / "worker", synchronous=False, step_s=0.01)
    monkeypatch.setattr(MockService, "_sleep", pause_sleep)
    with TestClient(create_app(service2)) as client2:
        j1 = client2.post(f"{API}/jobs", json={"vod": "9000000001"}).json()["job_id"]
        assert started.wait(timeout=5)
        original_upsert2 = service2._store.upsert_job
        monkeypatch.setattr(
            service2._store,
            "upsert_job",
            lambda _rec: (_ for _ in ()).throw(sqlite3.OperationalError("write failed")),
        )
        release.set()
        for _ in range(100):
            if client2.get(f"{API}/jobs/{j1}").json()["state"] == "failed":
                break
            threading.Event().wait(0.02)
        assert client2.get(f"{API}/jobs/{j1}").json()["state"] == "failed"
        assert service2._threads[0].is_alive()

        # After storage recovers, the same worker processes subsequent jobs.
        monkeypatch.setattr(service2._store, "upsert_job", original_upsert2)
        j2 = client2.post(f"{API}/jobs", json={"vod": "9000000002"}).json()["job_id"]
        for _ in range(100):
            if client2.get(f"{API}/jobs/{j2}").json()["state"] == "done":
                break
            threading.Event().wait(0.02)
        assert client2.get(f"{API}/jobs/{j2}").json()["state"] == "done"


def test_delete_job_stays_retryable_when_file_unlink_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _make_service(tmp_path)
    with TestClient(create_app(service), raise_server_exceptions=False) as client:
        job_id = client.post(f"{API}/jobs", json={"vod": "9000000001"}).json()["job_id"]
        export_id = client.post(
            f"{API}/exports",
            json={
                "job_id": job_id,
                "candidate_id": "c001",
                "start_s": 118.0,
                "end_s": 148.0,
                "layout": "center_crop",
                "captions": False,
            },
        ).json()["export_id"]
        export_path = (tmp_path / "artifacts" / "exports" / f"{export_id}.mp4").resolve()
        assert export_path.is_file()

        real_unlink = Path.unlink

        def blocked_unlink(self: Path, missing_ok: bool = False) -> None:
            if self.resolve() == export_path:
                raise PermissionError("file in use")
            real_unlink(self, missing_ok=missing_ok)

        monkeypatch.setattr(Path, "unlink", blocked_unlink)
        first = client.delete(f"{API}/jobs/{job_id}", params={"delete_exports": "true"})
        assert first.status_code == 500
        assert first.json()["error"]["code"] == "delete_failed"
        # Job record is still present so the user can retry after closing the file.
        assert client.get(f"{API}/jobs/{job_id}").status_code == 200

        monkeypatch.setattr(Path, "unlink", real_unlink)
        second = client.delete(f"{API}/jobs/{job_id}", params={"delete_exports": "true"})
        assert second.status_code == 200
        assert second.json()["deleted_files"] == 1
        assert not export_path.exists()
        assert client.get(f"{API}/jobs/{job_id}").status_code == 404


def test_legacy_download_keeps_working_with_unreadable_analysis(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _FakeClock()
    with TestClient(create_app(_make_service(tmp_path, clock=clock))) as client:
        job_id = client.post(f"{API}/jobs", json={"vod": "9000000001"}).json()["job_id"]
        render_id = client.post(
            f"{API}/exports",
            json={
                "job_id": job_id,
                "candidate_id": "c001",
                "start_s": 115.0,
                "end_s": 140.0,
                "layout": "center_crop",
                "captions": False,
            },
        ).json()["export_id"]

    db_path = tmp_path / "artifacts" / DEFAULT_MOCK_DB_NAME
    conn = sqlite3.connect(db_path)
    with conn:
        conn.execute("ALTER TABLE renders DROP COLUMN filename")
        conn.execute("PRAGMA user_version = 1")
    conn.close()

    result_path = tmp_path / "artifacts" / "analysis" / f"{job_id}.json"
    real_is_file = Path.is_file
    real_read_text = Path.read_text

    def denied_is_file(path: Path) -> bool:
        if path == result_path:
            raise PermissionError("access denied")
        return real_is_file(path)

    def denied_read_text(path: Path, *args: object, **kwargs: object) -> str:
        if path == result_path:
            raise PermissionError("access denied")
        return real_read_text(path, *args, **kwargs)  # type: ignore[arg-type]

    with monkeypatch.context() as blocked:
        blocked.setattr(Path, "is_file", denied_is_file)
        blocked.setattr(Path, "read_text", denied_read_text)
        with TestClient(create_app(_make_service(tmp_path, clock=clock))) as client:
            status = client.get(f"{API}/exports/{render_id}")
            assert status.status_code == 200
            filename = status.json()["filename"]
            assert filename == "user_001_2026-11-01_clip_00-01-55.mp4"
            downloaded = client.get(f"{API}/exports/{render_id}/file")
            assert downloaded.status_code == 200
            assert downloaded.headers["content-disposition"] == f'attachment; filename="{filename}"'

    # Restoring metadata later must not rename a previously advertised legacy download.
    with TestClient(create_app(_make_service(tmp_path, clock=clock))) as client:
        assert client.get(f"{API}/exports/{render_id}").json()["filename"] == filename


def test_out_of_order_finish_preserves_first_finished_filename_across_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _FakeClock()
    blocked = threading.Event()
    release = threading.Event()

    class _HoldFirstRender(_FakeClipRenderer):
        def __call__(self, spec: ClipSpec, /, **kwargs: object) -> object:
            if not blocked.is_set():
                blocked.set()
                release.wait(timeout=5)
            return super().__call__(spec, **kwargs)  # type: ignore[arg-type]

    service = _make_service(
        tmp_path, synchronous=False, step_s=0.0, clock=clock, render=_HoldFirstRender()
    )
    with TestClient(create_app(service)) as client:
        job_id = client.post(f"{API}/jobs", json={"vod": "9000000001"}).json()["job_id"]
        for _ in range(100):
            if client.get(f"{API}/jobs/{job_id}").json()["state"] == "done":
                break
            threading.Event().wait(0.02)

        first = client.post(
            f"{API}/exports",
            json={
                "job_id": job_id,
                "candidate_id": "c001",
                "start_s": 115.2,
                "end_s": 140.0,
                "layout": "center_crop",
                "captions": False,
            },
        ).json()
        assert blocked.wait(timeout=5)

        second = client.post(
            f"{API}/exports",
            json={
                "job_id": job_id,
                "candidate_id": "c001",
                "start_s": 115.8,
                "end_s": 140.0,
                "layout": "center_crop",
                "captions": True,
            },
        ).json()

        # Finish the later-created export out of order before releasing the earlier one.
        second_file = tmp_path / "artifacts" / "exports" / f"{second['export_id']}.mp4"
        second_file.parent.mkdir(parents=True, exist_ok=True)
        second_file.write_bytes(b"second-bytes")
        with service._lock:
            second_rec = service._renders[second["export_id"]]
        service._finish_render(second_rec, file=second_file)
        release.set()

        for _ in range(100):
            if client.get(f"{API}/exports/{first['export_id']}").json()["state"] == "done":
                break
            threading.Event().wait(0.02)

        first_state = client.get(f"{API}/exports/{first['export_id']}").json()
        second_state = client.get(f"{API}/exports/{second['export_id']}").json()
        assert (
            second_state["filename"] == "user_001_2026-10-12_Loud-audio-laughing-chat_00-01-55.mp4"
        )
        assert (
            first_state["filename"] == "user_001_2026-10-12_Loud-audio-laughing-chat_00-01-55_2.mp4"
        )

    # Restart without analysis JSON: persisted filenames stay intact even if path reads fail.
    result_path = tmp_path / "artifacts" / "analysis" / f"{job_id}.json"
    result_path.unlink()
    restarted = _make_service(tmp_path, clock=clock)
    with TestClient(create_app(restarted)) as client2:
        real_read_text = Path.read_text

        def blocked_read_text(self: Path, *args: object, **kwargs: object) -> str:
            if self == result_path:
                raise PermissionError("access denied")
            return real_read_text(self, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(Path, "read_text", blocked_read_text)
        assert (
            client2.get(f"{API}/exports/{second['export_id']}").json()["filename"]
            == "user_001_2026-10-12_Loud-audio-laughing-chat_00-01-55.mp4"
        )
        file_resp = client2.get(f"{API}/exports/{first['export_id']}/file")
        assert file_resp.status_code == 200
        assert (
            "user_001_2026-10-12_Loud-audio-laughing-chat_00-01-55_2.mp4"
            in file_resp.headers["content-disposition"]
        )


def test_settings_persist_and_older_stores_gain_the_settings_table(tmp_path: Path) -> None:
    path = tmp_path / "library.db"
    store = JobStore(path)
    store.close()
    with sqlite3.connect(path) as conn:  # a store from before settings existed
        conn.execute("DROP TABLE settings")
        conn.execute("PRAGMA user_version = 2")

    store = JobStore(path)
    assert store.get_setting("output_folder") is None
    store.set_setting("output_folder", "D:/Clips")
    store.set_setting("output_folder", "E:/Clips")  # replaced, not duplicated
    store.close()

    reopened = JobStore(path)
    assert reopened.get_setting("output_folder") == "E:/Clips"
    with reopened._conn:  # an unreadable value is ignored, not fatal
        reopened._conn.execute("UPDATE settings SET value = '{' WHERE key = 'output_folder'")
    assert reopened.get_setting("output_folder") is None
    reopened.set_setting("output_folder", None)
    assert reopened.get_setting("output_folder") is None
    reopened.close()
