import dataclasses
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl
import pytest

from autoclipper.features.chat import load_lexicon
from autoclipper.labels import alignment_study as study
from autoclipper.labels.alignment_study import StudyConfig, StudyError
from autoclipper.synthetic import generate_dataset
from autoclipper.synthetic.scenario import build_scenario

REPO = Path(__file__).resolve().parents[2]
LEXICON = REPO / "configs" / "lexicon.yaml"
CONFIG = REPO / "experiments" / "exp-001-chat-lag-alignment" / "config.yaml"


def small_config(**changes: Any) -> StudyConfig:  # noqa: ANN401 - StudyConfig field values.
    """The experiment's config, sized for the short synthetic VODs."""
    config = dataclasses.replace(
        StudyConfig.from_yaml(CONFIG),
        bootstrap_resamples=50,
        null_draws=5,
        detrend_s=61,
        loud_event_quantile=0.98,
        **changes,
    )
    config.check()
    return config


@pytest.fixture(scope="module")
def pilot(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("study") / "pilot"
    generate_dataset(root, seed=1, pilot=True)
    return root


def video_ids(root: Path) -> list[str]:
    return pl.read_parquet(root / "processed" / "vods.parquet")["video_id"].to_list()


def load(root: Path, ids: list[str], cache: Path) -> list[study.VodData]:
    return study.load_collection(root, ids, lexicon=load_lexicon(LEXICON), cache_dir=cache)


def test_the_experiment_config_loads() -> None:
    config = StudyConfig.from_yaml(CONFIG)
    assert config.vod_list == "configs/pilot_vods.txt"
    assert config.exclude_video_ids == ("2894413562",)
    assert (config.window_s, config.z, config.match_tolerance_s) == (10.0, 3.0, 15.0)
    assert config.as_dict()["loud_event"] == {"quantile": 0.995, "suppress_s": 60.0}
    assert config.offsets[0] == config.profile_range_s[0]


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ("seed: 42\n", "seed: 42\nsurprise: 1\n", "unknown keys surprise"),
        ("lag_range_s: [-30, 60]", "lag_range_s: [-90, 60]", "nest"),
        ("  window_s: 10\n", "  window_s: 2.5\n", "whole multiples"),
        ("smooth_s: 5", "smooth_s: 4", "odd"),
        ("quantile: 0.995", "quantile: 1.5", "quantile"),
        ("seed: 42\n", "", "seed"),
    ],
)
def test_bad_configs_are_rejected(tmp_path: Path, old: str, new: str, message: str) -> None:
    text = CONFIG.read_text(encoding="utf-8")
    assert old in text
    path = tmp_path / "config.yaml"
    path.write_text(text.replace(old, new, 1), encoding="utf-8")
    with pytest.raises(StudyError, match=message):
        StudyConfig.from_yaml(path)


