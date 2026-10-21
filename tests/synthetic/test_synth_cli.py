"""Tests for the `autoclipper synth` command."""

import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from autoclipper.cli import ExitCode, app

runner = CliRunner()
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _plain(text: str) -> str:
    return _ANSI_ESCAPE.sub("", text)


def test_help_describes_the_options() -> None:
    result = runner.invoke(app, ["synth", "--help"])

    assert result.exit_code == ExitCode.SUCCESS
    for option in ("--out", "--seed", "--no-video", "--video-duration", "--force"):
        assert option in _plain(result.output)


def test_writes_the_dataset_and_refuses_to_overwrite_it(tmp_path: Path) -> None:
    out = tmp_path / "synthetic"

    first = runner.invoke(app, ["synth", "--out", str(out), "--seed", "1", "--no-video"])
    assert first.exit_code == ExitCode.SUCCESS, first.output
    assert f"Wrote 11 files to {out}" in first.stdout
    assert (out / "processed" / "clips" / "synthetic-seed-1.parquet").is_file()

    again = runner.invoke(app, ["synth", "--out", str(out), "--seed", "1", "--no-video"])
    forced = runner.invoke(app, ["synth", "--out", str(out), "--no-video", "--force"])

    assert again.exit_code == ExitCode.USER_ERROR
    assert "already holds a synthetic dataset" in again.stderr
    assert forced.exit_code == ExitCode.SUCCESS, forced.output
    assert (out / "processed" / "clips" / "synthetic-seed-0.parquet").is_file()


def test_refuses_a_non_empty_directory(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("keep me", encoding="utf-8")

    result = runner.invoke(app, ["synth", "--out", str(tmp_path), "--no-video", "--force"])

    assert result.exit_code == ExitCode.USER_ERROR
    assert "is not empty" in result.stderr
    assert [p.name for p in tmp_path.iterdir()] == ["notes.txt"]


def test_writes_under_the_data_dir_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTOCLIPPER_DATA_DIR", str(tmp_path / "my-data"))

    result = runner.invoke(app, ["synth", "--seed", "3", "--no-video"])

    assert result.exit_code == ExitCode.SUCCESS, result.output
    assert (tmp_path / "my-data" / "synthetic" / "seed-3" / "manifest.json").is_file()


@pytest.mark.parametrize("args", [["--seed", "-1"], ["--video-duration", "0"]])
def test_invalid_values_are_user_errors(args: list[str], tmp_path: Path) -> None:
    result = runner.invoke(app, ["synth", "--out", str(tmp_path / "out"), *args])

    assert result.exit_code == ExitCode.USER_ERROR
    assert not (tmp_path / "out").exists()


@pytest.mark.ffmpeg
def test_generates_videos_with_ffmpeg(tmp_path: Path) -> None:
    result = runner.invoke(app, ["synth", "--out", str(tmp_path / "out"), "--video-duration", "1"])

    assert result.exit_code == ExitCode.SUCCESS, result.output
    assert len(list((tmp_path / "out" / "raw" / "video").glob("*.mp4"))) == 3
