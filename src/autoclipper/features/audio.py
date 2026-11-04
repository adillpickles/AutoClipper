"""Audio features per time window: how loud and busy the stream sounds (docs/ml/features.md).

`compute_audio_features` reads a VOD's normalized audio (16 kHz mono 16-bit WAV, from
`autoclipper.ingest.media.normalize_audio`) and its ``windows``, and returns the
``features_audio`` table: one row per window, keyed by ``window_id``.

How it works:

- The WAV is read in blocks of `BLOCK_FRAMES` analysis frames, never whole: a 6-hour VOD is
  about 700 MB of samples. Frames are `FRAME_LENGTH` samples (64 ms) every `HOP_LENGTH` samples
  (32 ms) on one grid over the whole file, so block edges change nothing.
- Per frame (NumPy only): RMS energy, zero-crossing rate, spectral centroid of the Hann-windowed
  magnitude spectrum (Hz), and spectral flux (the summed increase of the log-magnitude spectrum
  from the previous frame). Onsets are peaks of the flux envelope: local maxima over
  ±`ONSET_PEAK_FRAMES` frames that exceed the local mean over ±`ONSET_MEAN_FRAMES` frames by
  `ONSET_DELTA`, at most one per `ONSET_WAIT_FRAMES` frames (the method librosa's
  ``onset_detect`` uses).
- A frame belongs to the window holding its centre. A window with no frames (audio shorter
  than the VOD) gets 0 and is counted in a warning.
- Silence is RMS below `SILENCE_RMS` (-60 dBFS). Windows overlapping a Twitch muted segment keep
  their values and get ``audio_is_muted = 1``.
- ``_z`` against all windows of the VOD, ``_rz`` against the previous `ROLLING_WINDOWS` windows
  (at least two, else 0). The standard deviation is floored at `STD_FLOOR_RMS` (-60 dBFS), so
  near-constant audio can't produce huge scores, and a VOD where every window is alike scores 0.
  Lead/lag columns are ``audio_rms_max_z`` of windows t±k, 0 past the VOD's edges.
"""

import logging
import math
import wave
from pathlib import Path
from typing import Final

import numpy as np
import numpy.typing as npt
import polars as pl

log = logging.getLogger(__name__)

AUDIO_FEATURE_VERSION: Final = "audio-v1"

SAMPLE_RATE_HZ: Final = 16_000
FRAME_LENGTH: Final = 1024  # samples: 64 ms
HOP_LENGTH: Final = 512  # samples: 32 ms
BLOCK_FRAMES: Final = 1 << 15  # frames per read: about 17 minutes, 64 MB of float32 frames

SILENCE_RMS: Final = 10 ** (-60 / 20)  # -60 dBFS, full scale = 1.0
ONSET_PEAK_FRAMES: Final = 3
ONSET_MEAN_FRAMES: Final = 10
ONSET_DELTA: Final = 0.07
ONSET_WAIT_FRAMES: Final = 3
# Floor for the log-magnitude spectrum, so digital silence doesn't make flux explode.
_LOG_FLOOR: Final = 1e-5

# docs/ml/features.md § Normalization.
ROLLING_WINDOWS: Final = 30
# Smallest standard deviation an RMS z-score divides by: the silence level (see `_z`).
STD_FLOOR_RMS: Final = SILENCE_RMS
LEAD_LAG_WINDOWS: Final = (1, 2)

WINDOW_COLUMNS: Final = ("window_id", "video_id", "index", "start_s", "end_s", "is_muted")

FEATURE_COLUMNS: Final = (
    "audio_rms_mean",
    "audio_rms_max",
    "audio_rms_max_z",
    "audio_rms_std",
    "audio_onset_rate",
    "audio_spectral_centroid_mean",
    "audio_zcr_mean",
    "audio_silence_frac",
    "audio_rms_rz",
    "audio_is_muted",
    *(f"audio_rms_max_lead{k}" for k in LEAD_LAG_WINDOWS),
    *(f"audio_rms_max_lag{k}" for k in LEAD_LAG_WINDOWS),
)
"""The ``features_audio`` feature columns, all Float64, in output order."""

type FloatArray = npt.NDArray[np.float64]


class AudioFormatError(ValueError):
    """The WAV isn't 16 kHz mono 16-bit PCM, or can't be read."""


