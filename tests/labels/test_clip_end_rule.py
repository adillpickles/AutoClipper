"""exp-003's selection and pass/fail evaluation (`autoclipper.labels.clip_offsets`)."""

import dataclasses
import wave
from datetime import timedelta
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl
import pytest

from autoclipper.features.chat import load_lexicon
from autoclipper.labels import alignment_study as study
from autoclipper.labels import clip_offsets as co
from autoclipper.synthetic import generate_dataset

LEXICON = Path(__file__).resolve().parents[2] / "configs" / "lexicon.yaml"


@pytest.fixture(scope="module")
def pilot(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, list[study.VodData]]:
    root = tmp_path_factory.mktemp("rule") / "pilot"
    generate_dataset(root, seed=1, pilot=True)
    ids = pl.read_parquet(root / "processed" / "vods.parquet")["video_id"].to_list()
    vods = study.load_collection(
        root, ids, lexicon=load_lexicon(LEXICON), cache_dir=root.parent / "cache"
    )
    return root, vods


def test_select_stratified_clips_fills_cells_and_honors_exclusions(
    pilot: tuple[Path, list[study.VodData]],
) -> None:
    root, vods = pilot
    first = vods[0]
    late = max(first.clips["created_at"].to_list()) + timedelta(days=30)
    half_post = first.clips.with_columns(
        created_at=pl.when(pl.int_range(pl.len()) % 2 == 0)
        .then(pl.lit(late))
        .otherwise(pl.col("created_at"))
    )
    vods = [dataclasses.replace(first, clips=half_post), *vods[1:]]
    wavs = {v.video_id: root / "interim" / "audio" / f"{v.video_id}.wav" for v in vods}
    excluded = {first.clips.sort("view_count", descending=True)["clip_id"][0]}

    def pick(seed: int) -> co.Selection:
        return co.select_stratified_clips(
            vods,
            wav_paths=wavs,
            muted={},
            exclude=excluded,
            cell_counts=[(1, 1, 1, 1)],
            max_per_vod=9,
            max_silent_fraction=0.5,
            seed=seed,
        )

    clips = pick(3).clips
    assert not set(clips["clip_id"].to_list()) & excluded
    assert clips["clip_id"].n_unique() == clips.height
    assert set(clips["stratum"].to_list()) <= {"top", "low"}
    channel = clips.filter(pl.col("login") == first.login)
    cells = {(r["stratum"], r["clip_class"]) for r in channel.iter_rows(named=True)}
    assert {("top", "live"), ("top", "post_stream")} <= cells
    views = co.clip_candidates(vods).filter(pl.col("login") == first.login)["view_count"]
    median = float(np.median(views.to_numpy()))
    assert all(v <= median for v in channel.filter(pl.col("stratum") == "low")["view_count"])
    assert pick(3).clips.equals(clips)


def test_select_stratified_clips_skips_clips_silent_at_either_reading(
    pilot: tuple[Path, list[study.VodData]], tmp_path: Path
) -> None:
    _, vods = pilot
    vod = vods[0]
    silent = tmp_path / "silent.wav"
    with wave.open(str(silent), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(co.SAMPLE_RATE_HZ)
        writer.writeframes(np.zeros(int(vod.duration_s * co.SAMPLE_RATE_HZ), "<i2").tobytes())
    selection = co.select_stratified_clips(
        [vod],
        wav_paths={vod.video_id: silent},
        muted={},
        exclude=set(),
        cell_counts=[(2, 0, 2, 0)],
        max_per_vod=9,
        max_silent_fraction=0.5,
        seed=1,
    )
    assert selection.clips.height == 0
    assert selection.n_skipped_silent > 0


def end_matches(residuals: list[float | None]) -> pl.DataFrame:
    rows: list[dict[str, Any]] = []
    for i, r in enumerate(residuals):
        rows.append(
            {
                "video_id": f"v{i % 4}",
                "login": "a" if i % 2 else "b",
                "clip_class": "live" if i % 3 else "post_stream",
                "stratum": "top" if i < len(residuals) / 2 else "low",
                "duration_s": 30.0,
                "created_s": 1000.0,
                "true_start_s": None if r is None else 970.0 + r,
                "shift_s": None if r is None else r - 30.0,
                "corr": None if r is None else 0.99,
                "margin": None if r is None else 0.5,
                "at_search_edge": None if r is None else False,
            }
        )
    return pl.DataFrame(
        rows,
        schema_overrides={
            "true_start_s": pl.Float64,
            "shift_s": pl.Float64,
            "corr": pl.Float64,
            "margin": pl.Float64,
            "at_search_edge": pl.Boolean,
        },
    )


def evaluate(residuals: list[float | None]) -> dict[str, Any]:
    return co.evaluate_end_rule(
        end_matches(residuals),
        tolerance_s=2.0,
        max_median_abs_error_s=2.0,
        min_share_within_tolerance=0.7,
        min_corr=0.6,
        min_margin=0.1,
        seed=1,
        n_resamples=40,
    )


def test_evaluate_end_rule_passes_and_fails_as_written() -> None:
    # 7 of 10 within 2 s (both edges count) and a small median: PASS.
    passing = evaluate([0.5, -1.0, 1.0, 2.0, -2.0, 0.0, 1.5, 9.0, -8.0, 30.0])
    assert passing["result"] == "PASS"
    assert passing["overall"]["share_within_tolerance"] == 0.7
    assert passing["n_weak"] == 0
    # 6 of 10 within 2 s: FAIL although the median is small.
    assert evaluate([0.5, -1.0, 1.0, 2.0, -2.0, 0.0, 2.5, 9.0, -8.0, 30.0])["result"] == "FAIL"
    # Every clip a clip length off (start-anchored): FAIL.
    assert evaluate([30.0] * 8)["result"] == "FAIL"
    # Missing matches don't count either way; no strong match at all is a FAIL.
    with_missing = evaluate([0.5, 0.2, None, -0.1])
    assert (with_missing["result"], with_missing["n_strong"], with_missing["n_weak"]) == (
        "PASS",
        3,
        1,
    )
    assert evaluate([None, None])["result"] == "FAIL"


def test_evaluate_end_rule_breaks_down_by_cell() -> None:
    breakdown = evaluate([0.5, -1.0, 1.0, 0.2, -0.3, 0.0, 1.5, 0.1])["breakdown"]
    assert set(breakdown["stratum"]) == {"top", "low"}
    assert set(breakdown["cell"]) == {
        "top_live",
        "top_post_stream",
        "low_live",
        "low_post_stream",
    }
    assert set(breakdown["channel"]) == {"a", "b"}
