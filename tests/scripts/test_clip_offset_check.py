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


def string_values(value: object) -> list[str]:
    """Every string in a JSON value, keys included."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for k, v in value.items() for s in (k, *string_values(v))]
    if isinstance(value, list):
        return [s for v in value for s in string_values(v)]
    return []


def leaked(summary_path: Path, pilot: Path) -> list[str]:
    """Clip IDs, titles, creator or chatter pseudonyms or chat messages found in a summary."""
    text = summary_path.read_text(encoding="utf-8")
    strings = set(string_values(json.loads(text)))
    clips = pl.read_parquet(next((pilot / "processed" / "clips").glob("*.parquet")))
    chat = pl.concat(
        [pl.read_parquet(p) for p in sorted((pilot / "interim" / "chat").glob("*.parquet"))]
    )
    anywhere = {*clips["clip_id"].to_list(), *clips["creator_hash"].to_list()}
    anywhere |= set(chat["user_hash"].to_list())
    exact = {*clips["title"].to_list(), *chat["text"].to_list()}
    return sorted({s for s in anywhere if s in text} | (exact & strings))


def plant(pilot: Path, monkeypatch: pytest.MonkeyPatch, *, end_anchored: bool) -> None:
    """Cut each clip's audio from its VOD: `PLANTED_SHIFT_S` from the reported start, or (when
    `end_anchored`) ending at the reported position, as if Twitch reported clips' ends."""
    clips = pl.read_parquet(next((pilot / "processed" / "clips").glob("*.parquet")))
    where = {
        r["clip_id"]: (r["video_id"], r["vod_offset_s"], r["duration_s"])
        for r in clips.iter_rows(named=True)
    }

    def fetch(clip_id: str, cache_dir: Path, _config: coc.CheckConfig) -> Path:
        video_id, reported, duration = where[clip_id]
        start = max(reported - duration if end_anchored else reported + PLANTED_SHIFT_S, 0.0)
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


@pytest.fixture
def fake_fetch(pilot: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plant(pilot, monkeypatch, end_anchored=False)


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
    rule = summary["rerun"]["rule"]
    assert rule["rule"] == "constant"
    assert rule["shift_s"]["live"] == pytest.approx(PLANTED_SHIFT_S, abs=0.05)
    assert set(summary["rerun"]) == {"rule", "reported_positions", "corrected_positions"}
    assert "top_with_neither" in summary["rerun"]["corrected_positions"]["alignment"]
    # Clip IDs stay in the git-ignored per-clip table, never in the summary.
    matches = pl.read_parquet(tmp_path / "out" / "matches.parquet")
    assert matches.height == summary["selection"]["n_selected"]
    assert "clip_length_s" in matches.columns
    assert matches["clip_length_s"].drop_nulls().len() == matches.height
    assert summary["matching"]["end_residual_metadata"]["all"] is not None
    # Clip IDs, titles and pseudonyms stay out of the summary (the clip table keeps the IDs).
    assert leaked(tmp_path / "summary.json", pilot) == []


def test_clips_reported_at_their_end_select_the_end_anchored_rule(
    pilot: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plant(pilot, monkeypatch, end_anchored=True)
    result = run(configs(tmp_path, pilot), pilot, tmp_path)
    assert result.exit_code == ExitCode.SUCCESS, result.output
    summary = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    residual = summary["matching"]["end_residual"]["all"]
    assert residual["median_s"] == pytest.approx(0.0, abs=0.05)
    assert residual["iqr_s"] < summary["matching"]["shift"]["all"]["iqr_s"]
    assert summary["matching"]["shift_vs_duration"]["slope"] == pytest.approx(-1.0, abs=0.05)
    rule = summary["rerun"]["rule"]
    assert (rule["rule"], rule["per_duration"]) == ("end_anchored", -1.0)


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
    assert summary["rerun"] == {"rule": None}


def test_a_bad_config_is_a_user_error(pilot: Path, tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("experiment: x\n", encoding="utf-8")
    result = run(bad, pilot, tmp_path)
    assert result.exit_code == ExitCode.USER_ERROR
    assert "missing keys" in result.output


def test_rerun_shifts_prefers_a_consistent_class_median() -> None:
    def stats(median: float, n: int, consistent: bool) -> dict[str, object]:
        return {"median_s": median, "n_clips": n, "consistent": consistent}

    groups = {
        "all": stats(-20.0, 40, True),
        "live": stats(-22.0, 25, True),
        "post_stream": stats(-1.0, 3, True),
    }
    assert coc.rerun_shifts(groups) == {"live": -22.0, "post_stream": -20.0}
    groups["all"] = stats(-20.0, 40, False)
    assert coc.rerun_shifts(groups) == {"live": -22.0}
    groups["live"] = stats(-22.0, 25, False)
    assert coc.rerun_shifts(groups) is None


def test_rerun_rule_picks_the_consistent_rule_with_the_smaller_spread() -> None:
    def group(median: float, iqr: float) -> dict[str, object]:
        entry = {"median_s": median, "n_clips": 25, "consistent": iqr <= 10, "iqr_s": iqr}
        return {"all": entry, "live": entry, "post_stream": entry}

    tight_end = {"shift": group(-29.0, 8.0), "end_residual_metadata": group(0.5, 1.0)}
    rule = coc.rerun_rule(tight_end)
    assert rule is not None
    assert (rule["rule"], rule["per_duration"], rule["shift_s"]["live"]) == (
        "end_anchored",
        -1.0,
        0.5,
    )
    tight_shift = {"shift": group(-21.0, 0.5), "end_residual_metadata": group(5.0, 6.0)}
    rule = coc.rerun_rule(tight_shift)
    assert rule is not None
    assert rule["rule"] == "constant"
    assert (
        coc.rerun_rule({"shift": group(-29.0, 28.0), "end_residual_metadata": group(0.0, 12.0)})
        is None
    )
