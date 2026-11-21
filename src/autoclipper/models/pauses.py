"""Snap candidate clip boundaries to nearby speech pauses (#66).

A clip that starts or ends mid-word feels broken. Given a candidate's draft ``[start_s, end_s)``
and its peak window, this module finds nearby quiet pauses (from the normalized 16 kHz WAV or
from transcript word timestamps) and nudges `start_s` and `end_s` by up to `max_shift_s` so the
clip starts and ends cleanly without ever cutting the peak moment out or violating clip length
and VOD bounds.
"""

import math
import wave
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np


@dataclass(frozen=True, slots=True)
class PauseSnapParams:
    """Settings for snapping clip boundaries to pauses (#66).

    Attributes:
        max_shift_s: Maximum distance (seconds) either boundary may move to reach a pause.
        min_pause_s: Minimum contiguous silence/gap length (seconds) treated as a real pause.
        frame_s: Analysis frame length (seconds) for audio silence detection.
        hop_s: Hop between consecutive RMS frames (seconds).
        quiet_ratio: Fraction of local active speech RMS (80th percentile) below which a frame
            counts as quiet.
        abs_floor_rms: Absolute RMS floor (0..1 full scale) below which a frame is always quiet.
        margin_s: Clean silence buffer kept right before speech starts or after speech ends.
    """

    max_shift_s: float = 3.0
    min_pause_s: float = 0.2
    frame_s: float = 0.04
    hop_s: float = 0.02
    quiet_ratio: float = 0.2
    abs_floor_rms: float = 0.005
    margin_s: float = 0.15

    def __post_init__(self) -> None:
        if not (math.isfinite(self.max_shift_s) and self.max_shift_s >= 0):
            raise ValueError(f"max_shift_s must be finite and >= 0, got {self.max_shift_s!r}")
        if not (math.isfinite(self.min_pause_s) and self.min_pause_s > 0):
            raise ValueError(f"min_pause_s must be positive, got {self.min_pause_s!r}")
        if not (math.isfinite(self.frame_s) and self.frame_s > 0):
            raise ValueError(f"frame_s must be positive, got {self.frame_s!r}")
        if not (math.isfinite(self.hop_s) and self.hop_s > 0):
            raise ValueError(f"hop_s must be positive, got {self.hop_s!r}")
        if not (0 < self.quiet_ratio < 1):
            raise ValueError(f"quiet_ratio must be in (0, 1), got {self.quiet_ratio!r}")
        if not (0 <= self.abs_floor_rms < 1):
            raise ValueError(f"abs_floor_rms must be in [0, 1), got {self.abs_floor_rms!r}")
        if not (math.isfinite(self.margin_s) and self.margin_s >= 0):
            raise ValueError(f"margin_s must be finite and >= 0, got {self.margin_s!r}")


def find_pauses_in_samples(
    samples: np.ndarray,
    sample_rate_hz: int,
    *,
    offset_s: float = 0.0,
    params: PauseSnapParams | None = None,
) -> list[tuple[float, float]]:
    """Find contiguous quiet intervals in float samples normalized to ``[-1, 1]``.

    Args:
        samples: 1-D mono float samples in ``[-1, 1]``.
        sample_rate_hz: Sampling rate in Hz.
        offset_s: VOD time of `samples[0]`.
        params: Detection parameters.

    Returns:
        Sorted list of ``(start_s, end_s)`` pause intervals in VOD time.
    """
    params = params or PauseSnapParams()
    if sample_rate_hz <= 0 or samples.size == 0:
        return []
    frame_len = max(1, round(params.frame_s * sample_rate_hz))
    hop_len = max(1, round(params.hop_s * sample_rate_hz))
    if samples.size < frame_len:
        rms = np.array([float(np.sqrt(np.mean(np.square(samples, dtype=np.float64))))])
    else:
        n_frames = 1 + (samples.size - frame_len) // hop_len
        sq = np.square(samples.astype(np.float64, copy=False))
        cumsum = np.concatenate(([0.0], np.cumsum(sq)))
        starts = np.arange(n_frames) * hop_len
        rms = np.sqrt(np.maximum(0.0, (cumsum[starts + frame_len] - cumsum[starts]) / frame_len))

    # Measure local speech level from the 80th percentile so quiet stretches stay below threshold
    # even with mild background noise, while completely silent stretches return one full pause.
    loud_rms = float(np.percentile(rms, 80))
    if loud_rms <= params.abs_floor_rms:
        return [(offset_s, offset_s + samples.size / sample_rate_hz)]
    threshold = max(params.abs_floor_rms, params.quiet_ratio * loud_rms)
    quiet = rms <= threshold

    pauses: list[tuple[float, float]] = []
    run_start: int | None = None
    for idx, is_quiet in enumerate(quiet):
        if is_quiet and run_start is None:
            run_start = idx
        elif not is_quiet and run_start is not None:
            p_start = offset_s + run_start * hop_len / sample_rate_hz
            p_end = offset_s + ((idx - 1) * hop_len + frame_len) / sample_rate_hz
            if p_end - p_start >= params.min_pause_s:
                pauses.append((p_start, p_end))
            run_start = None
    if run_start is not None:
        p_start = offset_s + run_start * hop_len / sample_rate_hz
        p_end = offset_s + samples.size / sample_rate_hz
        if p_end - p_start >= params.min_pause_s:
            pauses.append((p_start, p_end))
    return pauses


