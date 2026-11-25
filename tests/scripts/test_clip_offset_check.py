"""exp-002's runner on the synthetic dataset, with a fake downloader (no network, no Twitch)."""

import json
import wave
from pathlib import Path

import clip_offset_check as coc
import polars as pl
import pytest
from typer.testing import CliRunner, Result

from autoclipper.cli import ExitCode
from autoclipper.labels import clip_offsets as co
from autoclipper.synthetic import generate_dataset

REPO = Path(__file__).resolve().parents[2]
EXP001 = REPO / "experiments" / "exp-001-chat-lag-alignment" / "config.yaml"
EXP002 = REPO / "experiments" / "exp-002-clip-offset-check" / "config.yaml"
PLANTED_SHIFT_S = -21.0


@pytest.fixture(scope="module")
def pilot(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("check") / "pilot"
    generate_dataset(root, seed=1, pilot=True)
    return root


def configs(tmp_path: Path, pilot: Path) -> Path:
    ids = pl.read_parquet(pilot / "processed" / "vods.parquet")["video_id"].to_list()
    vod_list = tmp_path / "pilot_vods.txt"
    vod_list.write_text("".join(f"{i}\n" for i in ids))
    first = EXP001.read_text(encoding="utf-8")
    for old, new in (
        ("vod_list: configs/pilot_vods.txt", f"vod_list: '{vod_list.as_posix()}'"),
        ('exclude_video_ids: ["2894413562"]', "exclude_video_ids: []"),
        ("bootstrap_resamples: 1000", "bootstrap_resamples: 20"),
        ("null_draws: 200", "null_draws: 2"),
        ("detrend_s: 300", "detrend_s: 61"),
        ("quantile: 0.995", "quantile: 0.98"),
    ):
        first = first.replace(old, new)
    exp001 = tmp_path / "exp001.yaml"
    exp001.write_text(first, encoding="utf-8")
    second = EXP002.read_text(encoding="utf-8")
    for old, new in (
        (
            "exp001_config: experiments/exp-001-chat-lag-alignment/config.yaml",
            f"exp001_config: '{exp001.as_posix()}'",
        ),
        ("per_channel: 10", "per_channel: 3"),
        ("live_per_channel: 5", "live_per_channel: 3"),
        ("search_s: 120", "search_s: 40"),
        ("bootstrap_resamples: 1000", "bootstrap_resamples: 20"),
    ):
        second = second.replace(old, new)
    config = tmp_path / "exp002.yaml"
    config.write_text(second, encoding="utf-8")
    return config


@pytest.fixture
def fake_fetch(pilot: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Cut each clip's audio from its VOD, starting `PLANTED_SHIFT_S` from the reported start."""
    clips = pl.read_parquet(next((pilot / "processed" / "clips").glob("*.parquet")))
    where = {
        r["clip_id"]: (r["video_id"], r["vod_offset_s"], r["duration_s"])
        for r in clips.iter_rows(named=True)
    }

    def fetch(clip_id: str, cache_dir: Path, _config: coc.CheckConfig) -> Path:
        video_id, reported, duration = where[clip_id]
        start = max(reported + PLANTED_SHIFT_S, 0.0)
        samples, _ = co.read_wav(
            pilot / "interim" / "audio" / f"{video_id}.wav", start, start + duration
        )
        out = cache_dir / "audio" / f"{clip_id}.wav"
        out.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(out), "wb") as writer:
            writer.setnchannels(1)
            writer.setsampwidth(2)
            writer.setframerate(co.SAMPLE_RATE_HZ)
            writer.writeframes((samples * 32767).astype("<i2").tobytes())
        return out

    monkeypatch.setattr(coc, "fetch_clip_audio", fetch)


def run(config: Path, pilot: Path, tmp_path: Path) -> Result:
    return CliRunner().invoke(
        coc.app,
        [
            "--config",
            str(config),
            "--data-dir",
            str(pilot),
            "--out-dir",
            str(tmp_path / "out"),
            "--cache-dir",
            str(tmp_path / "cache"),
            "--summary",
            str(tmp_path / "summary.json"),
        ],
    )


@pytest.mark.usefixtures("fake_fetch")
def test_the_check_recovers_the_planted_shift_and_reruns(pilot: Path, tmp_path: Path) -> None:
    result = run(configs(tmp_path, pilot), pilot, tmp_path)
    assert result.exit_code == ExitCode.SUCCESS, result.output
    summary = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    matching = summary["matching"]
    assert matching["n_strong"] >= 5
    assert matching["shift"]["all"]["median_s"] == pytest.approx(PLANTED_SHIFT_S, abs=0.05)
    assert matching["shift"]["all"]["consistent"]
    assert summary["rerun"]["shift_s_applied"]["live"] == pytest.approx(PLANTED_SHIFT_S, abs=0.05)
    assert set(summary["rerun"]) == {"shift_s_applied", "reported_positions", "corrected_positions"}
    assert "top_with_neither" in summary["rerun"]["corrected_positions"]["alignment"]
    # Clip IDs stay in the git-ignored per-clip table, never in the summary.
    matches = pl.read_parquet(tmp_path / "out" / "matches.parquet")
    text = (tmp_path / "summary.json").read_text(encoding="utf-8")
    assert matches.height == summary["selection"]["n_selected"]
    assert not [c for c in matches["clip_id"].to_list() if c in text]


def test_a_failed_download_is_counted_not_fatal(
    pilot: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(_clip_id: str, _cache: Path, _config: coc.CheckConfig) -> Path:
        raise OSError("no network")

    monkeypatch.setattr(coc, "fetch_clip_audio", broken)
    result = run(configs(tmp_path, pilot), pilot, tmp_path)
    assert result.exit_code == ExitCode.SUCCESS, result.output
    summary = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert summary["selection"]["n_fetch_failed"] == summary["selection"]["n_selected"]
    assert summary["matching"]["n_strong"] == 0
    assert summary["rerun"] == {"shift_s_applied": None}


def test_a_bad_config_is_a_user_error(pilot: Path, tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("experiment: x\n", encoding="utf-8")
    result = run(bad, pilot, tmp_path)
    assert result.exit_code == ExitCode.USER_ERROR
    assert "missing keys" in result.output


def test_rerun_shifts_prefers_a_consistent_class_median() -> None:
    def stats(median: float, n: int, consistent: bool) -> dict[str, object]:
        return {"median_s": median, "n_clips": n, "consistent": consistent}

    summary = {
        "shift": {
            "all": stats(-20.0, 40, True),
            "live": stats(-22.0, 25, True),
            "post_stream": stats(-1.0, 3, True),
        }
    }
    assert coc.rerun_shifts(summary) == {"live": -22.0, "post_stream": -20.0}
    summary["shift"]["all"] = stats(-20.0, 40, False)
    assert coc.rerun_shifts(summary) == {"live": -22.0}
    summary["shift"]["live"] = stats(-22.0, 25, False)
    assert coc.rerun_shifts(summary) is None
