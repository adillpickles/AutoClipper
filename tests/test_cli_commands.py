"""Unit tests for `autoclipper` CLI subcommand dispatch, validation and error handling."""

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from typer.testing import CliRunner

import autoclipper.analyze as analyze_mod
import autoclipper.api.app as api_app_mod
import autoclipper.api.service as api_service_mod
import autoclipper.cli as cli_mod
import autoclipper.export.clip as export_clip_mod
import autoclipper.export.highlights as export_highlights_mod
import autoclipper.export.timeline as export_timeline_mod
import autoclipper.ffmpeg as ffmpeg_mod
import autoclipper.ingest.media as ingest_media_mod
import autoclipper.ingest.twitch_api as twitch_api_mod
import autoclipper.ingest.vod as ingest_vod_mod
from autoclipper.analysis import AnalysisResult, Candidate, ScorerInfo, VodSummary
from autoclipper.cli import ExitCode, app
from autoclipper.export.captions import CaptioningError
from autoclipper.export.clip import ClipSpec
from autoclipper.export.cut import ContextPadding, CutPlan, Interval
from autoclipper.export.encode import ExportResult
from autoclipper.export.errors import ExportError
from autoclipper.export.highlights import HighlightsRequest, HighlightsResult
from autoclipper.export.reframe import FixedRegion, PixelRect, ReframePlan, StackedLayout
from autoclipper.ffmpeg import FFmpegError, MediaInfo, VideoStream
from autoclipper.ingest.media import VideoSegment
from autoclipper.ingest.vod import IngestReport, IngestTools, StepResult
from autoclipper.models.base import ScorerError

runner = CliRunner()
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _plain(text: str) -> str:
    return _ANSI_ESCAPE.sub("", text)


def _sample_analysis_result(duration_s: float = 120.0) -> AnalysisResult:
    return AnalysisResult(
        vod=VodSummary(
            video_id="123456",
            broadcaster_id="99",
            broadcaster_display_name="Streamer",
            title="Test Stream",
            duration_s=duration_s,
            created_at=datetime(2026, 10, 1, 12, 0, tzinfo=UTC),
        ),
        generated_at=datetime(2026, 10, 2, 12, 0, tzinfo=UTC),
        scorer=ScorerInfo(name="chat_loudness", kind="heuristic"),
        n_viewer_clips_total=0,
        candidates=(
            Candidate(
                candidate_id="c001",
                rank=1,
                start_s=10.0,
                end_s=35.0,
                score=2.5,
                source="discovered",
                n_viewer_clips=0,
                top_signals=("chat_rate",),
            ),
        ),
    )


def _sample_export_result(output: Path) -> ExportResult:
    interval = Interval(1.0, 5.0)
    return ExportResult(
        output=output,
        cut=CutPlan(
            requested=interval,
            padding=ContextPadding(),
            actual=interval,
            clamped_start=False,
            clamped_end=False,
        ),
        layout=FixedRegion(0.0, 0.0, 0.5, 1.0, fit="crop"),
        reframe=ReframePlan(
            crop=PixelRect(0, 0, 540, 960),
            scaled_width=1080,
            scaled_height=1920,
        ),
        duration_s=4.0,
        width=1080,
        height=1920,
        video_codec="h264",
        audio_codec="aac",
        silent_audio=False,
    )


# --- synth & build-dataset & evaluate -----------------------------------------------------------


def test_synth_ffmpeg_error_exits_with_external_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fail(*_args: object, **_kwargs: object) -> object:
        raise FFmpegError("ffmpeg crashed")

    monkeypatch.setattr(cli_mod, "generate_dataset", _fail)

    result = runner.invoke(app, ["synth", "--seed", "1"])

    assert result.exit_code == ExitCode.EXTERNAL_FAILURE
    assert "ffmpeg crashed" in _plain(result.output)


def test_build_dataset_prints_alert_when_null_offset_rate_exceeds_threshold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "label_rules_version": "v1",
                "clip_position": "end",
                "quality": {"null_offset_alert": True, "null_offset_rate": 0.25},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(cli_mod, "build_labeled_dataset", lambda *_a, **_kw: manifest)

    result = runner.invoke(app, ["build-dataset", "--dataset-version", "v-alert"])

    assert result.exit_code == ExitCode.SUCCESS
    assert "ALERT: more than 20% of clips lack a VOD offset" in _plain(result.output)
    assert "Label rules v1: viewer-clip positions read as clip ends" in _plain(result.output)
    assert str(manifest) in _plain(result.output)