def find_pauses_in_wav(
    wav_path: Path,
    start_s: float,
    end_s: float,
    *,
    params: PauseSnapParams | None = None,
) -> list[tuple[float, float]]:
    """Read only ``[start_s, end_s)`` of a 16-bit PCM WAV and return its quiet pauses."""
    params = params or PauseSnapParams()
    if end_s <= start_s:
        return []
    with wave.open(str(wav_path), "rb") as wav:
        if wav.getsampwidth() != 2:
            raise ValueError(f"expected 16-bit PCM WAV, got {wav.getsampwidth() * 8}-bit")
        sr = wav.getframerate()
        n_channels = wav.getnchannels()
        total_frames = wav.getnframes()
        first = max(0, min(total_frames, math.floor(start_s * sr)))
        last = max(first, min(total_frames, math.ceil(end_s * sr)))
        if last <= first:
            return []
        wav.setpos(first)
        raw = wav.readframes(last - first)
    data = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if n_channels > 1:
        data = data.reshape(-1, n_channels).mean(axis=1)
    return find_pauses_in_samples(data, sr, offset_s=first / sr, params=params)


def pauses_from_words(
    words: Sequence[tuple[float, float]],
    *,
    span_start_s: float,
    span_end_s: float,
    min_pause_s: float = 0.2,
) -> list[tuple[float, float]]:
    """Derive pause intervals from ``(word_start_s, word_end_s)`` transcript timestamps."""
    ordered = sorted(
        (max(span_start_s, s), min(span_end_s, e))
        for s, e in words
        if e > span_start_s and s < span_end_s and s < e
    )
    if not ordered:
        return [(span_start_s, span_end_s)] if span_end_s - span_start_s >= min_pause_s else []
    pauses: list[tuple[float, float]] = []
    cursor = span_start_s
    for w_start, w_end in ordered:
        if w_start - cursor >= min_pause_s:
            pauses.append((cursor, w_start))
        cursor = max(cursor, w_end)
    if span_end_s - cursor >= min_pause_s:
        pauses.append((cursor, span_end_s))
    return pauses


