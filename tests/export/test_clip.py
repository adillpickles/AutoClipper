"""Tests for `render_clip` (the export pipeline of the CLI and the API) and `autoclipper export`."""

from collections.abc import Callable
from pathlib import Path

import pytest
from typer.testing import CliRunner

from autoclipper.cli import ExitCode, app
from autoclipper.export.captions import CaptioningError, Stage, Word
from autoclipper.export.clip import (
    ClipSpec,
    ClipTooLongError,
    IntervalOutsideMediaError,
    NoRegionConfiguredError,
    SourceMissingError,
    available_layouts,
    layout_named,
    render_clip,
)
from autoclipper.export.cut import Interval
from autoclipper.export.reframe import CenterCrop, FixedRegion
from autoclipper.ffmpeg import probe

runner = CliRunner()


def test_layouts_offer_configured_region_only_with_a_region() -> None:
    assert available_layouts(None) == ("center_crop",)
    assert available_layouts((0.25, 0.0, 0.5, 1.0)) == ("center_crop", "configured_region")
    assert isinstance(layout_named("center_crop", None), CenterCrop)
    assert isinstance(layout_named("configured_region", (0.25, 0.0, 0.5, 1.0)), FixedRegion)
    with pytest.raises(NoRegionConfiguredError, match="AUTOCLIPPER_EXPORT_REGION"):
        layout_named("configured_region", None)


def test_checks_run_before_anything_is_rendered(tmp_path: Path) -> None:
    video = tmp_path / "vod.mp4"
    video.write_bytes(b"not decoded")

    with pytest.raises(ClipTooLongError, match="at most 90 s"):
        render_clip(ClipSpec(video, Interval(0.0, 90.5), tmp_path / "a.mp4"))
    with pytest.raises(SourceMissingError):
        render_clip(ClipSpec(tmp_path / "missing.mp4", Interval(0.0, 5.0), tmp_path / "b.mp4"))
    with pytest.raises(NoRegionConfiguredError):
        render_clip(
            ClipSpec(video, Interval(0.0, 5.0), tmp_path / "c.mp4", layout="configured_region")
        )
    assert not list(tmp_path.glob("*.mp4.*"))


@pytest.mark.ffmpeg
def test_a_plain_render_is_exact_and_reports_rendering(
    make_test_media: Callable[..., Path], tmp_path: Path
) -> None:
    video = make_test_media("vod.mp4", duration_s=6.0)
    stages: list[Stage] = []

    result = render_clip(
        ClipSpec(video, Interval(1.5, 4.0), tmp_path / "out" / "clip.mp4"),
        on_stage=stages.append,
    )

    assert stages == ["rendering"]
    assert (result.width, result.height) == (1080, 1920)
    assert probe(result.output).duration_s == pytest.approx(2.5, abs=0.05)
    assert result.cut.actual == Interval(1.5, 4.0)


@pytest.mark.ffmpeg
def test_a_captioned_render_reports_transcribing_then_rendering(
    make_test_media: Callable[..., Path], tmp_path: Path
) -> None:
    video = make_test_media("vod.mp4", duration_s=4.0)
    stages: list[Stage] = []
    heard: list[Path] = []

    def transcriber(wav: Path, /) -> list[Word]:
        heard.append(wav)
        assert stages == ["transcribing"]
        return [Word("HI", 0.1, 0.5)]

    result = render_clip(
        ClipSpec(video, Interval(0.5, 2.5), tmp_path / "clip.mp4", captions=True),
        transcriber=transcriber,
        on_stage=stages.append,
    )

    assert stages == ["transcribing", "rendering"]
    assert len(heard) == 1
    assert result.output.is_file()


