"""Tests for audio features per window, on generated 16 kHz WAVs (docs/ml/features.md § Audio)."""

import json
import logging
import wave
from pathlib import Path

import numpy as np
import polars as pl
import pytest

from autoclipper.features import audio
from autoclipper.features.audio import (
    FEATURE_COLUMNS,
    SAMPLE_RATE_HZ,
    STD_FLOOR_RMS,
    AudioFormatError,
    compute_audio_features,
)
from autoclipper.labels.windows import make_windows, windows_for_vods
from autoclipper.synthetic import generate_dataset

RATE = SAMPLE_RATE_HZ


def write_wav(
    path: Path, samples: np.ndarray, *, rate: int = RATE, channels: int = 1, width: int = 2
) -> Path:
    """Write float samples in [-1, 1] as PCM."""
    data = np.clip(samples, -1.0, 1.0)
    pcm = (data * 32767).astype("<i2") if width == 2 else (data * 127 + 128).astype("u1")
    if channels == 2:
        pcm = np.repeat(pcm, 2)
    with wave.open(str(path), "wb") as out:
        out.setnchannels(channels)
        out.setsampwidth(width)
        out.setframerate(rate)
        out.writeframes(pcm.tobytes())
    return path


def quiet_noise(seconds: float, level: float = 0.01, seed: int = 0) -> np.ndarray:
    return np.random.default_rng(seed).normal(0.0, level, int(seconds * RATE))


def tone(seconds: float, hz: float, level: float = 0.5) -> np.ndarray:
    t = np.arange(int(seconds * RATE)) / RATE
    return level * np.sin(2 * np.pi * hz * t)


def test_output_has_one_float_row_per_window_and_a_feature_version(tmp_path: Path) -> None:
    wav = write_wav(tmp_path / "a.wav", quiet_noise(30.0))

    frame = compute_audio_features(make_windows("v", 30.0), wav)

    assert frame.columns == ["window_id", *FEATURE_COLUMNS, "feature_version"]
    assert frame["window_id"].to_list() == ["v:0", "v:1", "v:2"]
    assert all(frame.schema[c] == pl.Float64 for c in FEATURE_COLUMNS)
    assert all(frame[c].null_count() == 0 for c in FEATURE_COLUMNS)
    assert frame["feature_version"].unique().to_list() == ["audio-v1"]


def test_a_loud_burst_shows_up_in_its_window(tmp_path: Path) -> None:
    samples = quiet_noise(60.0)
    burst = slice(int(23.5 * RATE), int(25.0 * RATE))
    samples[burst] += np.random.default_rng(1).normal(0.0, 0.4, burst.stop - burst.start)
    wav = write_wav(tmp_path / "burst.wav", samples)

    frame = compute_audio_features(make_windows("v", 60.0), wav)

    rms_max = frame["audio_rms_max"].to_list()
    assert int(np.argmax(rms_max)) == 2  # 20-30 s
    assert rms_max[2] > 10 * max(rms_max[:2] + rms_max[3:])
    assert frame["audio_rms_max_z"][2] == max(frame["audio_rms_max_z"])
    assert frame["audio_rms_max_z"][2] > 2.0
    assert frame["audio_rms_rz"][2] > 2.0
    assert frame["audio_rms_std"][2] > frame["audio_rms_std"][0]
    onsets = frame["audio_onset_rate"].to_list()
    assert onsets[2] > 0
    assert frame["audio_rms_max_lead1"][1] == frame["audio_rms_max_z"][2]
    assert frame["audio_rms_max_lag2"][4] == frame["audio_rms_max_z"][2]


def test_digital_silence_is_silent_and_scores_zero(tmp_path: Path) -> None:
    wav = write_wav(tmp_path / "silence.wav", np.zeros(40 * RATE))

    frame = compute_audio_features(make_windows("v", 40.0), wav)

    assert frame["audio_silence_frac"].to_list() == [1.0] * 4
    for name in FEATURE_COLUMNS:
        if name != "audio_silence_frac":
            assert frame[name].to_list() == [0.0] * 4, name


def test_a_steady_vod_scores_zero_everywhere(tmp_path: Path) -> None:
    wav = write_wav(tmp_path / "steady.wav", tone(60.0, 440.0))

    frame = compute_audio_features(make_windows("v", 60.0), wav)

    for name in ("audio_rms_max_z", "audio_rms_rz", "audio_rms_max_lead1", "audio_rms_max_lag1"):
        assert frame[name].to_list() == pytest.approx([0.0] * 6, abs=1e-6), name


def test_a_tone_has_its_frequency_as_centroid_and_crossings(tmp_path: Path) -> None:
    wav = write_wav(tmp_path / "tone.wav", tone(10.0, 1000.0))

    frame = compute_audio_features(make_windows("v", 10.0), wav)

    assert frame["audio_spectral_centroid_mean"][0] == pytest.approx(1000.0, rel=0.05)
    assert frame["audio_zcr_mean"][0] == pytest.approx(2 * 1000 / RATE, rel=0.05)
    assert frame["audio_rms_mean"][0] == pytest.approx(0.5 / np.sqrt(2), rel=0.01)
    assert frame["audio_silence_frac"][0] == 0.0