def test_evaluate_missing_vods_file_exits_with_user_error(tmp_path: Path) -> None:
    dummy = tmp_path / "dummy.parquet"
    dummy.write_bytes(b"")

    result = runner.invoke(
        app,
        [
            "evaluate",
            "--candidates",
            str(dummy),
            "--moments",
            str(dummy),
            "--clips",
            str(dummy),
            "--dataset-version",
            "v1",
            "--scorer",
            "chat_spike",
            "--vods",
            str(tmp_path / "missing-vods.txt"),
        ],
    )

    assert result.exit_code == ExitCode.USER_ERROR
    assert "input not found" in _plain(result.output)


# --- export -------------------------------------------------------------------------------------


def test_export_facecam_region_builds_stacked_layout_and_passes_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    video = tmp_path / "vod.mp4"
    video.write_bytes(b"stub")
    out = tmp_path / "out.mp4"
    captured: list[ClipSpec] = []

    def _fake_render(spec: ClipSpec, **_kwargs: object) -> ExportResult:
        captured.append(spec)
        return _sample_export_result(spec.output)

    monkeypatch.setattr(export_clip_mod, "render_clip", _fake_render)

    result = runner.invoke(
        app,
        [
            "export",
            str(video),
            "--start",
            "1",
            "--end",
            "5",
            "--layout",
            "stacked",
            "--facecam-region",
            "0.02,0.04,0.22,0.28",
            "--output",
            str(out),
        ],
    )

    assert result.exit_code == ExitCode.SUCCESS, result.output
    assert len(captured) == 1
    assert isinstance(captured[0].stacked_layout, StackedLayout)
    assert captured[0].stacked_layout.facecam == FixedRegion(0.02, 0.04, 0.22, 0.28, fit="crop")


@pytest.mark.parametrize(
    "bad_region",
    [
        pytest.param("0.1,0.2,0.3", id="too-few-numbers"),
        pytest.param("0.1,0.2,abc,0.4", id="non-numeric"),
        pytest.param("0.8,0.0,0.5,0.5", id="outside-frame"),
    ],
)
def test_export_rejects_invalid_facecam_region(tmp_path: Path, bad_region: str) -> None:
    video = tmp_path / "vod.mp4"
    video.write_bytes(b"stub")

    result = runner.invoke(
        app,
        [
            "export",
            str(video),
            "--start",
            "0",
            "--end",
            "5",
            "--layout",
            "stacked",
            "--facecam-region",
            bad_region,
        ],
    )

    assert result.exit_code == ExitCode.USER_ERROR
    assert "invalid --facecam-region" in _plain(result.output)


def test_export_rejects_invalid_channels_file_and_unknown_channel(tmp_path: Path) -> None:
    video = tmp_path / "vod.mp4"
    video.write_bytes(b"stub")

    bad_yaml = tmp_path / "bad-channels.yaml"
    bad_yaml.write_text("- not-a-mapping\n", encoding="utf-8")
    bad_res = runner.invoke(
        app,
        [
            "export",
            str(video),
            "--start",
            "0",
            "--end",
            "5",
            "--layout",
            "stacked",
            "--channel",
            "demo",
            "--channels",
            str(bad_yaml),
        ],
    )
    assert bad_res.exit_code == ExitCode.USER_ERROR
    assert "YAML mapping" in _plain(bad_res.output)

    empty_yaml = tmp_path / "empty-channels.yaml"
    empty_yaml.write_text("channels: []\n", encoding="utf-8")
    missing_res = runner.invoke(
        app,
        [
            "export",
            str(video),
            "--start",
            "0",
            "--end",
            "5",
            "--layout",
            "stacked",
            "--channel",
            "unknown_streamer",
            "--channels",
            str(empty_yaml),
        ],
    )
    assert missing_res.exit_code == ExitCode.USER_ERROR
    assert "no stacked_layout preset found for channel 'unknown_streamer'" in _plain(
        missing_res.output
    )


