"""Tests for pretrained audio-event features per window (docs/ml/features.md § Phase 2, #63)."""

import hashlib
import importlib
import logging
import wave
from pathlib import Path

import httpx
import numpy as np
import numpy.typing as npt
import polars as pl
import pytest
from typer.testing import CliRunner

from autoclipper.cli import ExitCode, app
from autoclipper.features import audio_events
from autoclipper.features.audio import SAMPLE_RATE_HZ, AudioFormatError
from autoclipper.features.audio_events import (
    AUDIO_EVENTS_FEATURE_VERSION,
    EVENT_CLASS_INDICES,
    FEATURE_COLUMNS,
    YAMNET_LABEL_MAP,
    YAMNET_MODEL_FILENAME,
    YAMNET_NUM_CLASSES,
    AudioEventsError,
    YamnetEventDetector,
    aggregate_event_probabilities,
    audio_events_available,
    compute_audio_event_features,
    ensure_yamnet_model,
    select_top_windows,
)
from autoclipper.labels.windows import make_windows

RATE = SAMPLE_RATE_HZ
runner = CliRunner()


def write_wav(
    path: Path,
    samples: npt.NDArray[np.floating],
    *,
    rate: int = RATE,
    channels: int = 1,
    width: int = 2,
) -> Path:
    """Write float samples in [-1, 1] as PCM WAV."""
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


class FakeDetector:
    """Deterministic fake detector that encodes a signal signature into AudioSet-521 classes."""

    def __init__(self, scores_by_call: list[npt.NDArray[np.float32]] | None = None) -> None:
        self.calls: list[npt.NDArray[np.float32]] = []
        self._scores_by_call = list(scores_by_call) if scores_by_call is not None else None

    def score_frames(self, waveform: npt.NDArray[np.float32], /) -> npt.NDArray[np.float32]:
        self.calls.append(waveform.copy())
        if self._scores_by_call is not None:
            return self._scores_by_call.pop(0)
        # Map RMS level into distinct AudioSet classes so tests can trace each window's slice.
        level = float(np.sqrt(np.mean(waveform * waveform)))
        frames = np.zeros((3, YAMNET_NUM_CLASSES), dtype=np.float32)
        frames[0, 13] = min(1.0, level)  # Laughter
        frames[1, 11] = min(1.0, level * 0.5)  # Screaming
        frames[2, 61] = min(1.0, level * 0.25)  # Cheering
        frames[0, 132] = min(1.0, level * 0.75)  # Music
        return frames


# --- Label mapping and aggregation -----------------------------------------------------------


def test_label_map_covers_all_four_columns_with_valid_audioset_indices() -> None:
    assert FEATURE_COLUMNS == (
        "audio_evt_laughter",
        "audio_evt_scream",
        "audio_evt_cheer",
        "audio_evt_music",
    )
    assert set(YAMNET_LABEL_MAP) == set(FEATURE_COLUMNS)
    seen: set[int] = set()
    for column, entries in YAMNET_LABEL_MAP.items():
        assert len(entries) >= 1, column
        for entry in entries:
            assert 0 <= entry.index < YAMNET_NUM_CLASSES
            assert entry.mid.startswith(("/m/", "/t/"))
            assert entry.display_name
            assert entry.index not in seen, f"duplicate class index {entry.index}"
            seen.add(entry.index)
    assert EVENT_CLASS_INDICES["audio_evt_laughter"] == (13, 14, 15, 16, 17, 18)
    assert EVENT_CLASS_INDICES["audio_evt_scream"] == (6, 7, 9, 11)
    assert EVENT_CLASS_INDICES["audio_evt_cheer"] == (8, 58, 61, 62)
    assert EVENT_CLASS_INDICES["audio_evt_music"] == (132,)


def test_aggregate_event_probabilities_takes_max_over_frames_and_subclasses() -> None:
    frames = np.zeros((4, YAMNET_NUM_CLASSES), dtype=np.float32)
    frames[0, 0] = 0.99  # Speech (unmapped) must not leak into any feature column
    frames[1, 15] = 0.42  # Giggle
    frames[3, 17] = 0.87  # Belly laugh -> max laughter
    frames[2, 9] = 0.61  # Yell -> max scream
    frames[0, 58] = 0.35  # Clapping
    frames[2, 62] = 0.73  # Applause -> max cheer
    frames[1, 132] = 1.05  # Music (slightly above 1.0 -> clipped)

    probs = aggregate_event_probabilities(frames)

    assert probs["audio_evt_laughter"] == pytest.approx(0.87, abs=1e-6)
    assert probs["audio_evt_scream"] == pytest.approx(0.61, abs=1e-6)
    assert probs["audio_evt_cheer"] == pytest.approx(0.73, abs=1e-6)
    assert probs["audio_evt_music"] == 1.0


