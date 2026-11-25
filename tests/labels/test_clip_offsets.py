import dataclasses
import wave
from datetime import timedelta
from pathlib import Path

import numpy as np
import polars as pl
import pytest

from autoclipper.features.chat import load_lexicon
from autoclipper.labels import alignment_study as study
from autoclipper.labels import clip_offsets as co
from autoclipper.synthetic import generate_dataset

RATE = co.SAMPLE_RATE_HZ
LEXICON = Path(__file__).resolve().parents[2] / "configs" / "lexicon.yaml"


def write_wav(path: Path, samples: np.ndarray, *, channels: int = 1, rate: int = RATE) -> Path:
    pcm = (np.clip(samples, -1, 1) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes(pcm.tobytes())
    return path


def stream_audio(seconds: float, seed: int) -> np.ndarray:
    """Noise whose loudness changes every 50 ms, like speech and game sound."""
    rng = np.random.default_rng(seed)
    n = int(seconds * RATE)
    steps = rng.uniform(0.01, 0.5, size=n // 800 + 1)
    return rng.normal(size=n) * np.repeat(steps, 800)[:n]


@pytest.fixture(scope="module")
def vod(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, np.ndarray]:
    samples = stream_audio(400, seed=1)
    return write_wav(tmp_path_factory.mktemp("vod") / "vod.wav", samples), samples


def cut(samples: np.ndarray, start_s: float, length_s: float, *, seed: int = 9) -> np.ndarray:
    """A re-encoded-looking copy: same audio from `start_s`, other gain, a little noise."""
    part = samples[int(start_s * RATE) : int((start_s + length_s) * RATE)]
    return 0.7 * part + np.random.default_rng(seed).normal(scale=0.002, size=part.size)


def test_a_planted_clip_is_found_at_its_true_start(vod: tuple[Path, np.ndarray]) -> None:
    path, samples = vod
    match = co.match_clip(cut(samples, 178.4, 30), path, reported_s=200.0, search_s=120)
    assert match is not None
    assert match.true_start_s == pytest.approx(178.4, abs=0.011)
    assert match.shift_s == pytest.approx(-21.6, abs=0.011)
    assert match.corr > 0.95
    assert match.margin > 0.3
    assert not match.at_search_edge


def test_a_clip_after_its_reported_start_and_near_the_vod_start(
    vod: tuple[Path, np.ndarray],
) -> None:
    path, samples = vod
    late = co.match_clip(cut(samples, 250.0, 20), path, reported_s=240.0, search_s=60)
    assert late is not None
    assert late.shift_s == pytest.approx(10.0, abs=0.011)
    early = co.match_clip(cut(samples, 5.0, 30), path, reported_s=20.0, search_s=120)
    assert early is not None
    assert early.true_start_s == pytest.approx(5.0, abs=0.011)


def test_unrelated_audio_gives_a_weak_match(vod: tuple[Path, np.ndarray]) -> None:
    path, _ = vod
    match = co.match_clip(stream_audio(30, seed=7), path, reported_s=200.0, search_s=120)
    assert match is not None
    assert match.corr < 0.6 or match.margin < 0.1


def test_a_true_start_outside_the_search_span_is_at_the_edge_or_weak(
    vod: tuple[Path, np.ndarray],
) -> None:
    path, samples = vod
    match = co.match_clip(cut(samples, 100.0, 30), path, reported_s=200.0, search_s=40)
    assert match is not None
    assert match.at_search_edge or match.corr < 0.6


def test_normalized_xcorr_peaks_at_the_planted_position() -> None:
    rng = np.random.default_rng(0)
    segment = rng.normal(size=500)
    corr = co.normalized_xcorr(segment, segment[123:173] * 3 + 5)
    assert int(np.argmax(corr)) == 123
    assert corr[123] == pytest.approx(1.0)
    assert corr.size == 451
    assert np.array_equal(co.normalized_xcorr(np.ones(10), segment[:4]), np.zeros(7))
    assert np.array_equal(co.normalized_xcorr(segment, np.ones(4)), np.zeros(497))
    assert co.normalized_xcorr(segment[:3], segment[:5]).size == 0


def test_envelope_frames_and_floor() -> None:
    samples = np.concatenate([np.zeros(RATE // 10), np.full(RATE // 10, 0.5)])
    env = co.envelope(np.concatenate([samples, np.zeros(50)]))
    assert env.size == 20
    assert env[:10].tolist() == [co.FLOOR_DB] * 10
    assert env[10:] == pytest.approx(20 * np.log10(0.5), abs=1e-6)


def test_read_wav_clamps_and_checks_the_format(
    vod: tuple[Path, np.ndarray], tmp_path: Path
) -> None:
    path, samples = vod
    part, start = co.read_wav(path, -5.0, 1.0)
    assert start == 0.0
    assert part.size == RATE
    tail, start = co.read_wav(path, 399.0, 500.0)
    assert (start, tail.size) == (399.0, RATE)
    assert np.allclose(tail, np.clip(samples[-RATE:], -1, 1), atol=1e-4)
    stereo = write_wav(tmp_path / "stereo.wav", np.zeros(200), channels=2)
    with pytest.raises(co.ClipAudioError, match="mono"):
        co.read_wav(stereo)


def test_silent_fraction(tmp_path: Path) -> None:
    path = write_wav(tmp_path / "half.wav", np.concatenate([np.zeros(RATE), stream_audio(1, 3)]))
    assert co.silent_fraction(path, 0.0, 1.0) == 1.0
    assert co.silent_fraction(path, 1.0, 2.0) < 0.05
    assert co.silent_fraction(path, 5.0, 6.0) == 1.0  # past the end: no audio


@pytest.fixture(scope="module")
def pilot(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, list[study.VodData]]:
    root = tmp_path_factory.mktemp("offsets") / "pilot"
    generate_dataset(root, seed=1, pilot=True)
    ids = pl.read_parquet(root / "processed" / "vods.parquet")["video_id"].to_list()
    vods = study.load_collection(
        root, ids, lexicon=load_lexicon(LEXICON), cache_dir=root.parent / "cache"
    )
    return root, vods


def wavs(root: Path, vods: list[study.VodData]) -> dict[str, Path]:
    return {v.video_id: root / "interim" / "audio" / f"{v.video_id}.wav" for v in vods}


def test_select_clips_takes_top_clips_per_channel_within_limits(
    pilot: tuple[Path, list[study.VodData]],
) -> None:
    root, vods = pilot
    late = vods[0].clips["created_at"].max() + timedelta(days=30)
    first = vods[0]
    post = first.clips.with_columns(
        created_at=pl.when(pl.col("clip_id") == first.clips["clip_id"][0])
        .then(pl.lit(late))
        .otherwise(pl.col("created_at"))
    )
    vods = [dataclasses.replace(first, clips=post), *vods[1:]]
    selection = co.select_clips(
        vods, wav_paths=wavs(root, vods), muted={}, per_channel=3, live_per_channel=2, max_per_vod=2
    )
    clips = selection.clips
    assert clips.group_by("video_id").len()["len"].max() <= 2
    assert clips.group_by("login").len()["len"].max() <= 3
    assert "post_stream" in clips["clip_class"].to_list()
    for login, group in clips.group_by("login"):
        views = group["view_count"].to_list()
        assert views == sorted(views, reverse=True) or group["clip_class"].n_unique() > 1, login


def test_select_clips_skips_muted_and_silent_clips(
    pilot: tuple[Path, list[study.VodData]], tmp_path: Path
) -> None:
    root, vods = pilot
    vod = vods[0]
    every = co.select_clips(
        [vod], wav_paths=wavs(root, [vod]), muted={}, per_channel=9, live_per_channel=9,
        max_per_vod=9,
    )  # fmt: skip
    muted = {vod.video_id: [(0.0, vod.duration_s)]}
    none = co.select_clips(
        [vod], wav_paths=wavs(root, [vod]), muted=muted, per_channel=9, live_per_channel=9,
        max_per_vod=9,
    )  # fmt: skip
    assert none.clips.height == 0
    assert none.n_skipped_muted >= every.clips.height > 0
    silent = {
        vod.video_id: write_wav(tmp_path / "silent.wav", np.zeros(int(vod.duration_s * RATE)))
    }
    quiet = co.select_clips(
        [vod], wav_paths=silent, muted={}, per_channel=9, live_per_channel=9, max_per_vod=9
    )
    assert quiet.clips.height == 0
    assert quiet.n_skipped_silent >= every.clips.height


def test_shift_vod_moves_clips_by_class_and_rebuilds_moments(
    pilot: tuple[Path, list[study.VodData]],
) -> None:
    _, vods = pilot
    vod = vods[0]
    moved = co.shift_vod(vod, {"live": -20.0, "post_stream": 5.0})
    before = dict(vod.clips.select("clip_id", "vod_offset_s").iter_rows())
    after = dict(moved.clips.select("clip_id", "vod_offset_s").iter_rows())
    for clip_id, offset in after.items():
        assert offset == pytest.approx(max(before[clip_id] - 20.0, 0.0))
    assert moved.moments["start_s"].min() == pytest.approx(
        max(vod.moments["start_s"].min() - 20.0, 0.0)
    )
    gone = co.shift_vod(vod, {"live": -10_000.0})
    assert gone.clips.height == 0
    assert gone.moments.height == 0


def matches_frame(
    rows: list[tuple[str, str, float | None, float | None, float | None]],
) -> pl.DataFrame:
    return pl.DataFrame(
        [
            {
                "video_id": vid,
                "login": vid[:1],
                "clip_class": cls,
                "duration_s": 30.0,
                "created_s": 1000.0,
                "true_start_s": None if shift is None else 900.0 + shift,
                "shift_s": shift,
                "corr": corr,
                "margin": margin,
                "at_search_edge": None if corr is None else False,
            }
            for vid, cls, shift, corr, margin in rows
        ],
        schema_overrides={"true_start_s": pl.Float64, "shift_s": pl.Float64, "corr": pl.Float64,
                          "margin": pl.Float64, "at_search_edge": pl.Boolean},
    )  # fmt: skip


def test_summarize_matches_uses_strong_matches_only() -> None:
    frame = matches_frame(
        [
            ("a1", "live", -22.0, 0.99, 0.6),
            ("a1", "live", -21.0, 0.97, 0.5),
            ("a2", "live", -23.0, 0.95, 0.4),
            ("b1", "post_stream", -1.0, 0.9, 0.5),
            ("b1", "post_stream", 90.0, 0.4, 0.05),  # weak
            ("b2", "live", None, None, None),  # couldn't be fetched
        ]
    )
    summary = co.summarize_matches(
        frame, min_corr=0.6, min_margin=0.1, consistent_iqr_s=10, seed=1, n_resamples=50
    )
    assert (summary["n_checked"], summary["n_strong"], summary["n_weak"]) == (6, 4, 2)
    assert summary["weak_by_reason"]["no_match"] == 1
    assert summary["weak_by_reason"]["low_corr"] == 1
    live = summary["shift"]["live"]
    assert live["median_s"] == -22.0
    assert live["n_vods"] == 2
    assert live["consistent"]
    assert summary["shift"]["post_stream"]["median_s"] == -1.0
    assert summary["shift_by_channel"]["a"]["n_clips"] == 3
    assert summary["live_true_end_minus_created_s"]["p50"] == pytest.approx(-92.0)
