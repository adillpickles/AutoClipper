"""Shortlist speech features with faster-whisper (docs/ml/features.md § Phase 2, #64).

Transcribes only the top-N shortlisted candidates (default 50), each with a small amount of
padding on both sides, reusing `autoclipper.export.captions.transcriber_for` so the Whisper model
is never loaded twice. Word-timed transcripts are cached under ``<data_dir>/interim/transcripts/``
(`autoclipper.features.transcript_cache`) keyed on the VOD's audio source identity (#138) and
model name so repeated runs and caption exports reuse them without re-transcribing.

The ``captions`` extra (``faster-whisper``) remains optional: when it is not installed, speech
features are skipped with a clear warning instead of crashing.

Per-candidate features (all Float64, null for candidates outside the shortlist or skipped):
- ``speech_word_rate``: spoken words per second inside the candidate span
- ``speech_exclaim_density``: fraction of spoken words/tokens carrying ``!``
- ``speech_question_density``: fraction of spoken words/tokens carrying ``?``
- ``speech_keyword_rate``: curated spoken reaction keyword matches per second
- ``speech_frac``: share of the candidate duration covered by spoken words (``1 - speech_frac``
  is the silence share)
- ``speech_peak_loudness_align``: alignment in ``[0, 1]`` between the candidate's loudest audio
  frame and spoken speech (``1.0`` when peak loudness falls inside a spoken word, decaying
  exponentially with distance from speech, ``0.0`` when silent or speechless)
"""

import importlib.util
import logging
import math
import re
import tempfile
import wave
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import numpy as np
import numpy.typing as npt
import polars as pl
import yaml

from autoclipper.analysis import AnalysisResult, Candidate, SpeechFeatures
from autoclipper.export.captions import (
    DEFAULT_MODEL,
    CaptioningError,
    Transcriber,
    Word,
    transcriber_for,
)
from autoclipper.features.audio import (
    FRAME_LENGTH,
    HOP_LENGTH,
    SAMPLE_RATE_HZ,
    SILENCE_RMS,
    AudioFormatError,
)
from autoclipper.features.transcript_cache import (
    TranscriptCache,
    slice_words_for_interval,
)
from autoclipper.ingest.media import validate_video_id

log = logging.getLogger(__name__)

SPEECH_FEATURE_VERSION: Final = "speech-v1"
DEFAULT_SPEECH_TOP_N: Final = 50
DEFAULT_SPEECH_PAD_S: Final = 2.0
PEAK_LOUDNESS_DECAY_S: Final = 2.0
DEFAULT_KEYWORDS_PATH: Final = Path("configs/speech_keywords.yaml")

MISSING_CAPTIONS_WARNING: Final = (
    "speech features skipped because faster-whisper is not installed; "
    "install it with `uv sync --extra captions`"
)

FEATURE_COLUMNS: Final[tuple[str, ...]] = (
    "speech_word_rate",
    "speech_exclaim_density",
    "speech_question_density",
    "speech_keyword_rate",
    "speech_frac",
    "speech_peak_loudness_align",
)
"""The per-candidate speech feature columns, all Float64, in output order."""

_DEFAULT_KEYWORD_WORDS: Final[tuple[str, ...]] = (
    "no way",
    "what the",
    "oh my god",
    "omg",
    "let's go",
    "lets go",
    "clip it",
    "clip that",
    "wait what",
    "holy",
    "insane",
    "clutch",
    "bro",
    "bruh",
    "dude",
    "crazy",
    "unreal",
    "sick",
    "cooked",
    "cracked",
)


class SpeechKeywordsError(ValueError):
    """The speech reaction keywords file is missing, malformed, or invalid."""