def test_a_config_must_be_a_mapping(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("- a list\n", encoding="utf-8")
    with pytest.raises(StudyError, match="mapping"):
        StudyConfig.from_yaml(path)


def test_place_clips_drops_clips_past_the_end_and_cuts_long_ones() -> None:
    clips = pl.DataFrame(
        {
            "clip_id": ["a", "b", "c", "d"],
            "vod_offset_s": [10.0, 95.0, 100.0, None],
            "duration_s": [30.0, 30.0, 20.0, 30.0],
        }
    )
    placed, without, dropped, cut = study.place_clips(clips, 100.0)
    assert (without, dropped, cut) == (1, 1, 1)
    assert placed["clip_id"].to_list() == ["a", "b"]
    assert placed["duration_s"].to_list() == [30.0, 5.0]


def test_load_collection_reads_the_synthetic_snapshot(pilot: Path, tmp_path: Path) -> None:
    ids = video_ids(pilot)
    vods = load(pilot, ids, tmp_path / "cache")
    assert [v.video_id for v in vods] == ids
    unclipped = next(v for v in vods if v.video_id == "9100000003")
    assert (unclipped.moments.height, unclipped.n_clips) == (0, 0)
    first = vods[0]
    assert first.n_clips_without_offset == 1
    # The study reads reported positions as starts on purpose. Synthetic clips report their
    # ends (#126), so read as starts the planted touching pair lands 8 s apart (past the 5 s
    # merge gap) and splits: 4 moments where the pipeline's end reading finds the planted 3.
    assert first.moments.height == 4
    assert first.moments["clip_position"].unique().to_list() == ["start"]
    assert first.series.height == int(first.duration_s)
    assert first.login == "user_001"
    assert (tmp_path / "cache" / f"{first.video_id}.parquet").is_file()


def test_load_collection_uses_per_vod_manifests(pilot: Path, tmp_path: Path) -> None:
    """The ingest layout: one ``<snapshot_id>.json`` + ``.parquet`` per VOD, no root manifest."""
    root = tmp_path / "ingest"
    for path in pilot.rglob("*"):
        if path.is_file():
            target = root / path.relative_to(pilot)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(path.read_bytes())
    (root / "manifest.json").unlink()
    combined = next((root / "processed" / "clips").glob("*.parquet"))
    clips = pl.read_parquet(combined)
    combined.unlink()
    for video_id in video_ids(root):
        snapshot = combined.with_name(f"{video_id}-20260920T000000Z.parquet")
        clips.filter(pl.col("video_id") == video_id).write_parquet(snapshot)
        snapshot.with_suffix(".json").write_text(
            json.dumps({"video_id": video_id, "file": snapshot.name}), encoding="utf-8"
        )
    vods = load(root, video_ids(root), tmp_path / "cache")
    assert sum(v.moments.height for v in vods) > 0
    assert next(v for v in vods if v.video_id == "9100000003").n_clips == 0

    # A second snapshot of one VOD is ambiguous.
    extra = combined.with_name("9100000001-20260921T000000Z.json")
    extra.write_text(json.dumps({"video_id": "9100000001"}), encoding="utf-8")
    with pytest.raises(StudyError, match="2 clip snapshots"):
        load(root, ["9100000001"], tmp_path / "c2")


def test_load_collection_rejects_bad_inputs(pilot: Path, tmp_path: Path) -> None:
    with pytest.raises(StudyError, match="repeats"):
        load(pilot, ["9100000001"] * 2, tmp_path)
    with pytest.raises(StudyError, match="0 rows"):
        load(pilot, ["1234"], tmp_path)


def test_load_collection_needs_a_snapshot_chat_and_audio(pilot: Path, tmp_path: Path) -> None:
    root = tmp_path / "copy"
    for path in pilot.rglob("*"):
        if path.is_file():
            target = root / path.relative_to(pilot)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(path.read_bytes())
    (root / "interim" / "audio" / "9100000002.wav").unlink()
    with pytest.raises(StudyError, match=r"9100000002\.wav is missing"):
        load(root, ["9100000002"], tmp_path / "cache")
    (root / "manifest.json").unlink()
    with pytest.raises(StudyError, match="no clip snapshot"):
        load(root, ["9100000001"], tmp_path / "cache")


def test_load_collection_reuses_the_series_cache(
    pilot: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = load(pilot, ["9100000001"], tmp_path)

    def fail(*_args: object, **_kwargs: object) -> pl.DataFrame:
        raise AssertionError("the cache should have been used")

    monkeypatch.setattr(study.al, "series_frame", fail)
    again = load(pilot, ["9100000001"], tmp_path)
    assert again[0].series.equals(first[0].series)


@pytest.fixture(scope="module")
def vods(pilot: Path, tmp_path_factory: pytest.TempPathFactory) -> list[study.VodData]:
    return load(pilot, video_ids(pilot), tmp_path_factory.mktemp("cache"))


@pytest.fixture(scope="module")
def result(vods: list[study.VodData]) -> study.StudyResult:
    return study.run_study(vods, small_config())


def planted_offset_s() -> float:
    """Mean gap between the middles of each planted loud burst and its chat burst (8.8 s)."""
    events = [e for v in build_scenario(1, pilot=True).vods for e in v.events]
    gaps = [(sum(e.chat_burst_s) - sum(e.audio_burst_s)) / 2 for e in events]
    return sum(gaps) / len(gaps)


def test_chat_follows_loud_events_by_the_planted_delay(result: study.StudyResult) -> None:
    # The loud event is the burst's loudest second and profiles are smoothed over 5 s, so the
    # peak lands within a couple of seconds of the planted middle-to-middle gap.
    expected = planted_offset_s()
    delay = result.summary["chat_delay"]
    pooled = delay["loud_event"]["all"]
    assert abs(pooled["peak_offset_s"] - expected) <= 2.5
    assert pooled["peak_height_z"] > pooled["chance_peak_height_p95_z"]
    assert delay["shift_lag_s"] == pooled["peak_offset_s"]
    assert abs(delay["whole_vod_xcorr"]["all"]["median"] - expected) <= 2.5
    # Smoothing (5 s) moves a loud event's own peak toward the middle of its 2.5-4 s burst.
    loud = result.summary["profiles"]["loudness"]["loud_event"]["all"]["all"]
    assert abs(loud["peak_offset_s"]) <= 3


def test_run_study_summarizes_every_part(result: study.StudyResult) -> None:
    summary = result.summary
    inputs = summary["inputs"]
    assert inputs["n_vods"] == 6
    assert inputs["channels"] == {"user_001": 2, **{f"user_00{i}": 1 for i in range(2, 6)}}
    assert inputs["n_post_stream_moments"] == 0
    assert inputs["n_loud_events"] > 0
    assert set(summary["profiles"]["chat"]) == set(study.ANCHORS)
    assert set(summary["profiles"]["chat"]["top_clip_start"]) == {"all", "live", "post_stream"}
    assert set(summary["profiles"]["chat"]["loud_event"]) == {"all"}
    main = summary["alignment"]["all"]
    assert 0 <= main["chance_near_moment"]["mean"] <= 1
    assert summary["padding"]["all"]["n_clips"] == result.padding.height
    shares = summary["padding"]["all"]["loud"]
    assert sum(shares[f"chance_share_{p}"] for p in ("before_start", "inside", "after_end")) == (
        pytest.approx(1.0)
    )
    assert set(result.sensitivity["window_s"].unique().to_list()) == {5.0, 10.0, 15.0, 20.0}
    assert result.per_vod.height == 6
    assert result.xcorr["lag_s"].min() == -30
    assert summary["clip_timing"]["n_post_stream_clips"] == 0
    # Every number the write-ups cite is in the summary, not only in the artifacts.
    assert len(summary["sensitivity"]) == 12  # 4 window sizes x 3 thresholds, all VODs
    assert {row["group"] for row in summary["sensitivity"]} == {"all"}
    xcorr = summary["chat_delay"]["whole_vod_xcorr"]["all"]
    assert xcorr["peak_corr_min"] <= xcorr["peak_corr_max"] <= 1
    assert main["chat_lift"] is None or main["chat_lift"]["n_vods"] <= 6


def test_the_summary_holds_no_chat_text_or_ids(result: study.StudyResult, pilot: Path) -> None:
    text = json.dumps(result.summary)
    chat = pl.concat([pl.read_parquet(p) for p in (pilot / "interim" / "chat").glob("*.parquet")])
    clips = pl.read_parquet(next((pilot / "processed" / "clips").glob("*.parquet")))
    words = {t for t in chat["text"].to_list() if len(t) > 3} | set(clips["title"].to_list())
    assert not [w for w in words if f'"{w}"' in text]
    assert not [h for h in chat["user_hash"].unique().to_list() if h in text]
    assert not [c for c in clips["clip_id"].to_list() if c in text]


def test_run_study_is_repeatable(vods: list[study.VodData], result: study.StudyResult) -> None:
    again = study.run_study(vods, small_config())
    assert study.rounded(again.summary) == study.rounded(result.summary)


def test_post_stream_clips_are_classified_by_the_vods_end(vods: list[study.VodData]) -> None:
    vod = vods[0]
    first_moment = vod.moments["clip_ids"][0].to_list()
    clips = vod.clips.with_columns(
        created_at=pl.when(pl.col("clip_id").is_in(first_moment))
        .then(pl.lit(datetime(2030, 1, 1, tzinfo=UTC)))
        .otherwise(pl.col("created_at"))
    )
    summary = study.run_study([dataclasses.replace(vod, clips=clips)], small_config()).summary
    assert summary["inputs"]["n_post_stream_moments"] == 1
    assert summary["inputs"]["n_live_moments"] == vod.moments.height - 1
    assert summary["clip_timing"]["n_post_stream_clips"] == len(first_moment)


def test_clip_timing_compares_creation_with_the_reported_position(
    vods: list[study.VodData],
) -> None:
    vod = vods[0]
    start = vod.clips["ended_at"][0] - timedelta(seconds=vod.duration_s)
    clips = pl.DataFrame(
        {
            "clip_id": ["a", "b"],
            "vod_offset_s": [100.0, 50.0],
            "duration_s": [30.0, 30.0],
            # a: created 10 s after its reported end. b: 10 s before it (impossible if exact).
            "created_at": [start + timedelta(seconds=140), start + timedelta(seconds=70)],
            "ended_at": [vod.clips["ended_at"][0]] * 2,
        }
    )
    timing = study.clip_timing([dataclasses.replace(vod, clips=clips)])
    assert timing["n_live_clips"] == 2
    assert timing["share_created_before_reported_end"] == 0.5
    assert timing["share_created_0_to_10s_after_reported_start"] == 0.0
    assert timing["by_clip_length"]["30s"]["created_after_end_s_p50"] == 0.0
    assert timing["created_after_reported_start_s"]["p50"] == 30.0


def test_run_study_needs_vods() -> None:
    with pytest.raises(StudyError, match="no VODs"):
        study.run_study([], small_config())


def test_rounded_rounds_floats_and_drops_non_finite() -> None:
    assert study.rounded({"a": [1.23456, float("nan")], "b": (2, "x")}, 2) == {
        "a": [1.23, None],
        "b": [2, "x"],
    }
