"""exp-003's runner on the synthetic dataset, with a fake downloader (no network, no Twitch)."""

import json
import wave
from pathlib import Path

import clip_end_confirmation as cec
import clip_offset_check as coc
import polars as pl
import pytest
from typer.testing import CliRunner, Result

from autoclipper.cli import ExitCode
from autoclipper.labels import clip_offsets as co
from autoclipper.synthetic import generate_dataset

REPO = Path(__file__).resolve().parents[2]
EXP001 = REPO / "experiments" / "exp-001-chat-lag-alignment" / "config.yaml"
EXP003 = REPO / "experiments" / "exp-003-clip-end-confirmation" / "config.yaml"


@pytest.fixture(scope="module")
def pilot(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("confirm") / "pilot"
    generate_dataset(root, seed=1, pilot=True)
    return root


def clips_of(pilot: Path) -> pl.DataFrame:
    return pl.read_parquet(next((pilot / "processed" / "clips").glob("*.parquet")))


def setup(tmp_path: Path, pilot: Path, *, exclude: list[str]) -> tuple[Path, Path]:
    """Configs pointing at the synthetic data, and an artifacts dir with exp-002's clip table."""
    ids = pl.read_parquet(pilot / "processed" / "vods.parquet")["video_id"].to_list()
    vod_list = tmp_path / "pilot_vods.txt"
    vod_list.write_text("".join(f"{i}\n" for i in ids))
    first = EXP001.read_text(encoding="utf-8")
    first = first.replace("vod_list: configs/pilot_vods.txt", f"vod_list: '{vod_list.as_posix()}'")
    first = first.replace('exclude_video_ids: ["2894413562"]', "exclude_video_ids: []")
    exp001 = tmp_path / "exp001.yaml"
    exp001.write_text(first, encoding="utf-8")
    third = EXP003.read_text(encoding="utf-8")
    third = third.replace(
        "exp001_config: experiments/exp-001-chat-lag-alignment/config.yaml",
        f"exp001_config: '{exp001.as_posix()}'",
    )
    third = third.replace("search_s: 120", "search_s: 40")
    third = third.replace("bootstrap_resamples: 1000", "bootstrap_resamples: 20")
    config = tmp_path / "exp003.yaml"
    config.write_text(third, encoding="utf-8")
    artifacts = tmp_path / "artifacts"
    reports = artifacts / "reports" / "exp-002-clip-offset-check"
    reports.mkdir(parents=True)
    table = pl.DataFrame({"clip_id": exclude}, schema={"clip_id": pl.String})
    table.write_parquet(reports / "matches.parquet")
    return config, artifacts


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
    """Cut each clip's audio so it ends at (or, if not `end_anchored`, starts at) vod_offset."""
    where = {
        r["clip_id"]: (r["video_id"], r["vod_offset_s"], r["duration_s"])
        for r in clips_of(pilot).iter_rows(named=True)
    }

    def fetch(clip_id: str, cache_dir: Path, _config: coc.CheckConfig) -> Path:
        video_id, reported, duration = where[clip_id]
        start = max(reported - duration, 0.0) if end_anchored else reported
        wav = pilot / "interim" / "audio" / f"{video_id}.wav"
        samples, _ = co.read_wav(wav, start, start + duration)
        out = cache_dir / "audio" / f"{clip_id}.wav"
        out.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(out), "wb") as writer:
            writer.setnchannels(1)
            writer.setsampwidth(2)
            writer.setframerate(co.SAMPLE_RATE_HZ)
            writer.writeframes((samples * 32767).astype("<i2").tobytes())
        return out

    monkeypatch.setattr(coc, "fetch_clip_audio", fetch)


def run(config: Path, pilot: Path, artifacts: Path, summary: Path) -> Result:
    args = ["--config", str(config), "--data-dir", str(pilot)]
    args += ["--artifacts-dir", str(artifacts), "--summary", str(summary)]
    return CliRunner().invoke(cec.app, args)


@pytest.mark.parametrize(("end_anchored", "expected"), [(True, "PASS"), (False, "FAIL")])
def test_the_rule_passes_only_when_clips_end_at_the_reported_position(
    pilot: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    end_anchored: bool,
    expected: str,
) -> None:
    ranked = clips_of(pilot).drop_nulls("vod_offset_s").sort("view_count", descending=True)
    used = sorted(ranked["clip_id"].head(2).to_list())
    config, artifacts = setup(tmp_path, pilot, exclude=used)
    plant(pilot, monkeypatch, end_anchored=end_anchored)
    result = run(config, pilot, artifacts, tmp_path / "summary.json")
    assert result.exit_code == ExitCode.SUCCESS, result.output
    assert f"{expected}: true clip end = vod_offset_s" in result.output
    summary = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert summary["evaluation"]["result"] == expected
    assert summary["selection"]["n_excluded_from_exp002"] == 2
    assert summary["selection"]["n_overlap_with_exp002"] == 0
    report = artifacts / "reports" / "exp-003-clip-end-confirmation" / "matches.parquet"
    matches = pl.read_parquet(report)
    assert matches.height > 0
    assert not set(matches["clip_id"].to_list()) & set(used)
    assert summary["sensitivity_audio_length"]["length"] == "audio"
    assert summary["evaluation"]["length"] == "metadata"
    assert set(summary["selection"]["cell_counts"]) == {
        "channel_order",
        "expected",
        "selected",
        "match",
    }
    assert leaked(tmp_path / "summary.json", pilot) == []


def test_the_exp002_clip_table_is_required(pilot: Path, tmp_path: Path) -> None:
    config, artifacts = setup(tmp_path, pilot, exclude=[])
    (artifacts / "reports" / "exp-002-clip-offset-check" / "matches.parquet").unlink()
    result = run(config, pilot, artifacts, tmp_path / "summary.json")
    assert result.exit_code == ExitCode.USER_ERROR
    assert "clips to exclude" in result.output


def test_the_committed_config_states_the_criteria() -> None:
    config = cec.EndConfig.from_yaml(EXP003)
    assert config.rule == "true clip end = vod_offset_s"
    criteria = (
        config.tolerance_s,
        config.max_median_abs_error_s,
        config.min_share_within_tolerance,
    )
    assert criteria == (2.0, 2.0, 0.7)
    assert config.exclude_experiment == "exp-002-clip-offset-check"


def test_cell_count_check_follows_the_channel_order_of_the_list() -> None:
    def row(login: str, stratum: str, clip_class: str) -> dict[str, str]:
        return {"login": login, "stratum": stratum, "clip_class": clip_class}

    # (top-live, top-post, low-live, low-post): 3/2/2/3 for the 1st channel, 2/3/3/2 for the 2nd.
    first = [row("b", "top", "live")] * 3 + [row("b", "top", "post_stream")] * 2
    first += [row("b", "low", "live")] * 2 + [row("b", "low", "post_stream")] * 3
    second = [row("a", "top", "live")] * 2 + [row("a", "top", "post_stream")] * 3
    second += [row("a", "low", "live")] * 3 + [row("a", "low", "post_stream")] * 2
    clips = pl.DataFrame(first + second)
    check = cec.cell_count_check(clips, channel_order=["b", "a"])
    assert check["match"]
    assert check["expected"] == {"b": [3, 2, 2, 3], "a": [2, 3, 3, 2]}
    # The same clips read in the other channel order don't match.
    assert not cec.cell_count_check(clips, channel_order=["a", "b"])["match"]