def test_export_ffmpeg_error_exits_with_external_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    video = tmp_path / "vod.mp4"
    video.write_bytes(b"stub")

    def _fail(*_args: object, **_kwargs: object) -> ExportResult:
        raise FFmpegError("encoder failed")

    monkeypatch.setattr(export_clip_mod, "render_clip", _fail)

    result = runner.invoke(app, ["export", str(video), "--start", "0", "--end", "5"])

    assert result.exit_code == ExitCode.EXTERNAL_FAILURE
    assert "encoder failed" in _plain(result.output)


# --- highlights ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("segments_text", "expected_error"),
    [
        pytest.param("{not json", "can't read", id="invalid-json"),
        pytest.param(json.dumps({"other": []}), "no 'segments' list", id="missing-segments-key"),
        pytest.param(
            json.dumps({"segments": [{"candidate_id": "c001", "start_s": 10.0}]}),
            "without candidate_id, start_s and end_s",
            id="missing-end-s",
        ),
        pytest.param(
            json.dumps({"segments": []}),
            "needs 1-20",
            id="empty-segments",
        ),
    ],
)
def test_highlights_rejects_malformed_segments_file(
    tmp_path: Path, segments_text: str, expected_error: str
) -> None:
    analysis_file = tmp_path / "result.json"
    analysis_file.write_text(_sample_analysis_result().model_dump_json(), encoding="utf-8")
    video = tmp_path / "vod.mp4"
    video.write_bytes(b"stub")
    seg_file = tmp_path / "segments.json"
    seg_file.write_text(segments_text, encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "highlights",
            str(analysis_file),
            "--video",
            str(video),
            "--segments",
            str(seg_file),
        ],
    )

    assert result.exit_code == ExitCode.USER_ERROR
    assert expected_error in _plain(result.output)


def test_highlights_reports_unreadable_video_and_duration_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    analysis_file = tmp_path / "result.json"
    analysis_file.write_text(_sample_analysis_result(120.0).model_dump_json(), encoding="utf-8")
    video = tmp_path / "vod.mp4"
    video.write_bytes(b"stub")

    def _fail_probe(_path: Path) -> MediaInfo:
        raise FFmpegError("corrupt container")

    monkeypatch.setattr(ffmpeg_mod, "probe", _fail_probe)
    unreadable = runner.invoke(app, ["highlights", str(analysis_file), "--video", str(video)])
    assert unreadable.exit_code == ExitCode.USER_ERROR
    assert "invalid_video_path" in _plain(unreadable.output)

    def _short_probe(path: Path) -> MediaInfo:
        return MediaInfo(
            path=path,
            format_name="mp4",
            duration_s=50.0,
            streams=(VideoStream(index=0, codec_name="h264", width=1920, height=1080),),
        )

    monkeypatch.setattr(ffmpeg_mod, "probe", _short_probe)
    monkeypatch.setattr(export_timeline_mod, "corrected_duration_s", lambda _path: 50.0)
    mismatch = runner.invoke(app, ["highlights", str(analysis_file), "--video", str(video)])
    assert mismatch.exit_code == ExitCode.USER_ERROR
    assert "video_mismatch" in _plain(mismatch.output)


def test_highlights_accepts_video_when_corrected_duration_matches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    analysis_file = tmp_path / "result.json"
    analysis_file.write_text(_sample_analysis_result(120.0).model_dump_json(), encoding="utf-8")
    video = tmp_path / "vod.mp4"
    video.write_bytes(b"stub")
    out = tmp_path / "highlights.mp4"

    monkeypatch.setattr(
        ffmpeg_mod,
        "probe",
        lambda path: MediaInfo(
            path=path,
            format_name="mp4",
            duration_s=30.0,
            streams=(VideoStream(index=0, codec_name="h264", width=1920, height=1080),),
        ),
    )
    monkeypatch.setattr(export_timeline_mod, "corrected_duration_s", lambda _path: 120.5)
    monkeypatch.setattr(
        export_highlights_mod,
        "render_highlights",
        lambda req: HighlightsResult(
            output=req.output,
            segments=tuple(req.segments),
            order=req.order,
            captions=req.captions,
            duration_s=25.0,
            width=1920,
            height=1080,
            video_codec="h264",
            audio_codec="aac",
        ),
    )

    result = runner.invoke(
        app,
        ["highlights", str(analysis_file), "--video", str(video), "--out", str(out)],
    )

    assert result.exit_code == ExitCode.SUCCESS, result.output
    assert str(out) in _plain(result.output)


