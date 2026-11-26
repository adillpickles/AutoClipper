"""Tests for deterministic candidate explanations and titles (autoclipper.models.explain)."""

import math
import wave
from pathlib import Path

import numpy as np
import polars as pl
import pytest

from autoclipper.analysis import Candidate
from autoclipper.models.explain import explain_candidates


def _candidate(
    rank: int,
    *,
    score: float = 2.0,
    start_s: float = 100.0,
    end_s: float = 130.0,
    source: str = "discovered",
    n_viewer_clips: int = 0,
    top_signals: tuple[str, ...] = (),
) -> Candidate:
    return Candidate(
        candidate_id=f"c{rank:03d}",
        rank=rank,
        score=score,
        start_s=start_s,
        end_s=end_s,
        source=source,  # type: ignore[arg-type]
        n_viewer_clips=n_viewer_clips,
        top_signals=top_signals,
    )


def _windows(n: int, step_s: float = 10.0) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "window_id": [f"w{i:04d}" for i in range(n)],
            "start_s": [i * step_s for i in range(n)],
            "end_s": [(i + 1) * step_s for i in range(n)],
        }
    )


def _write_wav(path: Path, samples: np.ndarray, sample_rate: int = 16_000) -> Path:
    pcm = np.clip(samples * 32767.0, -32768, 32767).astype("<i2")
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(pcm.tobytes())
    return path


def test_chat_spike_laugh_loudness_lookback_and_viewer_clips() -> None:
    # 200 windows = 2000 s (>33 min).
    # Window 10 (100-110 s) is the loudest in the whole VOD (0.95).
    # Window 140 (1400-1410 s) is 21.5 min later, with RMS 0.70 — loudest in the last 21 minutes!
    # Chat baseline is ~1.0 msg/s; window 140 spikes to 6.0 msg/s with lots of laugh reactions.
    n = 200
    win = _windows(n)
    msg_rate = np.full(n, 1.0)
    msg_rate[140] = 6.0
    msg_z = (msg_rate - msg_rate.mean()) / msg_rate.std()

    laugh_rate = np.full(n, 0.05)
    laugh_rate[140] = 3.0

    rms_max = np.full(n, 0.15)
    rms_max[10] = 0.95
    rms_max[140] = 0.70
    rms_z = (rms_max - rms_max.mean()) / rms_max.std()

    features = pl.DataFrame(
        {
            "window_id": win["window_id"],
            "chat_msg_rate": msg_rate,
            "chat_msg_rate_z": msg_z,
            "chat_laugh_rate": laugh_rate,
            "audio_rms_max": rms_max,
            "audio_rms_max_z": rms_z,
            "audio_is_muted": np.zeros(n),
        }
    )
    cand = _candidate(
        1,
        score=5.2,
        start_s=1380.0,
        end_s=1420.0,
        source="both",
        n_viewer_clips=3,
        top_signals=("chat_msg_rate_z", "audio_rms_max_z"),
    )

    (explained,) = explain_candidates([cand], windows=win, features=features, duration_s=2000.0)

    assert explained.title == "Loud laughs & chat burst"
    assert explained.reason == (
        "Chat jumped to 6\u00d7 its usual rate with lots of LUL; "
        "loudest moment in the last 21 minutes; 3 viewers clipped it"
    )
    kinds = [d.kind for d in explained.signal_details]
    assert "chat_rate" in kinds
    assert "chat_laugh" in kinds
    assert "audio_loudness" in kinds
    assert "viewer_clips" in kinds


def test_loudest_moment_of_stream_and_muted_guard() -> None:
    n = 30  # 300 s (5 minutes)
    win = _windows(n)
    rms_max = np.full(n, 0.1)
    rms_max[5] = 0.8  # loudest unmuted
    rms_max[15] = 0.95  # muted window — must be ignored!
    muted = np.zeros(n)
    muted[15] = 1.0
    rms_z = (rms_max - rms_max.mean()) / rms_max.std()

    features = pl.DataFrame(
        {
            "window_id": win["window_id"],
            "audio_rms_max": rms_max,
            "audio_rms_max_z": rms_z,
            "audio_is_muted": muted,
        }
    )
    unmuted_cand = _candidate(
        1, score=3.0, start_s=40.0, end_s=70.0, top_signals=("audio_rms_max_z",)
    )
    muted_cand = _candidate(2, score=1.0, start_s=150.0, end_s=160.0)

    out = explain_candidates(
        [unmuted_cand, muted_cand], windows=win, features=features, duration_s=300.0
    )

    assert out[0].title == "Loudest moment of stream"
    assert out[0].reason == "Loudest moment of the stream"
    assert out[1].reason == "Steady activity throughout"
    assert all(d.kind != "audio_loudness" for d in out[1].signal_details)