@dataclass(frozen=True, slots=True)
class SpeechKeywords:
    """Compiled spoken reaction keyword list (``configs/speech_keywords.yaml``).

    Attributes:
        version: Positive integer version from the YAML file; part of ``feature_version``.
        words: Normalized reaction words and phrases.
        pattern: Compiled case-insensitive word-boundary regex, or None when `words` is empty.
    """

    version: int
    words: tuple[str, ...]
    pattern: re.Pattern[str] | None

    @property
    def feature_version(self) -> str:
        """``feature_version`` string for features computed with this keyword list."""
        return f"{SPEECH_FEATURE_VERSION}+keywords-v{self.version}"


@dataclass(frozen=True, slots=True)
class SpeechExtraction:
    """Extracted per-candidate speech features alongside run counters and optional warning."""

    features: pl.DataFrame
    by_candidate_id: dict[str, SpeechFeatures]
    transcribed_candidates: int
    cached_candidates: int
    empty_audio_candidates: int
    skipped_unavailable: bool
    warning: str | None
    feature_version: str


def speech_available() -> bool:
    """Return True when the optional ``captions`` runtime (``faster-whisper``) is installed."""
    return importlib.util.find_spec("faster_whisper") is not None


def _compile_keywords(words: Sequence[str]) -> re.Pattern[str] | None:
    cleaned = sorted(
        {" ".join(w.replace("\u2019", "'").split()).lower() for w in words if w.strip()},
        key=lambda term: (-len(term), term),
    )
    if not cleaned:
        return None
    parts = [r"\s+".join(re.escape(piece) for piece in term.split(" ")) for term in cleaned]
    return re.compile(rf"(?<!\w)(?:{'|'.join(parts)})(?!\w)", re.IGNORECASE)


def default_speech_keywords() -> SpeechKeywords:
    """Built-in v1 spoken reaction keyword list (mirrors ``configs/speech_keywords.yaml``)."""
    return SpeechKeywords(
        version=1,
        words=_DEFAULT_KEYWORD_WORDS,
        pattern=_compile_keywords(_DEFAULT_KEYWORD_WORDS),
    )