def compute_audio_features(windows: pl.DataFrame, wav_path: Path) -> pl.DataFrame:
    """Compute the audio features of one VOD's windows from its normalized WAV.

    Args:
        windows: Rows of the ``windows`` table of one VOD (`autoclipper.labels.windows`).
        wav_path: The VOD's 16 kHz mono 16-bit WAV (``data/interim/audio/<video_id>.wav``).

    Returns:
        One row per window in window order: ``window_id``, the `FEATURE_COLUMNS` (Float64,
        never null) and ``feature_version``.

    Raises:
        ValueError: A column is missing or null, the windows belong to several VODs, repeat,
            overlap or have no length.
        AudioFormatError: The WAV is missing, unreadable or not 16 kHz mono 16-bit PCM.
    """
    windows = _checked_windows(windows)
    frames = frame_features(wav_path)
    centres_s = (np.arange(len(frames.rms)) * HOP_LENGTH + FRAME_LENGTH / 2) / SAMPLE_RATE_HZ
    onsets = detect_onsets(frames.flux)

    starts = windows["start_s"].to_numpy()
    ends = windows["end_s"].to_numpy()
    # The window whose start is the last one <= the frame centre, if the centre is before its end.
    slot = np.searchsorted(starts, centres_s, side="right") - 1
    inside = (slot >= 0) & (centres_s < ends[np.clip(slot, 0, None)])
    slot = slot[inside]
    n = windows.height
    count = np.bincount(slot, minlength=n).astype(np.float64)

    def total(values: FloatArray) -> FloatArray:
        return np.bincount(slot, weights=values[inside], minlength=n)

    def mean(values: FloatArray) -> FloatArray:
        return np.divide(total(values), count, out=np.zeros(n), where=count > 0)

    rms = frames.rms
    rms_mean = mean(rms)
    rms_sq_mean = mean(rms * rms)
    rms_std = np.sqrt(np.clip(rms_sq_mean - rms_mean * rms_mean, 0.0, None))
    rms_max = np.zeros(n)
    np.maximum.at(rms_max, slot, rms[inside])
    seconds = ends - starts
    empty = int((count == 0).sum())
    if empty:
        log.warning(
            "audio windows without audio video_id=%s n=%d of=%d path=%s",
            windows["video_id"][0],
            empty,
            n,
            wav_path,
        )

    frame = windows.with_columns(
        audio_rms_mean=pl.Series(rms_mean),
        audio_rms_max=pl.Series(rms_max),
        audio_rms_std=pl.Series(rms_std),
        audio_onset_rate=pl.Series(total(onsets.astype(np.float64)) / seconds),
        audio_spectral_centroid_mean=pl.Series(mean(frames.centroid_hz)),
        audio_zcr_mean=pl.Series(mean(frames.zcr)),
        audio_silence_frac=pl.Series(mean((rms < SILENCE_RMS).astype(np.float64))),
        audio_is_muted=pl.col("is_muted").cast(pl.Float64),
    )
    frame = frame.with_columns(
        audio_rms_max_z=_vod_z("audio_rms_max"), audio_rms_rz=_rolling_z("audio_rms_max")
    )
    frame = frame.with_columns(
        *(
            pl.col("audio_rms_max_z").shift(-k).fill_null(0.0).alias(f"audio_rms_max_lead{k}")
            for k in LEAD_LAG_WINDOWS
        ),
        *(
            pl.col("audio_rms_max_z").shift(k).fill_null(0.0).alias(f"audio_rms_max_lag{k}")
            for k in LEAD_LAG_WINDOWS
        ),
    )
    return frame.select(
        "window_id",
        *(pl.col(c).cast(pl.Float64) for c in FEATURE_COLUMNS),
        feature_version=pl.lit(AUDIO_FEATURE_VERSION),
    )


class FrameFeatures:
    """Per-frame values of a whole WAV, one entry per analysis frame (`HOP_LENGTH` apart)."""

    def __init__(
        self, rms: FloatArray, zcr: FloatArray, centroid_hz: FloatArray, flux: FloatArray
    ) -> None:
        self.rms = rms
        self.zcr = zcr
        self.centroid_hz = centroid_hz
        self.flux = flux


def frame_features(wav_path: Path) -> FrameFeatures:
    """Read `wav_path` block by block and compute RMS, ZCR, centroid and flux per frame.

    Raises:
        AudioFormatError: The file is missing, unreadable or not 16 kHz mono 16-bit PCM.
    """
    try:
        reader = wave.open(str(wav_path), "rb")  # noqa: SIM115 (closed below)
    except (OSError, wave.Error, EOFError) as exc:
        raise AudioFormatError(f"can't read WAV {wav_path}: {exc}") from exc
    with reader:
        layout = (reader.getframerate(), reader.getnchannels(), reader.getsampwidth())
        if layout != (SAMPLE_RATE_HZ, 1, 2):
            raise AudioFormatError(
                f"{wav_path} is {layout[0]} Hz, {layout[1]} channel(s), {8 * layout[2]}-bit; "
                f"expected {SAMPLE_RATE_HZ} Hz mono 16-bit (run normalize_audio first)"
            )
        n_samples = reader.getnframes()
        n_frames = 0 if n_samples < FRAME_LENGTH else 1 + (n_samples - FRAME_LENGTH) // HOP_LENGTH
        rms = np.zeros(n_frames)
        zcr = np.zeros(n_frames)
        centroid = np.zeros(n_frames)
        flux = np.zeros(n_frames)
        window = np.hanning(FRAME_LENGTH).astype(np.float32)
        freqs = np.fft.rfftfreq(FRAME_LENGTH, d=1 / SAMPLE_RATE_HZ).astype(np.float32)
        previous_log: npt.NDArray[np.float32] | None = None
        for first in range(0, n_frames, BLOCK_FRAMES):
            count = min(BLOCK_FRAMES, n_frames - first)
            reader.setpos(first * HOP_LENGTH)
            n_read = (count - 1) * HOP_LENGTH + FRAME_LENGTH
            raw = reader.readframes(n_read)
            if len(raw) != 2 * n_read:
                raise AudioFormatError(f"{wav_path} ended early: its header promised more audio")
            samples = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
            frames = np.lib.stride_tricks.sliding_window_view(samples, FRAME_LENGTH)[::HOP_LENGTH]
            block = slice(first, first + count)
            rms[block] = np.sqrt(np.mean(frames * frames, axis=1, dtype=np.float64))
            signs = np.signbit(frames)
            zcr[block] = np.mean(signs[:, 1:] != signs[:, :-1], axis=1)
            magnitude = np.abs(np.fft.rfft(frames * window, axis=1)).astype(np.float32)
            power = magnitude.sum(axis=1)
            centroid[block] = np.divide(
                magnitude @ freqs, power, out=np.zeros(count, dtype=np.float32), where=power > 0
            )
            log_mag = np.log10(np.maximum(magnitude, _LOG_FLOOR))
            before = np.vstack(
                [log_mag[:1] if previous_log is None else previous_log, log_mag[:-1]]
            )
            flux[block] = np.maximum(log_mag - before, 0.0).sum(axis=1)
            previous_log = log_mag[-1:]
    log.debug("audio frames path=%s n=%d", wav_path, n_frames)
    return FrameFeatures(rms, zcr, centroid, flux)


