"""Tests for snapping clip boundaries to nearby pauses (#66)."""

import wave
from pathlib import Path

import numpy as np
import polars as pl
import pytest

from autoclipper.models.candidates import CandidateParams, MomentInput, generate_candidates
from autoclipper.models.pauses import (
    PauseSnapParams,
    find_pauses_in_samples,
    find_pauses_in_wav,
    pauses_from_words,
    snap_interval_to_pauses,
    snap_interval_to_wav,
)

SR = 16_000


def _speech_with_pauses_wav(
    path: Path,
    *,
    duration_s: float,
    pauses: list[tuple[float, float]],
    speech_amp: float = 0.25,
    noise_amp: float = 0.002,
) -> Path:
    """Write a 16 kHz mono WAV with steady speech-like tone everywhere except `pauses`."""
    n = round(duration_s * SR)
    t = np.arange(n, dtype=np.float64) / SR
    rng = np.random.default_rng(123)
    signal = speech_amp * np.sin(2 * np.pi * 220.0 * t) + rng.normal(0.0, noise_amp, size=n)
    for start_s, end_s in pauses:
        lo = max(0, round(start_s * SR))
        hi = min(n, round(end_s * SR))
        signal[lo:hi] = rng.normal(0.0, noise_amp, size=hi - lo)
    pcm = np.round(np.clip(signal, -1.0, 1.0) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SR)
        wav.writeframes(pcm.tobytes())
    return path


def test_find_pauses_in_generated_audio_detects_known_pauses(tmp_path: Path) -> None:
    wav = _speech_with_pauses_wav(
        tmp_path / "speech.wav",
        duration_s=20.0,
        pauses=[(4.0, 4.8), (12.2, 13.0)],
    )

    found = find_pauses_in_wav(wav, 0.0, 20.0)

    assert len(found) == 2
    assert found[0][0] == pytest.approx(4.0, abs=0.06)
    assert found[0][1] == pytest.approx(4.8, abs=0.06)
    assert found[1][0] == pytest.approx(12.2, abs=0.06)
    assert found[1][1] == pytest.approx(13.0, abs=0.06)


def test_snap_moves_mid_word_start_and_end_to_nearby_pauses(tmp_path: Path) -> None:
    """Boundaries sitting mid-speech nudge to close pauses (within max_shift_s)."""
    wav = _speech_with_pauses_wav(
        tmp_path / "vod.wav",
        duration_s=90.0,
        pauses=[(18.0, 19.0), (50.8, 51.8)],
    )

    snapped_start, snapped_end = snap_interval_to_wav(
        wav,
        20.0,
        50.0,
        peak=(30.0, 40.0),
        duration_s=90.0,
        min_len_s=20.0,
        max_len_s=45.0,
        params=PauseSnapParams(max_shift_s=3.0),
    )

    # Start snaps inside [18.0, 19.0] near its end (clean lead-in before speech at 19.0).
    assert 18.0 <= snapped_start <= 19.0
    # End snaps inside [50.8, 51.8] near its start (clean tail right after speech ends at 50.8).
    assert 50.8 <= snapped_end <= 51.8
    # Peak window [30, 40] remains strictly inside.
    assert snapped_start <= 30.0
    assert snapped_end >= 40.0


def test_snap_never_cuts_the_peak_moment_out() -> None:
    """Even when the only nearby pauses lie inside the peak window, the peak stays intact."""
    snapped_start, snapped_end = snap_interval_to_pauses(
        30.0,
        50.0,
        peak=(30.0, 50.0),
        duration_s=100.0,
        start_pauses=[(31.0, 32.0)],  # inside peak -> forbidden for start
        end_pauses=[(48.0, 49.0)],  # inside peak -> forbidden for end
        min_len_s=20.0,
        max_len_s=45.0,
    )

    assert snapped_start <= 30.0
    assert snapped_end >= 50.0
    assert (snapped_start, snapped_end) == (30.0, 50.0)


def test_snap_respects_max_len_s_when_both_edges_want_to_expand() -> None:
    """Expanding both boundaries past max_len_s is prevented while still snapping cleanly."""
    snapped_start, snapped_end = snap_interval_to_pauses(
        20.0,
        64.0,  # already 44 s, max_len_s is 45 s
        peak=(30.0, 40.0),
        duration_s=100.0,
        start_pauses=[(17.5, 18.2)],  # would expand by ~2 s earlier
        end_pauses=[(65.5, 66.2)],  # would expand by ~1.7 s later
        min_len_s=20.0,
        max_len_s=45.0,
    )

    assert 20.0 <= snapped_end - snapped_start <= 45.0
    assert snapped_start <= 30.0
    assert snapped_end >= 40.0


