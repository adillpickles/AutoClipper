"""The #36 study runner on the synthetic five-streamer dataset (no real data, no network)."""

import json
from pathlib import Path

import pilot_alignment as pa
import polars as pl
import pytest
from typer.testing import CliRunner, Result

from autoclipper.cli import ExitCode
from autoclipper.synthetic import generate_dataset

REPO = Path(__file__).resolve().parents[2]
CONFIG = REPO / "experiments" / "exp-001-chat-lag-alignment" / "config.yaml"


@pytest.fixture(scope="module")
def pilot(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("runner") / "pilot"
    generate_dataset(root, seed=1, pilot=True)
    return root


def write_config(tmp_path: Path, ids: list[str], *, exclude: list[str]) -> Path:
    vod_list = tmp_path / "pilot_vods.txt"
    vod_list.write_text("".join(f"https://www.twitch.tv/videos/{i}\n" for i in ids))
    text = CONFIG.read_text(encoding="utf-8")
    text = text.replace("vod_list: configs/pilot_vods.txt", f"vod_list: '{vod_list.as_posix()}'")
    text = text.replace('exclude_video_ids: ["2894413562"]', f"exclude_video_ids: {exclude}")
    text = text.replace("bootstrap_resamples: 1000", "bootstrap_resamples: 30")
    text = text.replace("null_draws: 200", "null_draws: 3")
    text = text.replace("detrend_s: 300", "detrend_s: 61")
    text = text.replace("quantile: 0.995", "quantile: 0.98")
    config = tmp_path / "config.yaml"
    config.write_text(text, encoding="utf-8")
    return config


def run(config: Path, data: Path, tmp_path: Path) -> Result:
    return CliRunner().invoke(
        pa.app,
        [
            "--config",
            str(config),
            "--data-dir",
            str(data),
            "--out-dir",
            str(tmp_path / "out"),
            "--cache-dir",
            str(tmp_path / "cache"),
            "--summary",
            str(tmp_path / "summary.json"),
        ],
    )


def ids_of(pilot: Path) -> list[str]:
    return pl.read_parquet(pilot / "processed" / "vods.parquet")["video_id"].to_list()


def test_the_runner_writes_the_summary_and_tables(pilot: Path, tmp_path: Path) -> None:
    config = write_config(tmp_path, ids_of(pilot), exclude=[])
    result = run(config, pilot, tmp_path)
    assert result.exit_code == ExitCode.SUCCESS, result.output
    assert "6 VODs" in result.output
    summary = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert summary == json.loads((tmp_path / "out" / "summary.json").read_text(encoding="utf-8"))
    provenance = summary["provenance"]
    assert len(provenance["vod_list_sha256"]) == 64
    assert provenance["git_commit"]
    for name in ("per_vod", "xcorr", "profiles", "padding", "sensitivity"):
        assert pl.read_parquet(tmp_path / "out" / f"{name}.parquet").height > 0


def test_the_runner_refuses_an_excluded_vod(pilot: Path, tmp_path: Path) -> None:
    config = write_config(tmp_path, ids_of(pilot), exclude=["9100000003"])
    result = run(config, pilot, tmp_path)
    assert result.exit_code == ExitCode.USER_ERROR
    assert "lists excluded VODs: 9100000003" in result.output
    assert not (tmp_path / "summary.json").exists()


def test_the_runner_reports_bad_inputs(pilot: Path, tmp_path: Path) -> None:
    config = write_config(tmp_path, ["1234"], exclude=[])
    result = run(config, pilot, tmp_path)
    assert result.exit_code == ExitCode.USER_ERROR
    assert "VOD 1234 has 0 rows" in result.output
    bad = tmp_path / "bad.yaml"
    bad.write_text("experiment: x\n", encoding="utf-8")
    assert run(bad, pilot, tmp_path).exit_code == ExitCode.USER_ERROR
