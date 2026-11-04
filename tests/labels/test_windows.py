"""Tests for cutting VODs into fixed windows (docs/data/labeling.md § 3)."""

from pathlib import Path

import polars as pl
import pytest

from autoclipper.labels.windows import WINDOWS_SCHEMA, make_windows, windows_for_vods
from autoclipper.synthetic import generate_dataset


def bounds(frame: pl.DataFrame) -> list[tuple[float, float]]:
    return list(zip(frame["start_s"].to_list(), frame["end_s"].to_list(), strict=True))


def test_windows_cover_the_vod_in_10_second_steps_with_stable_ids() -> None:
    windows = make_windows("9100000001", 30.0)

    assert windows.schema == WINDOWS_SCHEMA
    assert bounds(windows) == [(0.0, 10.0), (10.0, 20.0), (20.0, 30.0)]
    assert windows["window_id"].to_list() == ["9100000001:0", "9100000001:1", "9100000001:2"]
    assert windows["index"].to_list() == [0, 1, 2]
    assert windows["video_id"].unique().to_list() == ["9100000001"]


@pytest.mark.parametrize(
    ("duration_s", "last"),
    [
        (34.9, (20.0, 30.0)),  # 4.9 s left: dropped
        (35.0, (30.0, 35.0)),  # exactly half a window: kept
        (37.5, (30.0, 37.5)),  # kept, ending with the VOD
    ],
)
def test_final_partial_window_is_kept_only_if_at_least_half_a_window(
    duration_s: float, last: tuple[float, float]
) -> None:
    windows = make_windows("v", duration_s)

    assert bounds(windows)[-1] == last


def test_vod_shorter_than_half_a_window_has_no_windows() -> None:
    windows = make_windows("v", 4.0)

    assert windows.height == 0
    assert windows.schema == WINDOWS_SCHEMA


def test_many_windows_have_exact_starts() -> None:
    # 10 h: starts are computed from integers, so no float drift accumulates.
    windows = make_windows("v", 36_000.0)

    assert windows.height == 3600
    assert windows["start_s"][3599] == 35_990.0
    assert windows["end_s"][3599] == 36_000.0


@pytest.mark.parametrize(
    ("segment", "muted"),
    [
        ((10.0, 10.0), [False, True, False, False]),  # exactly one window
        ((15.0, 10.0), [False, True, True, False]),  # straddles two
        ((20.0, 0.5), [False, False, True, False]),  # inside one
        ((0.0, 10.0), [True, False, False, False]),  # touches window 1's start: no overlap
        ((39.0, 100.0), [False, False, False, True]),  # runs past the end
    ],
)
def test_windows_overlapping_a_muted_segment_are_flagged_not_dropped(
    segment: tuple[float, float], muted: list[bool]
) -> None:
    windows = make_windows("v", 40.0, [segment])

    assert windows.height == 4
    assert windows["is_muted"].to_list() == muted


def test_overlapping_windows_with_a_smaller_stride() -> None:
    windows = make_windows("v", 20.0, window_s=10.0, stride_s=5.0)

    # 15-20 s is a half window and kept; nothing starts at 20 s.
    assert bounds(windows) == [(0.0, 10.0), (5.0, 15.0), (10.0, 20.0), (15.0, 20.0)]


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"video_id": " ", "duration_s": 10.0}, "blank"),
        ({"video_id": "v", "duration_s": -1.0}, "duration_s"),
        ({"video_id": "v", "duration_s": float("nan")}, "duration_s"),
        ({"video_id": "v", "duration_s": 10.0, "window_s": 0.0}, "window_s"),
        ({"video_id": "v", "duration_s": 10.0, "stride_s": -5.0}, "stride_s"),
        ({"video_id": "v", "duration_s": 10.0, "muted_segments": [(-1.0, 5.0)]}, "muted"),
    ],
)
def test_invalid_input_is_rejected(kwargs: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        make_windows(**kwargs)  # type: ignore[arg-type]


def test_same_input_gives_the_same_windows() -> None:
    first = make_windows("v", 123.4, [(30.0, 12.0)])
    second = make_windows("v", 123.4, [(30.0, 12.0)])

    assert first.equals(second)


# --- vods tables -----------------------------------------------------------------------------


def test_windows_for_vods_reads_the_vods_table() -> None:
    vods = pl.DataFrame(
        {
            "video_id": ["b", "a"],
            "duration_s": [20.0, 10.0],
            "muted_segments": [[{"offset_s": 0.0, "duration_s": 5.0}], None],
        }
    )

    windows = windows_for_vods(vods)

    assert windows["window_id"].to_list() == ["a:0", "b:0", "b:1"]
    assert windows["is_muted"].to_list() == [False, True, False]


def test_windows_for_vods_rejects_missing_columns_and_repeated_vods() -> None:
    with pytest.raises(ValueError, match="missing columns: muted_segments"):
        windows_for_vods(pl.DataFrame({"video_id": ["a"], "duration_s": [10.0]}))
    repeated = pl.DataFrame(
        {"video_id": ["a", "a"], "duration_s": [10.0, 10.0], "muted_segments": [[], []]}
    )
    with pytest.raises(ValueError, match="more than once"):
        windows_for_vods(repeated)


def test_empty_vods_table_gives_no_windows() -> None:
    vods = pl.DataFrame(
        schema={
            "video_id": pl.String,
            "duration_s": pl.Float64,
            "muted_segments": pl.List(
                pl.Struct({"offset_s": pl.Float64, "duration_s": pl.Float64})
            ),
        }
    )

    assert windows_for_vods(vods).schema == WINDOWS_SCHEMA


def test_synthetic_dataset_windows_cover_each_vod_and_flag_its_muted_segment(
    tmp_path: Path,
) -> None:
    dataset = generate_dataset(tmp_path / "synthetic", seed=1)
    vods = pl.read_parquet(dataset.out_dir / "processed" / "vods.parquet")

    windows = windows_for_vods(vods)

    for vod in vods.iter_rows(named=True):
        own = windows.filter(pl.col("video_id") == vod["video_id"])
        assert own["start_s"][0] == 0.0
        assert vod["duration_s"] - own["end_s"][-1] < 5.0  # nothing longer than half a window left
        assert own["end_s"][-1] <= vod["duration_s"]
        for segment in vod["muted_segments"]:
            start, end = segment["offset_s"], segment["offset_s"] + segment["duration_s"]
            overlapping = own.filter((pl.col("start_s") < end) & (pl.col("end_s") > start))
            assert overlapping.height > 0
            assert overlapping["is_muted"].all()
            assert (
                own.filter(~pl.col("window_id").is_in(overlapping["window_id"].implode()))[
                    "is_muted"
                ]
                .not_()
                .all()
            )
    assert windows.filter(pl.col("is_muted")).height > 0  # the scenario has a muted VOD