def test_snap_leaves_boundaries_alone_when_no_pause_is_close(tmp_path: Path) -> None:
    wav = _speech_with_pauses_wav(
        tmp_path / "continuous.wav",
        duration_s=80.0,
        pauses=[],  # continuous tone with no pauses anywhere
    )

    snapped = snap_interval_to_wav(
        wav,
        20.0,
        50.0,
        peak=(30.0, 40.0),
        duration_s=80.0,
        min_len_s=20.0,
        max_len_s=45.0,
    )

    assert snapped == (20.0, 50.0)


def test_pauses_from_words_works_with_snap_interval() -> None:
    words = [
        (17.0, 18.2),
        # 0.8 s pause: [18.2, 19.0]
        (19.0, 21.5),
        (48.0, 50.6),
        # 0.9 s pause: [50.6, 51.5]
        (51.5, 53.0),
    ]
    start_pauses = pauses_from_words(words, span_start_s=16.0, span_end_s=24.0)
    end_pauses = pauses_from_words(words, span_start_s=46.0, span_end_s=54.0)

    start_s, end_s = snap_interval_to_pauses(
        20.0,
        50.0,
        peak=(30.0, 40.0),
        duration_s=80.0,
        start_pauses=start_pauses,
        end_pauses=end_pauses,
        min_len_s=20.0,
        max_len_s=45.0,
    )

    assert 18.2 <= start_s <= 19.0
    assert 50.6 <= end_s <= 51.5


def test_generate_candidates_snaps_boundaries_when_audio_path_is_given(tmp_path: Path) -> None:
    """End-to-end through generate_candidates: candidate snaps onto generated audio pauses."""
    # 12 windows of 10 s (0..120 s), single clear peak at window 5 ([50, 60)).
    # Default bounds: pre_s=20 -> start=30, post_s=10 -> end=70.
    scores = pl.DataFrame(
        {
            "window_id": [f"w{i}" for i in range(12)],
            "start_s": [float(i * 10) for i in range(12)],
            "end_s": [float((i + 1) * 10) for i in range(12)],
            "score": [0.0, 0.0, 0.0, 0.0, 0.2, 5.0, 0.2, 0.0, 0.0, 0.0, 0.0, 0.0],
        }
    )
    wav = _speech_with_pauses_wav(
        tmp_path / "vod.wav",
        duration_s=120.0,
        pauses=[(28.0, 29.0), (71.0, 72.0)],
    )

    params = CandidateParams(smooth_windows=1)
    unsnapped = generate_candidates(scores, duration_s=120.0, params=params)
    snapped = generate_candidates(scores, duration_s=120.0, params=params, audio_path=wav)

    assert len(unsnapped) == len(snapped) == 1
    assert (unsnapped[0].start_s, unsnapped[0].end_s) == (30.0, 70.0)
    assert 28.0 <= snapped[0].start_s <= 29.0
    assert 71.0 <= snapped[0].end_s <= 72.0
    assert snapped[0].start_s <= 50.0  # peak preserved
    assert snapped[0].end_s >= 60.0


def test_find_pauses_in_samples_handles_silent_and_empty_inputs() -> None:
    assert find_pauses_in_samples(np.array([], dtype=np.float32), SR) == []
    silent = np.zeros(SR * 2, dtype=np.float32)
    assert find_pauses_in_samples(silent, SR, offset_s=5.0) == [(5.0, 7.0)]
    # Silent audio shorter than min_pause_s must not be returned as a pause.
    short_silent = np.zeros(round(SR * 0.1), dtype=np.float32)
    assert find_pauses_in_samples(short_silent, SR, offset_s=5.0) == []
    # Exact min_pause_s silent span must be kept even when offset_s + 0.2 - offset_s rounds low.
    exact_min = np.zeros(round(SR * 0.2), dtype=np.float32)
    assert find_pauses_in_samples(exact_min, SR, offset_s=10.0) == [(10.0, 10.2)]