def load_speech_keywords(path: Path) -> SpeechKeywords:
    """Read and compile a spoken reaction keyword file such as ``configs/speech_keywords.yaml``.

    Raises:
        SpeechKeywordsError: The file can't be read or does not match the expected schema.
    """
    try:
        data: object = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise SpeechKeywordsError(f"can't read speech keywords {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise SpeechKeywordsError(f"speech keywords {path} must be a YAML mapping")
    version = data.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise SpeechKeywordsError(f"speech keywords {path}: 'version' must be a positive integer")
    raw_words = data.get("words")
    if not isinstance(raw_words, list) or not all(
        isinstance(item, str) and item.strip() for item in raw_words
    ):
        raise SpeechKeywordsError(
            f"speech keywords {path}: 'words' must be a list of non-empty strings"
        )
    normalized = tuple(" ".join(item.replace("\u2019", "'").split()) for item in raw_words)
    return SpeechKeywords(
        version=version,
        words=normalized,
        pattern=_compile_keywords(normalized),
    )


def _speech_coverage_s(words: Sequence[Word], *, start_s: float, end_s: float) -> float:
    """Merged union duration (seconds) of spoken word intervals inside ``[start_s, end_s]``."""
    intervals = sorted(
        (max(start_s, w.start_s), min(end_s, w.end_s))
        for w in words
        if w.end_s > w.start_s and w.end_s > start_s and w.start_s < end_s
    )
    if not intervals:
        return 0.0
    total = 0.0
    cur_start, cur_end = intervals[0]
    for nxt_start, nxt_end in intervals[1:]:
        if nxt_start <= cur_end:
            cur_end = max(cur_end, nxt_end)
        else:
            total += cur_end - cur_start
            cur_start, cur_end = nxt_start, nxt_end
    total += cur_end - cur_start
    return total


def _point_to_interval_distance(t: float, start_s: float, end_s: float) -> float:
    if t < start_s:
        return start_s - t
    if t > end_s:
        return t - end_s
    return 0.0


def compute_peak_loudness_alignment(
    samples: npt.NDArray[np.float32] | None,
    words: Sequence[Word],
    *,
    samples_start_s: float,
    sample_rate_hz: int = SAMPLE_RATE_HZ,
    decay_s: float = PEAK_LOUDNESS_DECAY_S,
) -> float:
    """Measure how closely a candidate's loudest audio frame aligns with spoken speech.

    Computes short-frame RMS over `samples` (64 ms frames every 32 ms at 16 kHz), finds the
    loudest frame's centre time, and returns ``exp(-d / decay_s)`` where ``d`` is the distance
    in seconds from that peak to the nearest spoken word interval (``0.0`` when the peak falls
    inside a spoken word, yielding ``1.0``). Returns ``0.0`` when the clip is silent or has no
    spoken words.
    """
    if samples is None or samples.size == 0 or not words or sample_rate_hz <= 0:
        return 0.0
    frame_len = max(1, round(FRAME_LENGTH * sample_rate_hz / SAMPLE_RATE_HZ))
    hop_len = max(1, round(HOP_LENGTH * sample_rate_hz / SAMPLE_RATE_HZ))
    if samples.size < frame_len:
        peak_rms = float(np.sqrt(np.mean(np.square(samples, dtype=np.float64))))
        if peak_rms < SILENCE_RMS:
            return 0.0
        peak_time_s = samples_start_s + samples.size / (2.0 * sample_rate_hz)
    else:
        n_frames = 1 + (samples.size - frame_len) // hop_len
        sq = np.square(samples.astype(np.float64, copy=False))
        cumsum = np.concatenate(([0.0], np.cumsum(sq)))
        starts = np.arange(n_frames) * hop_len
        rms = np.sqrt(np.maximum(0.0, (cumsum[starts + frame_len] - cumsum[starts]) / frame_len))
        peak_idx = int(np.argmax(rms))
        if float(rms[peak_idx]) < SILENCE_RMS:
            return 0.0
        peak_time_s = samples_start_s + (peak_idx * hop_len + frame_len / 2.0) / sample_rate_hz

    min_dist = min(_point_to_interval_distance(peak_time_s, w.start_s, w.end_s) for w in words)
    return float(max(0.0, min(1.0, math.exp(-min_dist / decay_s))))


def compute_candidate_speech_features(
    words: Sequence[Word],
    *,
    start_s: float,
    end_s: float,
    keywords: SpeechKeywords,
    samples: npt.NDArray[np.float32] | None = None,
    sample_rate_hz: int = SAMPLE_RATE_HZ,
    samples_start_s: float | None = None,
) -> SpeechFeatures:
    """Compute the six speech features for one candidate interval ``[start_s, end_s)``.

    Args:
        words: VOD-timed words covering at least ``[start_s, end_s]``. Words outside the
            candidate's own span are sliced away before computing rates and coverage.
        start_s: Candidate start in VOD seconds.
        end_s: Candidate end in VOD seconds.
        keywords: Compiled reaction keyword list (`load_speech_keywords`).
        samples: Optional 1-D float32 mono PCM samples in ``[-1, 1]`` for the candidate's audio,
            used to compute ``speech_peak_loudness_align``.
        sample_rate_hz: Sampling rate of `samples` (default 16 kHz).
        samples_start_s: VOD timestamp of ``samples[0]`` (defaults to `start_s`).
    """
    if not (math.isfinite(start_s) and math.isfinite(end_s) and 0.0 <= start_s < end_s):
        raise ValueError(
            f"candidate span must satisfy 0 <= start_s < end_s, got [{start_s!r}, {end_s!r}]"
        )
    duration_s = end_s - start_s
    inside = slice_words_for_interval(words, start_s, end_s, clip_relative=False)

    n_words = sum(1 for w in inside if any(ch.isalnum() for ch in w.text))
    word_rate = n_words / duration_s
    if n_words > 0:
        n_exclaim = sum(1 for w in inside if "!" in w.text)
        n_question = sum(1 for w in inside if "?" in w.text)
        exclaim_density = min(1.0, n_exclaim / n_words)
        question_density = min(1.0, n_question / n_words)
    else:
        exclaim_density = 0.0
        question_density = 0.0

    if keywords.pattern is not None and inside:
        transcript_text = " ".join(w.text for w in inside).replace("\u2019", "'")
        n_keyword_matches = sum(1 for _ in keywords.pattern.finditer(transcript_text))
        keyword_rate = n_keyword_matches / duration_s
    else:
        keyword_rate = 0.0

    speech_s = _speech_coverage_s(inside, start_s=start_s, end_s=end_s)
    speech_frac = max(0.0, min(1.0, speech_s / duration_s))

    align = compute_peak_loudness_alignment(
        samples,
        inside,
        samples_start_s=start_s if samples_start_s is None else samples_start_s,
        sample_rate_hz=sample_rate_hz,
    )
    return SpeechFeatures(
        speech_word_rate=float(word_rate),
        speech_exclaim_density=float(exclaim_density),
        speech_question_density=float(question_density),
        speech_keyword_rate=float(keyword_rate),
        speech_frac=float(speech_frac),
        speech_peak_loudness_align=float(align),
    )


def _write_slice_wav(path: Path, raw_pcm: bytes) -> Path:
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(SAMPLE_RATE_HZ)
        out.writeframes(raw_pcm)
    return path


def _read_pcm(reader: wave.Wave_read, wav_path: Path, start_sample: int, end_sample: int) -> bytes:
    n_read = end_sample - start_sample
    if n_read <= 0:
        return b""
    reader.setpos(start_sample)
    raw = reader.readframes(n_read)
    if len(raw) != 2 * n_read:
        raise AudioFormatError(f"{wav_path} ended early: its header promised more audio")
    return raw


def _empty_features_frame() -> pl.DataFrame:
    return pl.DataFrame(
        schema={
            "candidate_id": pl.String,
            "video_id": pl.String,
            "start_s": pl.Float64,
            "end_s": pl.Float64,
            **dict.fromkeys(FEATURE_COLUMNS, pl.Float64),
            "feature_version": pl.String,
        }
    )


def extract_speech_features(
    candidates: Sequence[Candidate],
    wav_path: Path,
    *,
    video_id: str,
    vod_duration_s: float | None = None,
    top_n: int = DEFAULT_SPEECH_TOP_N,
    pad_s: float = DEFAULT_SPEECH_PAD_S,
    model_name: str = DEFAULT_MODEL,
    keywords: SpeechKeywords | None = None,
    transcriber: Transcriber | None = None,
    cache: TranscriptCache | None = None,
    data_dir: Path | None = None,
    on_progress: Callable[[int, int], None] | None = None,
) -> SpeechExtraction:
    """Transcribe only the top-`top_n` candidates and compute per-candidate speech features.

    Args:
        candidates: Ranked candidates (e.g. ``AnalysisResult.candidates``). Only the first
            `top_n` candidates are transcribed; remaining candidates stay in the returned
            DataFrame with ``null`` speech feature values.
        wav_path: Normalized 16 kHz mono 16-bit WAV (``data/interim/audio/<video_id>.wav``).
        video_id: Twitch video ID (used for cache keying and the output table).
        vod_duration_s: Optional VOD duration in seconds to clamp padded spans against.
        top_n: Maximum number of top-ranked candidates to transcribe (default 50).
        pad_s: Padding seconds added on each side of each candidate when transcribing (default
            2.0 s). Padding gives Whisper clean boundary context and widens cache coverage for
            trimmed caption exports; features are still computed strictly on the candidate span.
        model_name: faster-whisper model size/name (default ``base``).
        keywords: Compiled reaction keyword list; defaults to `default_speech_keywords()`.
        transcriber: Optional `Transcriber` (tests pass a fake). Defaults to the shared
            `transcriber_for(model_name)` from `autoclipper.export.captions`.
        cache: Optional explicit `TranscriptCache`. When None and `data_dir` is provided, a
            cache keyed on the VOD's audio source identity (#138) and `model_name` is opened
            under ``<data_dir>/interim/transcripts/<video_id>/``.
        data_dir: Data directory used to open the default `TranscriptCache` when `cache` is None.
        on_progress: Optional callback ``(done, total_shortlist)`` called after each shortlisted
            candidate is processed.

    Returns:
        A `SpeechExtraction` with the Polars table, a `by_candidate_id` dict of `SpeechFeatures`,
        counts of transcribed/cached/empty candidates, and a warning if `faster-whisper` was
        unavailable.

    Raises:
        ValueError: Invalid `video_id`, `top_n`, `pad_s`, or `model_name`.
        AudioFormatError: `wav_path` is missing, unreadable, truncated or not 16 kHz mono 16-bit.
    """
    vid = validate_video_id(video_id)
    if isinstance(top_n, bool) or top_n < 1:
        raise ValueError(f"top_n must be an integer >= 1, got {top_n!r}")
    if isinstance(pad_s, bool) or not math.isfinite(pad_s) or pad_s < 0:
        raise ValueError(f"pad_s must be finite and >= 0, got {pad_s!r}")
    if not model_name.strip():
        raise ValueError("model_name must be non-empty")

    kw = keywords if keywords is not None else default_speech_keywords()
    if not candidates:
        return SpeechExtraction(
            features=_empty_features_frame(),
            by_candidate_id={},
            transcribed_candidates=0,
            cached_candidates=0,
            empty_audio_candidates=0,
            skipped_unavailable=False,
            warning=None,
            feature_version=kw.feature_version,
        )

    try:
        reader = wave.open(str(wav_path), "rb")  # noqa: SIM115 (closed via context manager below)
    except (OSError, wave.Error, EOFError) as exc:
        hint = " (RF64 WAVs over 4 GiB aren't supported)" if "RIFF" in str(exc) else ""
        raise AudioFormatError(f"can't read WAV {wav_path}: {exc}{hint}") from exc

    active_cache = cache
    if active_cache is None and data_dir is not None:
        active_cache = TranscriptCache.for_vod(
            data_dir, vid, model_name=model_name, wav_path=wav_path
        )

    shortlist_ids = {c.candidate_id for c in candidates[:top_n]}
    total_shortlist = len(shortlist_ids)

    rows_by_col: dict[str, list[float | None]] = {col: [] for col in FEATURE_COLUMNS}
    by_candidate_id: dict[str, SpeechFeatures] = {}
    transcribed_count = 0
    cached_count = 0
    empty_audio_count = 0
    skipped_unavailable = False
    warning: str | None = None
    processed = 0
    active_transcriber = transcriber

    with reader, tempfile.TemporaryDirectory(prefix="autoclipper-speech-") as tmp_dir:
        layout = (reader.getframerate(), reader.getnchannels(), reader.getsampwidth())
        if layout != (SAMPLE_RATE_HZ, 1, 2):
            raise AudioFormatError(
                f"{wav_path} is {layout[0]} Hz, {layout[1]} channel(s), {8 * layout[2]}-bit; "
                f"expected {SAMPLE_RATE_HZ} Hz mono 16-bit (run normalize_audio first)"
            )
        total_frames = reader.getnframes()
        wav_duration_s = total_frames / SAMPLE_RATE_HZ
        upper_bound_s = (
            min(wav_duration_s, float(vod_duration_s))
            if vod_duration_s is not None
            else wav_duration_s
        )
        slice_wav = Path(tmp_dir) / "candidate.wav"

        for candidate in candidates:
            if candidate.candidate_id not in shortlist_ids:
                for col in FEATURE_COLUMNS:
                    rows_by_col[col].append(None)
                continue

            c_start_sample = max(0, round(candidate.start_s * SAMPLE_RATE_HZ))
            c_end_sample = min(total_frames, round(candidate.end_s * SAMPLE_RATE_HZ))
            if c_start_sample >= c_end_sample:
                empty_audio_count += 1
                sf = SpeechFeatures(
                    speech_word_rate=0.0,
                    speech_exclaim_density=0.0,
                    speech_question_density=0.0,
                    speech_keyword_rate=0.0,
                    speech_frac=0.0,
                    speech_peak_loudness_align=0.0,
                )
                by_candidate_id[candidate.candidate_id] = sf
                for col in FEATURE_COLUMNS:
                    rows_by_col[col].append(float(getattr(sf, col)))
                processed += 1
                if on_progress is not None:
                    on_progress(processed, total_shortlist)
                continue

            vod_words: list[Word] | None = (
                active_cache.get_words(candidate.start_s, candidate.end_s)
                if active_cache is not None
                else None
            )
            cand_samples: npt.NDArray[np.float32] | None = None
            cand_samples_start_s = c_start_sample / SAMPLE_RATE_HZ

            if vod_words is not None:
                cached_count += 1
                raw_cand = _read_pcm(reader, wav_path, c_start_sample, c_end_sample)
                cand_samples = np.frombuffer(raw_cand, dtype="<i2").astype(np.float32) / 32768.0
            else:
                if skipped_unavailable:
                    for col in FEATURE_COLUMNS:
                        rows_by_col[col].append(None)
                    processed += 1
                    if on_progress is not None:
                        on_progress(processed, total_shortlist)
                    continue

                if active_transcriber is None:
                    if not speech_available():
                        skipped_unavailable = True
                        warning = MISSING_CAPTIONS_WARNING
                        log.warning(
                            "speech features skipped video_id=%s reason=missing_captions_extra",
                            vid,
                        )
                        for col in FEATURE_COLUMNS:
                            rows_by_col[col].append(None)
                        processed += 1
                        if on_progress is not None:
                            on_progress(processed, total_shortlist)
                        continue
                    active_transcriber = transcriber_for(model_name)

                pad_start_s = max(0.0, candidate.start_s - pad_s)
                pad_end_s = min(upper_bound_s, candidate.end_s + pad_s)
                pad_start_sample = max(0, round(pad_start_s * SAMPLE_RATE_HZ))
                pad_end_sample = min(total_frames, round(pad_end_s * SAMPLE_RATE_HZ))
                if pad_end_sample <= pad_start_sample:
                    pad_start_sample, pad_end_sample = c_start_sample, c_end_sample
                actual_pad_start_s = pad_start_sample / SAMPLE_RATE_HZ
                actual_pad_end_s = pad_end_sample / SAMPLE_RATE_HZ

                raw_padded = _read_pcm(reader, wav_path, pad_start_sample, pad_end_sample)
                _write_slice_wav(slice_wav, raw_padded)
                try:
                    local_words = active_transcriber(slice_wav)
                except CaptioningError as exc:
                    skipped_unavailable = True
                    warning = f"speech features skipped: {exc}"
                    log.warning("speech features skipped video_id=%s error=%s", vid, exc)
                    for col in FEATURE_COLUMNS:
                        rows_by_col[col].append(None)
                    processed += 1
                    if on_progress is not None:
                        on_progress(processed, total_shortlist)
                    continue

                vod_words = [
                    Word(
                        text=w.text.strip(),
                        start_s=actual_pad_start_s + max(0.0, float(w.start_s)),
                        end_s=actual_pad_start_s + max(max(0.0, float(w.start_s)), float(w.end_s)),
                    )
                    for w in local_words
                    if w.text.strip()
                    and math.isfinite(w.start_s)
                    and math.isfinite(w.end_s)
                    and w.end_s >= w.start_s
                ]
                if active_cache is not None:
                    active_cache.put_span(actual_pad_start_s, actual_pad_end_s, vod_words)
                transcribed_count += 1

                # Reuse the already-read padded PCM buffer for candidate loudness alignment.
                rel_start = max(0, c_start_sample - pad_start_sample)
                rel_end = min(pad_end_sample - pad_start_sample, c_end_sample - pad_start_sample)
                padded_arr = np.frombuffer(raw_padded, dtype="<i2").astype(np.float32) / 32768.0
                cand_samples = padded_arr[rel_start:rel_end]
                cand_samples_start_s = (pad_start_sample + rel_start) / SAMPLE_RATE_HZ

            sf = compute_candidate_speech_features(
                vod_words,
                start_s=candidate.start_s,
                end_s=candidate.end_s,
                keywords=kw,
                samples=cand_samples,
                sample_rate_hz=SAMPLE_RATE_HZ,
                samples_start_s=cand_samples_start_s,
            )
            by_candidate_id[candidate.candidate_id] = sf
            for col in FEATURE_COLUMNS:
                rows_by_col[col].append(float(getattr(sf, col)))

            processed += 1
            if on_progress is not None:
                on_progress(processed, total_shortlist)

    if empty_audio_count:
        log.warning(
            "speech feature candidates without audio video_id=%s n=%d of=%d path=%s",
            vid,
            empty_audio_count,
            total_shortlist,
            wav_path,
        )
    log.info(
        "speech features video_id=%s shortlist=%d transcribed=%d cached=%d empty_audio=%d "
        "skipped_unavailable=%s model=%s",
        vid,
        total_shortlist,
        transcribed_count,
        cached_count,
        empty_audio_count,
        skipped_unavailable,
        model_name,
    )

    frame = pl.DataFrame(
        {
            "candidate_id": pl.Series(
                "candidate_id", [c.candidate_id for c in candidates], dtype=pl.String
            ),
            "video_id": pl.Series("video_id", [vid] * len(candidates), dtype=pl.String),
            "start_s": pl.Series("start_s", [c.start_s for c in candidates], dtype=pl.Float64),
            "end_s": pl.Series("end_s", [c.end_s for c in candidates], dtype=pl.Float64),
            **{
                col: pl.Series(col, values, dtype=pl.Float64) for col, values in rows_by_col.items()
            },
            "feature_version": pl.Series(
                "feature_version", [kw.feature_version] * len(candidates), dtype=pl.String
            ),
        }
    )
    return SpeechExtraction(
        features=frame,
        by_candidate_id=by_candidate_id,
        transcribed_candidates=transcribed_count,
        cached_candidates=cached_count,
        empty_audio_candidates=empty_audio_count,
        skipped_unavailable=skipped_unavailable,
        warning=warning,
        feature_version=kw.feature_version,
    )


def attach_speech_features(
    result: AnalysisResult,
    by_candidate_id: Mapping[str, SpeechFeatures],
) -> AnalysisResult:
    """Return a copy of `result` with computed `SpeechFeatures` attached to matching candidates."""
    if not by_candidate_id:
        return result
    updated = tuple(
        c.model_copy(update={"speech_features": by_candidate_id[c.candidate_id]})
        if c.candidate_id in by_candidate_id
        else c
        for c in result.candidates
    )
    return result.model_copy(update={"candidates": updated})


__all__ = [
    "DEFAULT_KEYWORDS_PATH",
    "DEFAULT_SPEECH_PAD_S",
    "DEFAULT_SPEECH_TOP_N",
    "FEATURE_COLUMNS",
    "MISSING_CAPTIONS_WARNING",
    "PEAK_LOUDNESS_DECAY_S",
    "SPEECH_FEATURE_VERSION",
    "SpeechExtraction",
    "SpeechKeywords",
    "SpeechKeywordsError",
    "attach_speech_features",
    "compute_candidate_speech_features",
    "compute_peak_loudness_alignment",
    "default_speech_keywords",
    "extract_speech_features",
    "load_speech_keywords",
    "speech_available",
]