def snap_interval_to_pauses(
    start_s: float,
    end_s: float,
    *,
    peak: tuple[float, float],
    duration_s: float,
    start_pauses: Sequence[tuple[float, float]],
    end_pauses: Sequence[tuple[float, float]],
    min_len_s: float,
    max_len_s: float,
    params: PauseSnapParams | None = None,
) -> tuple[float, float]:
    """Nudge `start_s` and `end_s` to nearby pauses while protecting `peak` and length limits.

    Guaranteed invariants:
    - ``0.0 <= new_start < new_end <= duration_s``
    - ``new_start <= peak[0]`` (when ``start_s <= peak[0]``) and ``new_end >= peak[1]`` (when
      ``end_s >= peak[1]``), so the peak moment is never cut out.
    - Length stays inside ``[min(min_len_s, orig_len), max(max_len_s, orig_len)]``.
    - Each edge moves at most ``params.max_shift_s``.
    """
    params = params or PauseSnapParams()
    if params.max_shift_s == 0:
        return float(start_s), float(end_s)

    orig_len = end_s - start_s
    eff_min = min(min_len_s, orig_len)
    eff_max = max(max_len_s, orig_len)

    # Peak protection: never move start past peak start, or end before peak end.
    start_limit = min(peak[0], end_s - eff_min) if start_s <= peak[0] else start_s
    end_limit = max(peak[1], start_s + eff_min) if end_s >= peak[1] else end_s

    start_lo = max(0.0, start_s - params.max_shift_s)
    start_hi = min(start_limit, start_s + params.max_shift_s)
    end_lo = max(end_limit, end_s - params.max_shift_s)
    end_hi = min(duration_s, end_s + params.max_shift_s)

    snapped_start = _pick_boundary(
        start_s, start_lo, start_hi, start_pauses, edge="start", margin_s=params.margin_s
    )
    snapped_end = _pick_boundary(
        end_s, end_lo, end_hi, end_pauses, edge="end", margin_s=params.margin_s
    )

    if _valid_pair(snapped_start, snapped_end, eff_min, eff_max, duration_s):
        return _clean(snapped_start, duration_s), _clean(snapped_end, duration_s)

    # Snapping both independently pushed the length outside [eff_min, eff_max]: pick the best
    # constrained alternative (either snapping one edge, or re-snapping the second inside the
    # remaining length budget) that moves boundaries the least.
    candidates: list[tuple[float, float]] = []
    if _valid_pair(snapped_start, end_s, eff_min, eff_max, duration_s):
        candidates.append((snapped_start, end_s))
    if _valid_pair(start_s, snapped_end, eff_min, eff_max, duration_s):
        candidates.append((start_s, snapped_end))

    # Try keeping snapped_start and re-picking end within the length budget.
    alt_end = _pick_boundary(
        end_s,
        max(end_lo, snapped_start + eff_min),
        min(end_hi, snapped_start + eff_max),
        end_pauses,
        edge="end",
        margin_s=params.margin_s,
    )
    if _valid_pair(snapped_start, alt_end, eff_min, eff_max, duration_s):
        candidates.append((snapped_start, alt_end))

    # Try keeping snapped_end and re-picking start within the length budget.
    alt_start = _pick_boundary(
        start_s,
        max(start_lo, snapped_end - eff_max),
        min(start_hi, snapped_end - eff_min),
        start_pauses,
        edge="start",
        margin_s=params.margin_s,
    )
    if _valid_pair(alt_start, snapped_end, eff_min, eff_max, duration_s):
        candidates.append((alt_start, snapped_end))

    if not candidates:
        return float(start_s), float(end_s)

    # Prefer snapping both edges when possible, then smallest total displacement.
    best_start, best_end = min(
        candidates,
        key=lambda pair: (
            -(int(pair[0] != start_s) + int(pair[1] != end_s)),
            abs(pair[0] - start_s) + abs(pair[1] - end_s),
        ),
    )
    return _clean(best_start, duration_s), _clean(best_end, duration_s)


def snap_interval_to_wav(
    wav_path: Path,
    start_s: float,
    end_s: float,
    *,
    peak: tuple[float, float],
    duration_s: float,
    min_len_s: float,
    max_len_s: float,
    params: PauseSnapParams | None = None,
) -> tuple[float, float]:
    """Snap ``[start_s, end_s)`` to pauses detected in `wav_path` around each boundary."""
    params = params or PauseSnapParams()
    if params.max_shift_s == 0:
        return float(start_s), float(end_s)
    pad = params.max_shift_s + params.min_pause_s
    start_pauses = find_pauses_in_wav(
        wav_path, max(0.0, start_s - pad), min(duration_s, start_s + pad), params=params
    )
    end_pauses = find_pauses_in_wav(
        wav_path, max(0.0, end_s - pad), min(duration_s, end_s + pad), params=params
    )
    return snap_interval_to_pauses(
        start_s,
        end_s,
        peak=peak,
        duration_s=duration_s,
        start_pauses=start_pauses,
        end_pauses=end_pauses,
        min_len_s=min_len_s,
        max_len_s=max_len_s,
        params=params,
    )


def _pick_boundary(
    target: float,
    lo: float,
    hi: float,
    pauses: Sequence[tuple[float, float]],
    *,
    edge: str,
    margin_s: float,
) -> float:
    """Return the best boundary time inside ``[lo, hi]`` on a pause, or `target` if none."""
    if lo > hi:
        return target
    best: float | None = None
    best_dist = float("inf")
    for p_start, p_end in pauses:
        a = max(lo, p_start)
        b = min(hi, p_end)
        if a > b:
            continue
        if a <= target <= b:
            return target  # already inside a pause
        if edge == "start":
            # Start just before speech resumes (clean lead-in without dead air).
            point = max(a, b - min(margin_s, (b - a) / 2))
        else:
            # End just after speech finishes (clean tail without cutting off).
            point = min(b, a + min(margin_s, (b - a) / 2))
        dist = abs(point - target)
        if dist < best_dist:
            best_dist = dist
            best = point
    return target if best is None else best


def _valid_pair(
    start_s: float, end_s: float, min_len_s: float, max_len_s: float, duration_s: float
) -> bool:
    return (
        0.0 <= start_s < end_s <= duration_s
        and min_len_s - 1e-6 <= (end_s - start_s) <= max_len_s + 1e-6
    )


def _clean(value: float, duration_s: float) -> float:
    return float(min(duration_s, max(0.0, round(value, 3))))