def test_flat_audio_does_not_claim_loudest_of_stream() -> None:
    n = 20
    win = _windows(n)
    features = pl.DataFrame(
        {
            "window_id": win["window_id"],
            "audio_rms_max": np.full(n, 0.2),
            "audio_rms_max_z": np.zeros(n),
            "audio_rms_rz": np.zeros(n),
        }
    )
    cand = _candidate(1, score=0.0, start_s=20.0, end_s=50.0)

    (out,) = explain_candidates([cand], windows=win, features=features, duration_s=200.0)

    assert out.reason == "Steady activity throughout"
    assert out.title == "Stream moment"


@pytest.mark.parametrize(
    ("column", "expected_title", "expected_fragment"),
    [
        ("chat_hype_rate", "Hype chat surge", "lots of hype"),
        ("chat_shock_rate", "Shocked chat reaction", "shocked reactions"),
        ("chat_keyword_rate", '"Clip it" chat spike', '"clip it" calls'),
    ],
)
def test_chat_reaction_categories(column: str, expected_title: str, expected_fragment: str) -> None:
    n = 30
    win = _windows(n)
    msg_rate = np.full(n, 1.0)
    msg_rate[10] = 4.0
    msg_z = (msg_rate - msg_rate.mean()) / msg_rate.std()
    cat_rate = np.zeros(n)
    cat_rate[10] = 2.0

    features = pl.DataFrame(
        {
            "window_id": win["window_id"],
            "chat_msg_rate": msg_rate,
            "chat_msg_rate_z": msg_z,
            column: cat_rate,
        }
    )
    cand = _candidate(1, start_s=90.0, end_s=120.0, top_signals=("chat_msg_rate_z",))

    (out,) = explain_candidates([cand], windows=win, features=features, duration_s=300.0)

    assert out.title == expected_title
    assert expected_fragment in (out.reason or "")


def test_delayed_chat_lead_spike_and_secondary_chat_details() -> None:
    n = 20
    win = _windows(n)
    lead1 = np.zeros(n)
    lead1[5] = 3.2
    events = np.zeros(n)
    events[5] = 3.0
    emote_frac = np.zeros(n)
    emote_frac[5] = 0.72
    msg_rate = np.full(n, 0.5)

    features = pl.DataFrame(
        {
            "window_id": win["window_id"],
            "chat_msg_rate": msg_rate,
            "chat_msg_rate_z": np.zeros(n),
            "chat_msg_rate_lead1": lead1,
            "chat_event_count": events,
            "chat_emote_frac": emote_frac,
        }
    )
    cand = _candidate(1, start_s=50.0, end_s=70.0, top_signals=("chat_msg_rate_lead1",))

    (out,) = explain_candidates([cand], windows=win, features=features, duration_s=200.0)

    assert "chat spiked right after" in (out.reason or "").lower()
    kinds = {d.kind for d in out.signal_details}
    assert {"chat_rate", "chat_events", "chat_emotes"} <= kinds


def test_optional_audio_events_with_null_unscored_windows() -> None:
    n = 15
    win = _windows(n)
    # Subset mode leaves unselected windows as None/null.
    laughter: list[float | None] = [None] * n
    scream: list[float | None] = [None] * n
    laughter[4] = 0.88
    scream[9] = 0.91

    features = pl.DataFrame(
        {
            "window_id": win["window_id"],
            "audio_evt_laughter": pl.Series(laughter, dtype=pl.Float64),
            "audio_evt_scream": pl.Series(scream, dtype=pl.Float64),
        }
    )
    c1 = _candidate(1, score=3.0, start_s=35.0, end_s=55.0)
    c2 = _candidate(2, score=2.0, start_s=85.0, end_s=105.0)
    c3 = _candidate(3, score=1.0, start_s=0.0, end_s=20.0)

    out = explain_candidates([c1, c2, c3], windows=win, features=features, duration_s=150.0)

    assert out[0].title == "Big laugh moment"
    assert "laughter detected" in (out[0].reason or "").lower()
    assert any(d.kind == "audio_event" and "88%" in d.detail for d in out[0].signal_details)

    assert out[1].title == "Loud scream moment"
    assert "screamed or shouted" in (out[1].reason or "").lower()
    assert any(d.kind == "audio_event" and "91%" in d.detail for d in out[1].signal_details)

    # Unscored (null) window should fall back cleanly without errors.
    assert out[2].reason == "Steady activity throughout"