def test_highlights_rejects_unknown_candidate_id_in_segments_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    analysis_file = tmp_path / "result.json"
    analysis_file.write_text(_sample_analysis_result(120.0).model_dump_json(), encoding="utf-8")
    video = tmp_path / "vod.mp4"
    video.write_bytes(b"stub")
    seg_file = tmp_path / "segments.json"
    seg_file.write_text(
        json.dumps({"segments": [{"candidate_id": "c999", "start_s": 10.0, "end_s": 25.0}]}),
        encoding="utf-8",
    )

    def _unexpected_render(_req: HighlightsRequest) -> HighlightsResult:
        pytest.fail("render_highlights should not be called for an unknown candidate_id")

    monkeypatch.setattr(export_highlights_mod, "render_highlights", _unexpected_render)

    result = runner.invoke(
        app,
        [
            "highlights",
            str(analysis_file),
            "--video",
            str(video),
            "--segments",
            str(seg_file),
        ],
    )

    assert result.exit_code == ExitCode.USER_ERROR
    assert "c999" in _plain(result.output)


@pytest.mark.parametrize(
    ("exc", "expected_code"),
    [
        pytest.param(CaptioningError("whisper missing"), "captioning_failed", id="captioning"),
        pytest.param(ExportError("bad layout"), "render_failed", id="export"),
        pytest.param(FFmpegError("ffmpeg failed"), "render_failed", id="ffmpeg"),
    ],
)
def test_highlights_maps_render_errors_to_user_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    exc: Exception,
    expected_code: str,
) -> None:
    analysis_file = tmp_path / "result.json"
    analysis_file.write_text(_sample_analysis_result(120.0).model_dump_json(), encoding="utf-8")
    video = tmp_path / "vod.mp4"
    video.write_bytes(b"stub")

    monkeypatch.setattr(
        ffmpeg_mod,
        "probe",
        lambda path: MediaInfo(
            path=path,
            format_name="mp4",
            duration_s=120.0,
            streams=(VideoStream(index=0, codec_name="h264", width=1920, height=1080),),
        ),
    )

    def _raise(req: HighlightsRequest) -> HighlightsResult:
        raise exc

    monkeypatch.setattr(export_highlights_mod, "render_highlights", _raise)

    result = runner.invoke(app, ["highlights", str(analysis_file), "--video", str(video)])

    assert result.exit_code == ExitCode.USER_ERROR
    assert expected_code in _plain(result.output)


# --- ingest & analyze ---------------------------------------------------------------------------


def test_ingest_closes_helix_client_and_exits_0_when_all_steps_succeed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed: list[bool] = []

    class _FakeHelix:
        def __init__(self, _creds: object) -> None:
            pass

        def close(self) -> None:
            closed.append(True)

    monkeypatch.setenv("TWITCH_CLIENT_ID", "fake-id")
    monkeypatch.setenv("TWITCH_CLIENT_SECRET", "fake-secret")
    monkeypatch.setenv("AUTOCLIPPER_CHAT_HASH_SALT", "fake-salt")
    monkeypatch.setattr(twitch_api_mod, "HelixClient", _FakeHelix)
    monkeypatch.setattr(
        ingest_vod_mod,
        "ingest_vod",
        lambda video_id, **_kwargs: IngestReport(
            video_id=video_id,
            steps=(StepResult("vod_meta", "ok", "saved"),),
        ),
    )

    result = runner.invoke(app, ["ingest", "123456"])

    assert result.exit_code == ExitCode.SUCCESS, result.output
    assert closed == [True]
    assert "vod_meta: ok - saved" in _plain(result.output)


def test_analyze_rejects_missing_model_dir_and_bad_lexicon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _unexpected_analyze(*_args: object, **_kwargs: object) -> AnalysisResult:
        pytest.fail("analyze_vod should not be called when CLI input validation fails")

    monkeypatch.setattr(analyze_mod, "analyze_vod", _unexpected_analyze)

    missing_model = runner.invoke(
        app, ["analyze", "123456", "--model", str(tmp_path / "no-such-model")]
    )
    assert missing_model.exit_code == ExitCode.USER_ERROR
    assert "no trained model run" in _plain(missing_model.output)

    bad_lexicon = tmp_path / "bad-lexicon.yaml"
    bad_lexicon.write_text("not_a_valid_lexicon: true\n", encoding="utf-8")
    bad_lex_res = runner.invoke(app, ["analyze", "123456", "--lexicon", str(bad_lexicon)])
    assert bad_lex_res.exit_code == ExitCode.USER_ERROR
    assert "Error:" in _plain(bad_lex_res.output)
    assert "lexicon" in _plain(bad_lex_res.output).lower()