def test_find_pauses_in_samples_does_not_mark_loud_burst_in_silence_as_silent() -> None:
    """A loud 100 ms burst inside a 2 s silent slice must split the silence into two pauses."""
    samples = np.zeros(SR * 2, dtype=np.float32)
    burst_lo = round(0.95 * SR)
    burst_hi = round(1.05 * SR)
    t = np.arange(burst_hi - burst_lo, dtype=np.float64) / SR
    samples[burst_lo:burst_hi] = (0.5 * np.sin(2 * np.pi * 440.0 * t)).astype(np.float32)

    found = find_pauses_in_samples(samples, SR, offset_s=10.0)

    assert len(found) == 2
    assert found[0][0] == pytest.approx(10.0)
    assert found[0][1] <= 10.95
    assert found[1][0] >= 11.05
    assert found[1][1] == pytest.approx(12.0)


def test_snap_shrinking_off_viewer_moment_recomputes_attribution(tmp_path: Path) -> None:
    """A discovered span that snaps away from a viewer moment stays `discovered`, and the
    viewer moment becomes its own `viewer_clips` candidate instead of mislabeling `both`."""
    # Peak at window 11 ([110, 120)): with pre_s=10, post_s=10, unsnapped span is [100, 130).
    scores = pl.DataFrame(
        {
            "window_id": [f"w{i}" for i in range(20)],
            "start_s": [float(i * 10) for i in range(20)],
            "end_s": [float((i + 1) * 10) for i in range(20)],
            "score": [5.0 if i == 11 else 0.0 for i in range(20)],
        }
    )
    moment = MomentInput("m0", 129.0, 149.0, ("clip1", "clip2"))
    # Pause at [127.2, 128.0] pulls the discovered end to ~127.35, strictly before 129.0.
    wav = _speech_with_pauses_wav(
        tmp_path / "vod.wav",
        duration_s=200.0,
        pauses=[(127.2, 128.0)],
    )
    params = CandidateParams(smooth_windows=1, pre_s=10.0, post_s=10.0)

    unsnapped = generate_candidates(scores, duration_s=200.0, moments=[moment], params=params)
    assert len(unsnapped) == 1
    assert unsnapped[0].source == "both"
    assert unsnapped[0].n_viewer_clips == 2

    snapped = generate_candidates(
        scores, duration_s=200.0, moments=[moment], params=params, audio_path=wav
    )
    assert len(snapped) == 2
    by_source = {c.source: c for c in snapped}
    assert set(by_source) == {"discovered", "viewer_clips"}
    disc = by_source["discovered"]
    assert 127.2 <= disc.end_s <= 128.0
    assert disc.n_viewer_clips == 0
    clipped = by_source["viewer_clips"]
    assert clipped.n_viewer_clips == 2
    assert clipped.start_s < moment.end_s
    assert moment.start_s < clipped.end_s


def test_snap_expanding_into_viewer_moment_marks_both_even_past_shortlist(tmp_path: Path) -> None:
    """A discovered peak past `shortlist_k` that expands onto a viewer moment during snapping
    is promoted to `both` with the moment's clips instead of emitting a duplicate."""
    # Window 3 ([30, 40)) is the sole shortlisted peak; window 11 ([110, 120)) is beyond
    # shortlist_k=1 with unsnapped span [100, 128) (pre_s=10, post_s=8).
    scores = pl.DataFrame(
        {
            "window_id": [f"w{i}" for i in range(20)],
            "start_s": [float(i * 10) for i in range(20)],
            "end_s": [float((i + 1) * 10) for i in range(20)],
            "score": [9.0 if i == 3 else (5.0 if i == 11 else 0.0) for i in range(20)],
        }
    )
    moment = MomentInput("m0", 129.0, 149.0, ("clip1",))
    # Pause at [129.6, 130.4] pulls window 11's end from 128.0 to ~129.75, overlapping m0.
    wav = _speech_with_pauses_wav(
        tmp_path / "vod.wav",
        duration_s=200.0,
        pauses=[(129.6, 130.4)],
    )
    params = CandidateParams(smooth_windows=1, pre_s=10.0, post_s=8.0, shortlist_k=1)

    unsnapped = generate_candidates(scores, duration_s=200.0, moments=[moment], params=params)
    assert {c.source for c in unsnapped} == {"discovered", "viewer_clips"}

    snapped = generate_candidates(
        scores, duration_s=200.0, moments=[moment], params=params, audio_path=wav
    )
    assert len(snapped) == 2
    by_source = {c.source: c for c in snapped}
    assert set(by_source) == {"discovered", "both"}
    merged = by_source["both"]
    assert merged.score == 5.0
    assert merged.end_s > 129.0
    assert merged.n_viewer_clips == 1