def detect_onsets(flux: FloatArray) -> npt.NDArray[np.bool_]:
    """Mark onset frames: peaks of the normalized flux envelope (see the module docstring)."""
    if not len(flux):
        return np.zeros(0, dtype=bool)
    peak = float(flux.max())
    envelope = flux / peak if peak > 0 else flux
    padded = np.pad(envelope, ONSET_PEAK_FRAMES, mode="constant", constant_values=-np.inf)
    local_max = np.lib.stride_tricks.sliding_window_view(padded, 2 * ONSET_PEAK_FRAMES + 1).max(
        axis=1
    )
    cumulative = np.concatenate([[0.0], np.cumsum(envelope)])
    lo = np.clip(np.arange(len(envelope)) - ONSET_MEAN_FRAMES, 0, None)
    hi = np.clip(np.arange(len(envelope)) + ONSET_MEAN_FRAMES + 1, None, len(envelope))
    local_mean = (cumulative[hi] - cumulative[lo]) / (hi - lo)
    candidates = np.flatnonzero(
        (envelope == local_max) & (envelope >= local_mean + ONSET_DELTA) & (envelope > 0)
    )
    onsets = np.zeros(len(envelope), dtype=bool)
    last = -math.inf
    for index in candidates.tolist():  # peaks are sparse: a few per second at most
        if index - last > ONSET_WAIT_FRAMES:
            onsets[index] = True
            last = index
    return onsets


def _checked_windows(windows: pl.DataFrame) -> pl.DataFrame:
    missing = sorted(set(WINDOW_COLUMNS) - set(windows.columns))
    if missing:
        raise ValueError(f"windows is missing columns: {', '.join(missing)}")
    nulls = [c for c in WINDOW_COLUMNS if windows[c].null_count()]
    if nulls:
        raise ValueError(f"windows has nulls in: {', '.join(nulls)}")
    if windows["video_id"].n_unique() > 1:
        raise ValueError("windows must belong to one VOD (one WAV per VOD)")
    if windows["window_id"].n_unique() != windows.height:
        raise ValueError("windows lists a window_id more than once")
    windows = windows.select(WINDOW_COLUMNS).sort("start_s")
    bad = windows.filter(
        ~(pl.col("end_s") > pl.col("start_s")) | (pl.col("start_s") < pl.col("end_s").shift(1))
    )
    if bad.height:
        raise ValueError(f"windows overlap or have no length, e.g. {bad['window_id'][0]}")
    return windows


def _vod_z(column: str) -> pl.Expr:
    x = pl.col(column)
    return _z(x, x.mean(), x.std(ddof=0))


def _rolling_z(column: str) -> pl.Expr:
    previous = pl.col(column).shift(1)
    mean = previous.rolling_mean(ROLLING_WINDOWS, min_samples=2)
    std = previous.rolling_std(ROLLING_WINDOWS, min_samples=2, ddof=0)
    return _z(pl.col(column), mean, std).fill_null(0.0)


def _z(x: pl.Expr, mean: pl.Expr, std: pl.Expr) -> pl.Expr:
    # The standard deviation is floored at `STD_FLOOR_RMS`: stretches of near-constant digital
    # audio (a muted segment, a paused game) have a tiny but non-zero spread, and dividing by it
    # would turn the next sound into a z-score in the thousands. A VOD where every window is
    # alike still scores 0 (x == mean).
    return pl.when(std.is_not_null()).then((x - mean) / pl.max_horizontal(std, STD_FLOOR_RMS))