def test_aggregate_event_probabilities_handles_empty_frames_and_rejects_bad_inputs() -> None:
    empty = np.zeros((0, YAMNET_NUM_CLASSES), dtype=np.float32)
    assert aggregate_event_probabilities(empty) == dict.fromkeys(FEATURE_COLUMNS, 0.0)

    with pytest.raises(ValueError, match="2D"):
        aggregate_event_probabilities(np.zeros((YAMNET_NUM_CLASSES,), dtype=np.float32))
    with pytest.raises(ValueError, match="out of range"):
        aggregate_event_probabilities(np.zeros((2, 10), dtype=np.float32))
    with pytest.raises(ValueError, match="no class indices"):
        aggregate_event_probabilities(
            np.zeros((2, YAMNET_NUM_CLASSES), dtype=np.float32),
            label_indices={"audio_evt_music": ()},
        )
    bad = np.zeros((2, YAMNET_NUM_CLASSES), dtype=np.float32)
    bad[0, 13] = np.nan
    with pytest.raises(ValueError, match="non-finite"):
        aggregate_event_probabilities(bad)


# --- Subset selection ------------------------------------------------------------------------


def test_select_top_windows_ranks_deterministically_from_array_and_dataframe() -> None:
    windows = make_windows("v1", 50.0)  # v1:0 .. v1:4

    # Ties at score 2.0 break by earlier start_s (v1:1 before v1:3).
    top_from_array = select_top_windows(windows, [0.5, 2.0, 5.0, 2.0, -1.0], top_k=2)
    assert top_from_array == ["v1:2", "v1:1"]

    scores_df = pl.DataFrame(
        {
            "window_id": ["v1:4", "v1:0", "v1:3", "v1:1", "v1:2"],
            "score": [-1.0, 0.5, 2.0, 2.0, 5.0],
        }
    )
    assert select_top_windows(windows, scores_df, top_k=3) == ["v1:2", "v1:1", "v1:3"]


def test_select_top_windows_validates_inputs() -> None:
    windows = make_windows("v1", 30.0)

    with pytest.raises(ValueError, match="top_k"):
        select_top_windows(windows, [1.0, 2.0, 3.0], top_k=0)
    with pytest.raises(ValueError, match="top_k"):
        select_top_windows(windows, [1.0, 2.0, 3.0], top_k=True)
    with pytest.raises(ValueError, match="one value per window"):
        select_top_windows(windows, [1.0, 2.0], top_k=2)
    with pytest.raises(ValueError, match="finite"):
        select_top_windows(windows, [1.0, float("nan"), 3.0], top_k=2)
    with pytest.raises(ValueError, match="unknown window_id"):
        select_top_windows(
            windows,
            pl.DataFrame({"window_id": ["v1:0", "v1:999"], "score": [1.0, 2.0]}),
            top_k=2,
        )


# --- Per-window extraction -------------------------------------------------------------------


def test_compute_audio_event_features_full_vod_with_fake_detector(tmp_path: Path) -> None:
    # Three 10 s windows with distinct constant amplitudes (0.2, 0.6, 0.4).
    samples = np.concatenate(
        [
            np.full(10 * RATE, 0.2, dtype=np.float32),
            np.full(10 * RATE, 0.6, dtype=np.float32),
            np.full(10 * RATE, 0.4, dtype=np.float32),
        ]
    )
    wav = write_wav(tmp_path / "v.wav", samples)
    detector = FakeDetector()

    frame = compute_audio_event_features(make_windows("v", 30.0), wav, detector=detector)

    assert frame.columns == ["window_id", *FEATURE_COLUMNS, "feature_version"]
    assert frame["window_id"].to_list() == ["v:0", "v:1", "v:2"]
    assert all(frame.schema[c] == pl.Float64 for c in FEATURE_COLUMNS)
    assert all(frame[c].null_count() == 0 for c in FEATURE_COLUMNS)
    assert frame["feature_version"].unique().to_list() == [AUDIO_EVENTS_FEATURE_VERSION]
    assert len(detector.calls) == 3
    assert all(len(call) == 10 * RATE for call in detector.calls)
    assert frame["audio_evt_laughter"].to_list() == pytest.approx([0.2, 0.6, 0.4], abs=1e-3)
    assert frame["audio_evt_music"].to_list() == pytest.approx([0.15, 0.45, 0.3], abs=1e-3)