def test_near_constant_audio_cant_make_the_rolling_z_explode(tmp_path: Path) -> None:
    # Five minutes of almost-silence (tiny but non-zero spread), then a normal sound.
    samples = np.concatenate([quiet_noise(300.0, level=1e-4), quiet_noise(10.0, level=0.05)])
    wav = write_wav(tmp_path / "jump.wav", samples)

    frame = compute_audio_features(make_windows("v", 310.0), wav)

    jump = frame["audio_rms_rz"][-1]
    rms = frame["audio_rms_max"].to_list()
    assert jump > 5.0
    assert jump <= (rms[-1] - min(rms)) / STD_FLOOR_RMS + 1e-6  # divided by the floor at most


def test_muted_windows_are_flagged(tmp_path: Path) -> None:
    wav = write_wav(tmp_path / "a.wav", quiet_noise(30.0))

    frame = compute_audio_features(make_windows("v", 30.0, [(12.0, 3.0)]), wav)

    assert frame["audio_is_muted"].to_list() == [0.0, 1.0, 0.0]


def test_windows_past_the_end_of_the_audio_are_zero_and_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    wav = write_wav(tmp_path / "short.wav", quiet_noise(15.0, level=0.1))

    with caplog.at_level(logging.WARNING, logger="autoclipper.features.audio"):
        frame = compute_audio_features(make_windows("v", 40.0), wav)

    assert frame["audio_rms_mean"][0] > 0
    assert frame["audio_rms_mean"].to_list()[2:] == [0.0, 0.0]
    assert "windows without audio" in caplog.text
    assert "n=2" in caplog.text


def test_block_size_doesnt_change_the_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    samples = quiet_noise(40.0)
    samples[int(17 * RATE) : int(18 * RATE)] += tone(1.0, 300.0, level=0.6)
    wav = write_wav(tmp_path / "a.wav", samples)
    windows = make_windows("v", 40.0)

    whole = compute_audio_features(windows, wav)
    monkeypatch.setattr(audio, "BLOCK_FRAMES", 97)  # many blocks, odd edges
    blocked = compute_audio_features(windows, wav)

    for name in FEATURE_COLUMNS:
        assert blocked[name].to_list() == pytest.approx(whole[name].to_list(), rel=1e-5), name


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"rate": 44_100}, "44100 Hz"),
        ({"channels": 2}, "2 channel"),
        ({"width": 1}, "8-bit"),
    ],
)
def test_audio_that_isnt_16_khz_mono_16_bit_is_rejected(
    tmp_path: Path, kwargs: dict[str, int], message: str
) -> None:
    wav = write_wav(tmp_path / "bad.wav", quiet_noise(10.0), **kwargs)

    with pytest.raises(AudioFormatError, match=message):
        compute_audio_features(make_windows("v", 10.0), wav)


def test_missing_or_truncated_wav_is_an_audio_format_error(tmp_path: Path) -> None:
    with pytest.raises(AudioFormatError, match="can't read"):
        compute_audio_features(make_windows("v", 10.0), tmp_path / "missing.wav")
    wav = write_wav(tmp_path / "cut.wav", quiet_noise(10.0))
    wav.write_bytes(wav.read_bytes()[: 44 + 2 * RATE])  # the header still says 10 s
    with pytest.raises(AudioFormatError, match="ended early"):
        compute_audio_features(make_windows("v", 10.0), wav)


def test_windows_of_several_vods_or_overlapping_windows_are_rejected(tmp_path: Path) -> None:
    wav = write_wav(tmp_path / "a.wav", quiet_noise(20.0))
    with pytest.raises(ValueError, match="one VOD"):
        compute_audio_features(pl.concat([make_windows("a", 20.0), make_windows("b", 20.0)]), wav)
    overlapping = make_windows("v", 20.0, window_s=10.0, stride_s=5.0)
    with pytest.raises(ValueError, match="overlap"):
        compute_audio_features(overlapping, wav)
    with pytest.raises(ValueError, match="missing columns: is_muted"):
        compute_audio_features(make_windows("v", 20.0).drop("is_muted"), wav)


def test_synthetic_dataset_bursts_and_muted_segment(tmp_path: Path) -> None:
    dataset = generate_dataset(tmp_path / "synthetic", seed=1)
    root = dataset.out_dir
    vods = pl.read_parquet(root / "processed" / "vods.parquet")
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))

    for vod in manifest["vods"]:
        windows = windows_for_vods(vods.filter(pl.col("video_id") == vod["video_id"]))
        wav = root / "interim" / "audio" / f"{vod['video_id']}.wav"
        frame = windows.join(compute_audio_features(windows, wav), on="window_id")
        for event in vod["planted_events"]:
            start, end = event["audio_burst_s"]
            burst = frame.filter((pl.col("start_s") < end) & (pl.col("end_s") > start))
            assert (burst["audio_rms_max_z"] > 1.0).any(), (vod["video_id"], event["case"])
        muted = frame.filter(pl.col("audio_is_muted") == 1.0)
        if vod["muted_s"]:
            assert muted.height > 0
            # The muted segment is digital silence; windows only partly inside it are mostly so.
            assert (muted["audio_silence_frac"] > 0.5).all()
        else:
            assert muted.height == 0