def test_analyze_closes_helix_and_maps_scorer_error_when_salt_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    closed: list[bool] = []
    seen_tools: list[IngestTools] = []

    class _FakeHelix:
        def __init__(self, _creds: object) -> None:
            pass

        def close(self) -> None:
            closed.append(True)

    lexicon_path = Path(__file__).resolve().parents[1] / "configs" / "lexicon.yaml"
    monkeypatch.setenv("TWITCH_CLIENT_ID", "fake-id")
    monkeypatch.setenv("TWITCH_CLIENT_SECRET", "fake-secret")
    monkeypatch.setattr(twitch_api_mod, "HelixClient", _FakeHelix)

    def _fail_scorer(*_args: object, **kwargs: Any) -> AnalysisResult:  # noqa: ANN401
        tools = kwargs["tools"]
        assert isinstance(tools, IngestTools)
        seen_tools.append(tools)
        raise ScorerError("unknown baseline scorer")

    monkeypatch.setattr(analyze_mod, "analyze_vod", _fail_scorer)

    result = runner.invoke(app, ["analyze", "123456", "--lexicon", str(lexicon_path)])

    assert result.exit_code == ExitCode.USER_ERROR
    assert "unknown baseline scorer" in _plain(result.output)
    assert closed == [True]
    assert len(seen_tools) == 1
    assert seen_tools[0].salt is None
    assert "AUTOCLIPPER_CHAT_HASH_SALT" in (seen_tools[0].missing_salt or "")


# --- serve --------------------------------------------------------------------------------------


def test_serve_exits_with_external_failure_when_sample_media_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fail_samples(_dir: Path) -> api_service_mod.SampleMedia:
        raise FFmpegError("ffmpeg missing")

    monkeypatch.setattr(api_service_mod, "ensure_sample_media", _fail_samples)

    result = runner.invoke(app, ["serve", "--jobs", "mock"])

    assert result.exit_code == ExitCode.EXTERNAL_FAILURE
    assert "can't generate the sample videos" in _plain(result.output)


def test_serve_pipeline_exits_with_user_error_for_bad_lexicon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sample_path = tmp_path / "preview.mp4"
    sample_path.write_bytes(b"stub")
    monkeypatch.setattr(
        api_service_mod,
        "ensure_sample_media",
        lambda _dir: api_service_mod.SampleMedia(sample_path),
    )

    def _unexpected_run(*_args: object, **_kwargs: object) -> None:
        pytest.fail("uvicorn.run should not be called when the lexicon is invalid")

    monkeypatch.setattr(uvicorn, "run", _unexpected_run)
    bad_lexicon = tmp_path / "bad-lexicon.yaml"
    bad_lexicon.write_text("bad: 1\n", encoding="utf-8")

    result = runner.invoke(app, ["serve", "--jobs", "pipeline", "--lexicon", str(bad_lexicon)])

    assert result.exit_code == ExitCode.USER_ERROR
    assert "Error:" in _plain(result.output)
    assert "lexicon" in _plain(result.output).lower()


def test_serve_mock_starts_without_lexicon_or_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sample_path = tmp_path / "preview.mp4"
    sample_path.write_bytes(b"stub")
    captured_service_kwargs: dict[str, Any] = {}
    uvicorn_calls: list[tuple[str, int]] = []

    monkeypatch.setattr(
        api_service_mod,
        "ensure_sample_media",
        lambda _dir: api_service_mod.SampleMedia(sample_path),
    )

    class _FakeService:
        def __init__(self, **kwargs: Any) -> None:  # noqa: ANN401
            captured_service_kwargs.update(kwargs)

    monkeypatch.setattr(api_service_mod, "MockService", _FakeService)
    monkeypatch.setattr(api_app_mod, "create_app", lambda *_args, **_kwargs: object())

    def _fake_run(_app: object, *, host: str, port: int, **_kwargs: object) -> None:
        uvicorn_calls.append((host, port))

    monkeypatch.setattr(uvicorn, "run", _fake_run)

    result = runner.invoke(
        app,
        [
            "serve",
            "--jobs",
            "mock",
            "--host",
            "127.0.0.1",
            "--port",
            "8123",
            "--lexicon",
            str(tmp_path / "ignored-missing-lexicon.yaml"),
        ],
    )

    assert result.exit_code == ExitCode.SUCCESS, result.output
    assert captured_service_kwargs["job_runner"] == "mock"
    assert "tools_for" not in captured_service_kwargs
    assert uvicorn_calls == [("127.0.0.1", 8123)]