def test_pause_snapped_speech_detected_from_wav_and_not_on_silence(tmp_path: Path) -> None:
    sr = 16_000
    duration_s = 30.0
    t = np.arange(int(duration_s * sr)) / sr
    # Active speech-like tone from 5.2 s to 24.8 s; quiet pauses before 5.2 s and after 24.8 s.
    speech = np.zeros_like(t, dtype=np.float32)
    active = (t >= 5.2) & (t <= 24.8)
    speech[active] = (0.3 * np.sin(2 * math.pi * 440.0 * t[active])).astype(np.float32)
    speech_wav = _write_wav(tmp_path / "speech.wav", speech, sr)

    # Candidate snapped cleanly to pauses right outside speech (5.05 s and 24.95 s).
    snapped = _candidate(1, score=2.0, start_s=5.05, end_s=24.95)
    # Candidate cut mid-speech (8.0 s to 20.0 s).
    unsnapped = _candidate(2, score=1.0, start_s=8.0, end_s=20.0)

    out = explain_candidates([snapped, unsnapped], duration_s=duration_s, audio_path=speech_wav)
    assert any(d.kind == "pause_snap" for d in out[0].signal_details)
    assert "speech pauses" in (out[0].reason or "").lower()
    assert all(d.kind != "pause_snap" for d in out[1].signal_details)

    # Completely silent WAV must NOT claim pause-snapped speech boundaries.
    silent_wav = _write_wav(tmp_path / "silent.wav", np.zeros_like(t, dtype=np.float32), sr)
    (silent_out,) = explain_candidates([snapped], duration_s=duration_s, audio_path=silent_wav)
    assert all(d.kind != "pause_snap" for d in silent_out.signal_details)


def test_fallback_without_features_uses_top_signals_and_viewer_clips() -> None:
    c1 = _candidate(
        1,
        score=3.0,
        start_s=10.0,
        end_s=40.0,
        source="both",
        n_viewer_clips=2,
        top_signals=("chat_msg_rate_z", "audio_rms_max_z"),
    )
    c2 = _candidate(
        2,
        score=2.0,
        start_s=60.0,
        end_s=90.0,
        source="viewer_clips",
        n_viewer_clips=1,
        top_signals=(),
    )
    c3 = _candidate(3, score=1.0, start_s=120.0, end_s=150.0, top_signals=())

    out = explain_candidates([c1, c2, c3])

    assert out[0].title == "Loud reaction & chat spike"
    assert out[0].reason == "Chat spiked; streamer got loud; 2 viewers clipped it"
    assert out[1].title == "Viewer-clipped moment"
    assert out[1].reason == "1 viewer clipped it"
    assert out[2].title == "Stream moment"
    assert out[2].reason == "Steady activity throughout"


def test_privacy_never_leaks_raw_chat_text_or_usernames() -> None:
    n = 10
    win = _windows(n)
    secret_user = "RealViewerName_99123"
    secret_msg = "SUPER_PRIVATE_CHAT_LINE_XYZ"
    secret_hash = "deadbeef" * 8

    features = pl.DataFrame(
        {
            "window_id": win["window_id"],
            "chat_msg_rate": [1.0, 1.0, 8.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0],
            "chat_msg_rate_z": [0.0, 0.0, 4.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
            "chat_laugh_rate": [0.0, 0.0, 5.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
            # Extra raw columns that must never be touched.
            "text": [secret_msg] * n,
            "username": [secret_user] * n,
            "user_hash": [secret_hash] * n,
        }
    )
    cand = _candidate(1, start_s=15.0, end_s=35.0, top_signals=("chat_msg_rate_z",))

    (out,) = explain_candidates([cand], windows=win, features=features, duration_s=100.0)
    serialized = out.model_dump_json()

    assert secret_user not in serialized
    assert secret_msg not in serialized
    assert secret_hash not in serialized
    assert out.title is not None
    assert out.reason is not None


def test_ranking_order_and_scores_are_strictly_unchanged() -> None:
    n = 60
    win = _windows(n)
    msg_rate = np.full(n, 1.0)
    msg_rate[50] = 20.0  # huge chat spike on rank 4 candidate!
    msg_z = (msg_rate - msg_rate.mean()) / msg_rate.std()

    features = pl.DataFrame(
        {
            "window_id": win["window_id"],
            "chat_msg_rate": msg_rate,
            "chat_msg_rate_z": msg_z,
        }
    )
    original = [
        _candidate(1, score=9.5, start_s=10.0, end_s=40.0),
        _candidate(2, score=5.0, start_s=100.0, end_s=130.0),
        _candidate(
            3, score=5.0, start_s=200.0, end_s=230.0, source="viewer_clips", n_viewer_clips=2
        ),
        _candidate(
            4,
            score=1.2,
            start_s=490.0,
            end_s=520.0,
            source="both",
            n_viewer_clips=5,
            top_signals=("chat_msg_rate_z",),
        ),
    ]

    explained_a = explain_candidates(original, windows=win, features=features, duration_s=600.0)
    explained_b = explain_candidates(original, windows=win, features=features, duration_s=600.0)

    assert explained_a == explained_b  # deterministic
    assert len(explained_a) == len(original)
    for before, after in zip(original, explained_a, strict=True):
        assert after.candidate_id == before.candidate_id
        assert after.rank == before.rank
        assert after.score == before.score
        assert after.start_s == before.start_s
        assert after.end_s == before.end_s
        assert after.source == before.source
        assert after.n_viewer_clips == before.n_viewer_clips
        assert after.top_signals == before.top_signals
        assert after.title is not None
        assert after.reason is not None
