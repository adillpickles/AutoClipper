"""Tests for deterministic candidate explanations and titles (autoclipper.models.explain)."""

import math
import wave
from pathlib import Path

import numpy as np
import polars as pl
import pytest

from autoclipper.analysis import Candidate
from autoclipper.models.explain import explain_candidates
from autoclipper.models.pauses import snap_interval_to_wav


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

    # Without an audio-event laughter feature, title keeps loud audio and laughing chat distinct.
    assert explained.title == "Loud audio & laughing chat"
    assert explained.reason == (
        "Chat jumped to 6\u00d7 its usual rate with lots of laugh reactions; "
        "loudest moment in the last 21 minutes; 3 viewer clips"
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
    # Partly muted candidate: window 15 is muted (loud), window 16 is unmuted (quiet),
    # yet `top_signals` still carries `audio_rms_max_z` from the muted window.
    partly_muted_cand = _candidate(
        2, score=1.0, start_s=150.0, end_s=170.0, top_signals=("audio_rms_max_z",)
    )

    out = explain_candidates(
        [unmuted_cand, partly_muted_cand], windows=win, features=features, duration_s=300.0
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
        ("chat_laugh_rate", "Chat erupts laughing", "lots of laugh reactions"),
        ("chat_hype_rate", "Hype chat surge", "lots of hype reactions"),
        ("chat_shock_rate", "Shocked chat reaction", "shocked reactions"),
        ("chat_keyword_rate", "Reaction-heavy chat spike", "reaction keywords"),
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


def test_unique_chatter_spike_stays_distinct_from_message_rate_spike() -> None:
    # Total message rate is flat across the entire stream (z == 0), while unique chatters spike
    # modestly (z == 1.2, so it appears in `top_signals`). Must NOT claim "Chat spiked".
    n = 30
    win = _windows(n)
    unique_z = np.zeros(n)
    unique_z[10] = 1.2
    features = pl.DataFrame(
        {
            "window_id": win["window_id"],
            "chat_msg_rate": np.full(n, 1.0),
            "chat_msg_rate_z": np.zeros(n),
            "chat_unique_rate_z": unique_z,
        }
    )
    cand = _candidate(1, start_s=90.0, end_s=120.0, top_signals=("chat_unique_rate_z",))

    (out,) = explain_candidates([cand], windows=win, features=features, duration_s=300.0)

    assert out.reason == "Lots of viewers reacted at once"
    assert [d.kind for d in out.signal_details] == ["chat_unique"]


def test_emote_and_caps_fractions_measured_on_peak_chat_window_only() -> None:
    # Window 8 has 1 emote in a quiet window (0.1 msg/s, 100% emotes); window 9 is the busy peak
    # (10.0 msg/s, 0% emotes, 0% caps). Must NOT claim "100% emotes in peak window".
    n = 30
    win = _windows(n)
    msg_rate = np.full(n, 1.0)
    msg_rate[8] = 0.1
    msg_rate[9] = 10.0
    emote_frac = np.zeros(n)
    emote_frac[8] = 1.0
    caps_frac = np.zeros(n)
    caps_frac[8] = 1.0

    features = pl.DataFrame(
        {
            "window_id": win["window_id"],
            "chat_msg_rate": msg_rate,
            "chat_emote_frac": emote_frac,
            "chat_caps_frac": caps_frac,
        }
    )
    cand = _candidate(1, start_s=80.0, end_s=110.0)

    (out,) = explain_candidates([cand], windows=win, features=features, duration_s=300.0)

    assert all(d.kind not in ("chat_emotes", "chat_caps") for d in out.signal_details)


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
    msg_rate[5] = 1.0

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
    event_detail = next(d for d in out.signal_details if d.kind == "chat_events")
    assert event_detail.detail == "3 stream events in chat"


def test_optional_audio_events_with_null_unscored_windows_and_vocal_titles() -> None:
    n = 15
    win = _windows(n)
    # Subset mode leaves unselected windows as None/null.
    laughter: list[float | None] = [None] * n
    scream: list[float | None] = [None] * n
    laughter[4] = 0.88
    scream[9] = 0.91
    hype_rate = np.zeros(n)
    hype_rate[9] = 2.0
    msg_rate = np.full(n, 1.0)
    msg_rate[9] = 5.0

    features = pl.DataFrame(
        {
            "window_id": win["window_id"],
            "chat_msg_rate": msg_rate,
            "chat_hype_rate": hype_rate,
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

    # With actual audio_evt_scream + hype chat, vocal "Hype shout" title IS used.
    assert out[1].title == "Hype shout & chat surge"
    assert any(d.kind == "audio_event" and "91%" in d.detail for d in out[1].signal_details)

    # Unscored (null) window should fall back cleanly without errors.
    assert out[2].reason == "Steady activity throughout"


def test_pause_snapped_speech_detected_from_wav_explicit_edges_and_not_on_silence(
    tmp_path: Path,
) -> None:
    sr = 16_000
    duration_s = 40.0
    t = np.arange(int(duration_s * sr)) / sr
    # Speech from 0..10.2 s, long pause 10.2..13.0 s, speech 13.0..34.8 s, silence 34.8..40.0 s.
    speech = np.zeros_like(t, dtype=np.float32)
    active = ((t >= 0.0) & (t <= 10.2)) | ((t >= 13.0) & (t <= 34.8))
    speech[active] = (0.3 * np.sin(2 * math.pi * 440.0 * t[active])).astype(np.float32)
    speech_wav = _write_wav(tmp_path / "speech.wav", speech, sr)

    # Reproduce actual snapping from 10.0..35.0 -> ~12.85..34.95.
    snapped_start, snapped_end = snap_interval_to_wav(
        speech_wav,
        10.0,
        35.0,
        peak=(20.0, 30.0),
        duration_s=duration_s,
        min_len_s=20.0,
        max_len_s=45.0,
    )
    assert snapped_start == pytest.approx(12.85, abs=0.1)

    snapped = _candidate(1, score=2.0, start_s=snapped_start, end_s=snapped_end)
    unsnapped = _candidate(2, score=1.0, start_s=16.0, end_s=28.0)

    out = explain_candidates([snapped, unsnapped], duration_s=duration_s, audio_path=speech_wav)
    assert any(d.kind == "pause_snap" for d in out[0].signal_details)
    assert "speech pauses" in (out[0].reason or "").lower()
    assert all(d.kind != "pause_snap" for d in out[1].signal_details)

    # Explicit per-edge snap metadata takes precedence cleanly.
    (explicit_start_only,) = explain_candidates(
        [unsnapped], pause_snapped_edges={"c002": (True, False)}
    )
    assert any(
        d.kind == "pause_snap" and "Start aligned" in d.detail
        for d in explicit_start_only.signal_details
    )

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

    assert out[0].title == "Loud audio & chat spike"
    assert out[0].reason == "Chat spiked; loud audio spike; 2 viewer clips"
    assert out[1].title == "Viewer-clipped moment"
    assert out[1].reason == "1 viewer clip"
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


def test_ranking_rule_flags_are_stated_in_the_reason_and_details() -> None:
    """A candidate the default ranking's rules lowered says why (#192); others don't."""
    n = 100
    win = _windows(n)
    msg_rate = np.full(n, 1.0)
    msg_rate[4] = 8.0
    msg_rate[50] = 8.0
    features = pl.DataFrame(
        {
            "window_id": win["window_id"],
            "chat_msg_rate": msg_rate,
            "chat_msg_rate_z": (msg_rate - msg_rate.mean()) / msg_rate.std(),
        }
    )
    lowered = _candidate(2, start_s=30.0, end_s=60.0, top_signals=("chat_msg_rate_z",))
    plain = _candidate(1, start_s=490.0, end_s=520.0, top_signals=("chat_msg_rate_z",))

    out = explain_candidates(
        [plain, lowered],
        windows=win,
        features=features,
        ranking_flags={"c002": ("stream_start", "silent_audio")},
    )

    assert out[1].reason is not None
    assert out[1].reason.endswith(
        "; ranked lower: near the start of the stream, the VOD's audio is silent here"
    )
    assert out[1].reason.startswith("Chat jumped")
    rule = [d for d in out[1].signal_details if d.kind == "ranking_rule"]
    assert [d.detail for d in rule] == [
        "Near the start of the stream; the VOD's audio is silent here"
    ]
    assert out[0].reason is not None
    assert "ranked lower" not in out[0].reason
    assert all(d.kind != "ranking_rule" for d in out[0].signal_details)
    # Order and scores are untouched.
    assert [c.candidate_id for c in out] == ["c001", "c002"]
    assert [c.score for c in out] == [plain.score, lowered.score]