def test_serve_pipeline_wires_tools_for_and_segment_fetcher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sample_path = tmp_path / "preview.mp4"
    sample_path.write_bytes(b"stub")
    lexicon_path = Path(__file__).resolve().parents[1] / "configs" / "lexicon.yaml"
    channels_path = Path(__file__).resolve().parents[1] / "configs" / "channels.yaml"
    captured_service_kwargs: dict[str, Any] = {}
    segment_calls: list[tuple[str, float, float, Path]] = []

    monkeypatch.setattr(
        api_service_mod,
        "ensure_sample_media",
        lambda _dir: api_service_mod.SampleMedia(sample_path),
    )

    class _FakeService:
        def __init__(self, **kwargs: Any) -> None:  # noqa: ANN401
            captured_service_kwargs.update(kwargs)

    monkeypatch.setattr(api_service_mod, "MockService", _FakeService)
    monkeypatch.setattr(api_app_mod, "create_app", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(uvicorn, "run", lambda *_args, **_kwargs: None)

    def _fake_download_segment(
        video_id: str, *, begin_s: float, end_s: float, output_dir: Path
    ) -> VideoSegment:
        segment_calls.append((video_id, begin_s, end_s, output_dir))
        return VideoSegment(path=output_dir / "seg.mp4", begin_s=begin_s, end_s=end_s)

    monkeypatch.setattr(ingest_media_mod, "download_video_segment", _fake_download_segment)

    # First run without credentials/salt to verify missing_* messages.
    res_missing = runner.invoke(
        app,
        [
            "serve",
            "--jobs",
            "pipeline",
            "--lexicon",
            str(lexicon_path),
            "--channels",
            str(channels_path),
        ],
    )
    assert res_missing.exit_code == ExitCode.SUCCESS, res_missing.output
    tools_missing = captured_service_kwargs["tools_for"]()
    assert tools_missing.helix is None
    assert tools_missing.salt is None
    assert "TWITCH_CLIENT_ID" in (tools_missing.missing_helix or "")
    assert "AUTOCLIPPER_CHAT_HASH_SALT" in (tools_missing.missing_salt or "")

    # Verify segment_fetcher routes into <data_dir>/raw/video/<video_id>.
    seg = captured_service_kwargs["segment_fetcher"]("987654", 10.0, 40.0)
    assert seg.begin_s == 10.0
    assert seg.end_s == 40.0
    assert segment_calls == [("987654", 10.0, 40.0, tmp_path / "data" / "raw" / "video" / "987654")]

    # Second run with credentials/salt configured to verify HelixClient and salt are wired.
    seen_creds: list[Any] = []

    class _FakeHelix:
        def __init__(self, creds: Any) -> None:  # noqa: ANN401
            seen_creds.append(creds)

        def close(self) -> None:
            pass

    monkeypatch.setattr(twitch_api_mod, "HelixClient", _FakeHelix)
    monkeypatch.setenv("TWITCH_CLIENT_ID", "fake-id")
    monkeypatch.setenv("TWITCH_CLIENT_SECRET", "fake-secret")
    monkeypatch.setenv("AUTOCLIPPER_CHAT_HASH_SALT", "fake-salt")
    cli_mod.get_settings.cache_clear()

    res_configured = runner.invoke(
        app,
        [
            "serve",
            "--jobs",
            "pipeline",
            "--lexicon",
            str(lexicon_path),
            "--channels",
            str(channels_path),
        ],
    )
    assert res_configured.exit_code == ExitCode.SUCCESS, res_configured.output
    tools_ok = captured_service_kwargs["tools_for"]()
    assert isinstance(tools_ok.helix, _FakeHelix)
    assert len(seen_creds) == 1
    assert seen_creds[0].client_id.get_secret_value() == "fake-id"
    assert seen_creds[0].client_secret.get_secret_value() == "fake-secret"
    assert tools_ok.salt is not None
    assert tools_ok.salt.get_secret_value() == "fake-salt"