def test_subset_by_scores_runs_detector_only_on_top_k_windows(tmp_path: Path) -> None:
    samples = np.full(50 * RATE, 0.5, dtype=np.float32)
    wav = write_wav(tmp_path / "v.wav", samples)
    # Pass windows intentionally reversed to confirm positional scores stay aligned.
    windows = make_windows("v", 50.0).reverse()
    # In reversed order: v:4, v:3, v:2, v:1, v:0 -> highest scores at v:3 (9.0) and v:0 (7.0).
    positional_scores = [1.0, 9.0, 2.0, 3.0, 7.0]
    detector = FakeDetector()

    frame = compute_audio_event_features(
        windows, wav, detector=detector, scores=positional_scores, top_k=2
    )

    assert len(detector.calls) == 2
    assert frame["window_id"].to_list() == ["v:0", "v:1", "v:2", "v:3", "v:4"]
    laughter = frame["audio_evt_laughter"].to_list()
    assert laughter[0] == pytest.approx(0.5, abs=1e-3)
    assert laughter[1] is None
    assert laughter[2] is None
    assert laughter[3] == pytest.approx(0.5, abs=1e-3)
    assert laughter[4] is None


def test_subset_by_window_ids_and_validation(tmp_path: Path) -> None:
    wav = write_wav(tmp_path / "v.wav", np.full(30 * RATE, 0.4, dtype=np.float32))
    windows = make_windows("v", 30.0)
    detector = FakeDetector()

    frame = compute_audio_event_features(windows, wav, detector=detector, window_ids=["v:1"])

    assert len(detector.calls) == 1
    assert frame["audio_evt_laughter"][0] is None
    assert frame["audio_evt_laughter"][1] == pytest.approx(0.4, abs=1e-3)
    assert frame["audio_evt_laughter"][2] is None

    with pytest.raises(ValueError, match="not both"):
        compute_audio_event_features(
            windows, wav, detector=detector, window_ids=["v:0"], scores=[1.0, 2.0, 3.0]
        )
    with pytest.raises(ValueError, match="requires scores"):
        compute_audio_event_features(windows, wav, detector=detector, top_k=2)
    with pytest.raises(ValueError, match="unknown window_id"):
        compute_audio_event_features(windows, wav, detector=detector, window_ids=["v:99"])


def test_empty_windows_and_short_wav_behaviour(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    wav = write_wav(tmp_path / "short.wav", np.full(10 * RATE, 0.3, dtype=np.float32))
    detector = FakeDetector()

    empty = compute_audio_event_features(make_windows("v", 2.0), wav, detector=detector)
    assert empty.height == 0
    assert empty.columns == ["window_id", *FEATURE_COLUMNS, "feature_version"]
    assert detector.calls == []

    # 30 s of windows against a 10 s WAV: windows 1 and 2 have no audio -> 0.0 + warning.
    with caplog.at_level(logging.WARNING, logger="autoclipper.features.audio_events"):
        frame = compute_audio_event_features(make_windows("v", 30.0), wav, detector=detector)

    assert len(detector.calls) == 1
    assert frame["audio_evt_laughter"][0] == pytest.approx(0.3, abs=1e-3)
    assert frame["audio_evt_laughter"][1] == 0.0
    assert frame["audio_evt_laughter"][2] == 0.0
    assert any("without audio" in rec.message for rec in caplog.records)


def test_invalid_wav_format_raises_audio_format_error(tmp_path: Path) -> None:
    stereo = write_wav(tmp_path / "stereo.wav", np.zeros(10 * RATE), channels=2)
    with pytest.raises(AudioFormatError, match="16000 Hz mono 16-bit"):
        compute_audio_event_features(make_windows("v", 10.0), stereo, detector=FakeDetector())

    with pytest.raises(AudioFormatError, match="can't read WAV"):
        compute_audio_event_features(
            make_windows("v", 10.0), tmp_path / "missing.wav", detector=FakeDetector()
        )


# --- Model cache, checksums, and optional dependency -----------------------------------------


def test_ensure_yamnet_model_downloads_verifies_and_caches(tmp_path: Path) -> None:
    payload = b"fake-onnx-model-bytes"
    digest = hashlib.sha256(payload).hexdigest()
    hits = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal hits
        hits += 1
        return httpx.Response(200, content=payload)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        first = ensure_yamnet_model(
            tmp_path / "cache",
            url="https://example.test/yamnet.onnx",
            expected_sha256=digest,
            client=client,
        )
        second = ensure_yamnet_model(
            tmp_path / "cache",
            url="https://example.test/yamnet.onnx",
            expected_sha256=digest,
            client=client,
        )

    assert first == second == tmp_path / "cache" / YAMNET_MODEL_FILENAME
    assert first.read_bytes() == payload
    assert hits == 1


def test_ensure_yamnet_model_rejects_bad_checksum_and_recovers_corrupt_cache(
    tmp_path: Path,
) -> None:
    good = b"valid-yamnet-bytes"
    good_sha = hashlib.sha256(good).hexdigest()
    cache = tmp_path / "cache"

    with (
        httpx.Client(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"tampered"))
        ) as bad_client,
        pytest.raises(AudioEventsError, match="SHA-256"),
    ):
        ensure_yamnet_model(
            cache,
            url="https://example.test/yamnet.onnx",
            expected_sha256=good_sha,
            client=bad_client,
        )
    assert not (cache / YAMNET_MODEL_FILENAME).exists()
    assert list(cache.glob("*.tmp")) == []

    # Pre-seed a corrupted cache file and confirm it is replaced on the next call.
    (cache / YAMNET_MODEL_FILENAME).write_bytes(b"corrupt")
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=good))
    ) as good_client:
        repaired = ensure_yamnet_model(
            cache,
            url="https://example.test/yamnet.onnx",
            expected_sha256=good_sha,
            client=good_client,
        )
    assert repaired.read_bytes() == good