@pytest.mark.ffmpeg
def test_an_interval_past_the_end_of_the_video_is_refused(
    make_test_media: Callable[..., Path], tmp_path: Path
) -> None:
    video = make_test_media("vod.mp4", duration_s=3.0)
    called: list[Path] = []

    def transcriber(wav: Path, /) -> list[Word]:
        called.append(wav)
        return []

    for captions in (False, True):
        with pytest.raises(IntervalOutsideMediaError, match="covers only"):
            render_clip(
                ClipSpec(video, Interval(1.0, 5.0), tmp_path / "clip.mp4", captions=captions),
                transcriber=transcriber,
            )

    assert called == []  # refused before transcribing
    assert not (tmp_path / "clip.mp4").exists()


@pytest.mark.ffmpeg
def test_failed_captions_write_nothing(
    make_test_media: Callable[..., Path], tmp_path: Path
) -> None:
    video = make_test_media("vod.mp4", duration_s=3.0)

    def broken(wav: Path, /) -> list[Word]:
        raise CaptioningError("transcription failed: boom")

    with pytest.raises(CaptioningError):
        render_clip(
            ClipSpec(video, Interval(0.0, 2.0), tmp_path / "clip.mp4", captions=True),
            transcriber=broken,
        )

    assert not (tmp_path / "clip.mp4").exists()


# --- autoclipper export ------------------------------------------------------------------------


@pytest.mark.ffmpeg
def test_the_export_command_writes_to_the_artifacts_dir(
    make_test_media: Callable[..., Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    video = make_test_media("my vod.mp4", duration_s=5.0)
    monkeypatch.setenv("AUTOCLIPPER_ARTIFACTS_DIR", str(tmp_path / "artifacts"))

    result = runner.invoke(app, ["export", str(video), "--start", "1", "--end", "3.5"])

    assert result.exit_code == ExitCode.SUCCESS, result.output
    output = tmp_path / "artifacts" / "exports" / "my_vod_1-3.5.mp4"
    assert f"Wrote {output}" in result.output
    info = probe(output)
    assert (info.video_streams[0].width, info.video_streams[0].height) == (1080, 1920)
    assert info.duration_s == pytest.approx(2.5, abs=0.05)
    again = runner.invoke(app, ["export", str(video), "--start", "1", "--end", "3.5"])
    assert again.exit_code == ExitCode.USER_ERROR  # exists; --overwrite replaces it
    replaced = runner.invoke(
        app, ["export", str(video), "--start", "1", "--end", "3.5", "--overwrite"]
    )
    assert replaced.exit_code == ExitCode.SUCCESS, replaced.output


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--start", "5", "--end", "5"], "--end must be after --start"),
        (["--start", "0", "--end", "91"], "at most 90 s"),
        (["--start", "0", "--end", "5", "--layout", "configured_region"], "EXPORT_REGION"),
    ],
    ids=["empty", "too_long", "no_region"],
)
def test_bad_export_requests_are_user_errors(tmp_path: Path, args: list[str], message: str) -> None:
    video = tmp_path / "vod.mp4"
    video.write_bytes(b"x")

    result = runner.invoke(app, ["export", str(video), *args])

    assert result.exit_code == ExitCode.USER_ERROR
    assert message in result.output


def test_a_missing_video_is_a_user_error(tmp_path: Path) -> None:
    result = runner.invoke(
        app, ["export", str(tmp_path / "nope.mp4"), "--start", "0", "--end", "2"]
    )

    assert result.exit_code == ExitCode.USER_ERROR
    assert "video file not found" in result.output


def test_failed_captions_are_an_external_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    video = tmp_path / "vod.mp4"
    video.write_bytes(b"x")

    def broken(spec: ClipSpec, **_: object) -> object:
        raise CaptioningError("captions need faster-whisper; install it with `uv sync`")

    monkeypatch.setattr("autoclipper.export.clip.render_clip", broken)

    result = runner.invoke(app, ["export", str(video), "--start", "0", "--end", "2", "--captions"])

    assert result.exit_code == ExitCode.EXTERNAL_FAILURE
    assert "faster-whisper" in result.output