def test_missing_onnxruntime_raises_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    real_import = importlib.import_module

    def fake_import(name: str, package: str | None = None) -> object:
        if name == "onnxruntime":
            raise ImportError("No module named 'onnxruntime'")
        return real_import(name, package)

    monkeypatch.setattr(importlib, "import_module", fake_import)
    detector = YamnetEventDetector()
    with pytest.raises(AudioEventsError, match="uv sync --extra audio-events"):
        detector.score_frames(np.zeros(RATE, dtype=np.float32))


# --- CLI -------------------------------------------------------------------------------------


def test_cli_audio_events_writes_parquet_with_subset_scoring(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wav = write_wav(tmp_path / "v.wav", np.full(30 * RATE, 0.5, dtype=np.float32))
    windows_path = tmp_path / "windows.parquet"
    scores_path = tmp_path / "scores.parquet"
    out_path = tmp_path / "events.parquet"
    make_windows("v", 30.0).write_parquet(windows_path)
    pl.DataFrame({"window_id": ["v:0", "v:1", "v:2"], "score": [0.1, 5.0, 0.2]}).write_parquet(
        scores_path
    )

    fake = FakeDetector()
    monkeypatch.setattr(audio_events, "YamnetEventDetector", lambda **_: fake)

    res = runner.invoke(
        app,
        [
            "audio-events",
            str(wav),
            "--windows",
            str(windows_path),
            "--scores",
            str(scores_path),
            "--top-k",
            "1",
            "--out",
            str(out_path),
        ],
    )
    assert res.exit_code == ExitCode.SUCCESS, res.output
    assert "1 of 3 windows scored" in res.output
    saved = pl.read_parquet(out_path)
    assert saved["audio_evt_laughter"][0] is None
    assert saved["audio_evt_laughter"][1] == pytest.approx(0.5, abs=1e-3)
    assert saved["audio_evt_laughter"][2] is None

    # Refuses to overwrite without --force.
    second = runner.invoke(
        app,
        [
            "audio-events",
            str(wav),
            "--windows",
            str(windows_path),
            "--out",
            str(out_path),
        ],
    )
    assert second.exit_code == ExitCode.USER_ERROR
    assert "--force" in second.output

    # Rejects --top-k without --scores.
    bad_flags = runner.invoke(
        app,
        [
            "audio-events",
            str(wav),
            "--windows",
            str(windows_path),
            "--top-k",
            "5",
        ],
    )
    assert bad_flags.exit_code == ExitCode.USER_ERROR


# --- Opt-in real-model test (skipped in CI) --------------------------------------------------


@pytest.mark.live
@pytest.mark.skipif(
    not audio_events_available(),
    reason="needs `uv sync --extra audio-events`",
)
def test_real_yamnet_model_scores_synthetic_clip(tmp_path: Path) -> None:
    t = np.arange(10 * RATE, dtype=np.float32) / RATE
    # Synthesize a bright C-major chord (music-like harmonic tone) for one 10 s window.
    chord = 0.2 * (
        np.sin(2 * np.pi * 261.63 * t)
        + np.sin(2 * np.pi * 329.63 * t)
        + np.sin(2 * np.pi * 392.00 * t)
    )
    wav = write_wav(tmp_path / "chord.wav", chord)

    frame = compute_audio_event_features(
        make_windows("synth_live", 10.0),
        wav,
        cache_dir=tmp_path / "cache",
    )

    assert frame.height == 1
    assert frame["feature_version"][0] == AUDIO_EVENTS_FEATURE_VERSION
    for col in FEATURE_COLUMNS:
        val = frame[col][0]
        assert isinstance(val, float)
        assert 0.0 <= val <= 1.0
