"""Per-VOD, per-span transcript cache keyed on audio source identity and Whisper model (#64).

Shortlist speech features (`autoclipper.features.speech`) transcribe only the top-N candidates
(each with a small amount of padding). Caching those word-timed transcripts under
``<data_dir>/interim/transcripts/<video_id>/`` lets:

1. Repeated speech-feature runs skip re-transcribing already cached candidate spans.
2. Caption export (`autoclipper.export.captions`) reuse cached words whenever a clip's interval
   is covered by a cached span, saving a second Whisper transcription.
3. Downstream rerankers and explanation modules (#65) read the same word-timed transcripts.

Cache entries are keyed by both the Whisper ``model_name`` and the audio source identity:
- When ``<video_id>.source.json`` exists next to the normalized WAV (#138, written for local
  media files such as ``--video`` or ``ingest --media``), its identity dictionary is used
  directly. Because caption export computes the identical identity for the local media file,
  exports against the same file match automatically, and editing or swapping the file invalidates
  the cache.
- Otherwise (downloaded Twitch audio or standalone WAVs without a sidecar), the WAV file's own
  path, size, mtime and 3x64 KiB sampled SHA-256 fingerprint identify the audio source, so
  switching between downloaded audio and a local file (or replacing the WAV) invalidates cached
  transcripts.
"""

import contextlib
import hashlib
import json
import logging
import math
import re
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from autoclipper.export.captions import Word
from autoclipper.ingest.media import normalized_audio_path, validate_video_id
from autoclipper.ingest.vod import audio_source_path

log = logging.getLogger(__name__)

CACHE_SCHEMA_VERSION: Final = 1
SPAN_TOLERANCE_S: Final = 1e-3
# Matches `autoclipper.ingest.vod._AUDIO_FINGERPRINT_CHUNK_BYTES` so local-file identities equal
# `<video_id>.source.json` byte-for-byte.
_FINGERPRINT_CHUNK_BYTES: Final = 64 * 1024
_SAFE_MODEL_RE: Final = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


@dataclass(frozen=True, slots=True)
class CachedTranscriptSpan:
    """One cached transcript span in VOD seconds.

    Attributes:
        start_s: Inclusive start of the transcribed span, seconds from VOD start.
        end_s: Exclusive end of the transcribed span, seconds from VOD start.
        words: Spoken words with timestamps in VOD seconds, sorted in time order.
    """

    start_s: float
    end_s: float
    words: tuple[Word, ...]

    def covers(
        self, start_s: float, end_s: float, *, tolerance_s: float = SPAN_TOLERANCE_S
    ) -> bool:
        """Return True when this span covers ``[start_s, end_s]`` within `tolerance_s`."""
        return self.start_s <= start_s + tolerance_s and self.end_s >= end_s - tolerance_s


def transcripts_root(data_dir: Path) -> Path:
    """Return ``<data_dir>/interim/transcripts``."""
    return data_dir / "interim" / "transcripts"


def vod_transcripts_dir(data_dir: Path, video_id: str) -> Path:
    """Return ``<data_dir>/interim/transcripts/<video_id>``."""
    return transcripts_root(data_dir) / validate_video_id(video_id)


def _model_slug(model_name: str) -> str:
    """Turn a Whisper model name or path into a collision-resistant filename prefix."""
    cleaned = model_name.strip()
    if not cleaned:
        raise ValueError("model_name must be non-empty")
    if _SAFE_MODEL_RE.fullmatch(cleaned) and cleaned not in (".", ".."):
        return cleaned
    sanitized = re.sub(r"[^A-Za-z0-9._-]+", "_", cleaned).strip("._-")[:40] or "model"
    suffix = hashlib.sha256(cleaned.encode("utf-8")).hexdigest()[:8]
    return f"{sanitized}-{suffix}"


def file_source_identity(path: Path, *, kind: str = "local_file") -> dict[str, object]:
    """Identify a media or WAV file by resolved path, stat metadata and sampled SHA-256.

    When ``kind == "local_file"``, this produces the exact same dictionary shape that
    ``autoclipper.ingest.vod`` writes to ``<video_id>.source.json`` (#138).
    """
    resolved = path.resolve()
    stat = resolved.stat()
    size = stat.st_size
    digest = hashlib.sha256()
    with resolved.open("rb") as handle:
        if size <= 3 * _FINGERPRINT_CHUNK_BYTES:
            digest.update(handle.read())
        else:
            digest.update(handle.read(_FINGERPRINT_CHUNK_BYTES))
            handle.seek((size - _FINGERPRINT_CHUNK_BYTES) // 2)
            digest.update(handle.read(_FINGERPRINT_CHUNK_BYTES))
            handle.seek(size - _FINGERPRINT_CHUNK_BYTES)
            digest.update(handle.read(_FINGERPRINT_CHUNK_BYTES))
    return {
        "kind": kind,
        "path": str(resolved),
        "size": size,
        "mtime_ns": stat.st_mtime_ns,
        "sample_sha256": digest.hexdigest(),
    }


def resolve_vod_audio_identity(
    data_dir: Path,
    video_id: str,
    *,
    wav_path: Path | None = None,
) -> dict[str, object]:
    """Resolve the active audio source identity for `video_id`.

    Prefers ``<video_id>.source.json`` next to the WAV (written by ingest #138 for local media
    files); otherwise fingerprints the normalized WAV itself so any WAV replacement or switch
    between downloaded and local audio invalidates the cache key.

    Raises:
        FileNotFoundError: Neither a readable sidecar nor the WAV file exists.
    """
    validate_video_id(video_id)
    wav = wav_path if wav_path is not None else normalized_audio_path(data_dir, video_id)
    sidecar = (
        wav.with_suffix(".source.json")
        if wav_path is not None
        else audio_source_path(data_dir, video_id)
    )
    if sidecar.is_file():
        try:
            loaded: object = json.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning(
                "audio source sidecar unreadable video_id=%s path=%s error=%s",
                video_id,
                sidecar,
                exc,
            )
        else:
            if isinstance(loaded, dict) and loaded:
                return dict(loaded)
            log.warning(
                "audio source sidecar malformed video_id=%s path=%s",
                video_id,
                sidecar,
            )
    if not wav.is_file():
        raise FileNotFoundError(f"normalized audio WAV not found: {wav}")
    return file_source_identity(wav, kind="wav_file")


def compute_source_key(identity: Mapping[str, object]) -> str:
    """Deterministic SHA-256 hex key for an audio source identity mapping."""
    payload = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def cache_file_path(data_dir: Path, video_id: str, model_name: str, source_key: str) -> Path:
    """Return the JSON cache file path for one ``(video_id, model_name, source_key)``."""
    return (
        vod_transcripts_dir(data_dir, video_id)
        / f"{_model_slug(model_name)}_{source_key[:16]}.json"
    )


def _clean_words(words: Sequence[Word], *, start_s: float, end_s: float) -> tuple[Word, ...]:
    cleaned: list[Word] = []
    for word in words:
        text = word.text.strip()
        if not text:
            continue
        if not (math.isfinite(word.start_s) and math.isfinite(word.end_s)):
            continue
        if word.end_s < word.start_s:
            continue
        w_start = max(start_s, min(end_s, float(word.start_s)))
        w_end = max(w_start, min(end_s, float(word.end_s)))
        cleaned.append(Word(text=text, start_s=w_start, end_s=w_end))
    cleaned.sort(key=lambda w: (w.start_s, w.end_s))
    return tuple(cleaned)


def slice_words_for_interval(
    words: Sequence[Word],
    start_s: float,
    end_s: float,
    *,
    clip_relative: bool = False,
) -> list[Word]:
    """Slice VOD-timed `words` to ``[start_s, end_s]`` and optionally shift to clip-local time.

    A word belongs to the interval when its midpoint lies inside ``[start_s, end_s]`` (using
    strict upper bound except at exact `end_s` when zero-length), so adjacent clips never
    duplicate a boundary word. Selected word boundaries are clamped to ``[start_s, end_s]``.
    """
    if not (math.isfinite(start_s) and math.isfinite(end_s) and end_s > start_s):
        raise ValueError(f"invalid interval [{start_s!r}, {end_s!r}]")
    duration_s = end_s - start_s
    selected: list[Word] = []
    for word in words:
        text = word.text.strip()
        if not text:
            continue
        if not (math.isfinite(word.start_s) and math.isfinite(word.end_s)):
            continue
        if word.end_s < word.start_s:
            continue
        mid = 0.5 * (word.start_s + word.end_s)
        if not (start_s <= mid <= end_s):
            continue
        if mid == end_s and word.start_s >= end_s:
            continue
        clipped_start = max(start_s, min(end_s, float(word.start_s)))
        clipped_end = max(clipped_start, min(end_s, float(word.end_s)))
        if clip_relative:
            local_start = max(0.0, min(duration_s, clipped_start - start_s))
            local_end = max(local_start, min(duration_s, clipped_end - start_s))
            selected.append(Word(text=text, start_s=local_start, end_s=local_end))
        else:
            selected.append(Word(text=text, start_s=clipped_start, end_s=clipped_end))
    selected.sort(key=lambda w: (w.start_s, w.end_s))
    return selected


class TranscriptCache:
    """Read/write per-VOD transcript span cache keyed by audio source identity and model name.

    Attributes:
        path: JSON file backing this cache entry.
        video_id: Twitch video ID.
        model_name: faster-whisper model size/name.
        source_identity: Audio source identity dictionary.
        source_key: SHA-256 hex digest of `source_identity`.
    """

    def __init__(
        self,
        *,
        data_dir: Path,
        video_id: str,
        model_name: str,
        source_identity: Mapping[str, object],
    ) -> None:
        self.video_id = validate_video_id(video_id)
        if not model_name.strip():
            raise ValueError("model_name must be non-empty")
        self.model_name = model_name
        self.source_identity: dict[str, object] = dict(source_identity)
        self.source_key = compute_source_key(self.source_identity)
        self.path = cache_file_path(data_dir, self.video_id, self.model_name, self.source_key)
        self._loaded_stat: tuple[int, int] | None = None
        self._spans: list[CachedTranscriptSpan] = []

    @classmethod
    def for_vod(
        cls,
        data_dir: Path,
        video_id: str,
        *,
        model_name: str,
        wav_path: Path | None = None,
        source_identity: Mapping[str, object] | None = None,
    ) -> "TranscriptCache":
        """Build a `TranscriptCache` using the VOD's active audio source identity."""
        identity = (
            dict(source_identity)
            if source_identity is not None
            else resolve_vod_audio_identity(data_dir, video_id, wav_path=wav_path)
        )
        return cls(
            data_dir=data_dir,
            video_id=video_id,
            model_name=model_name,
            source_identity=identity,
        )

    @classmethod
    def for_media(
        cls,
        data_dir: Path,
        video_id: str,
        media_path: Path,
        *,
        model_name: str,
    ) -> "TranscriptCache":
        """Build a `TranscriptCache` keyed on a local media file's identity (matching #138)."""
        return cls(
            data_dir=data_dir,
            video_id=video_id,
            model_name=model_name,
            source_identity=file_source_identity(media_path, kind="local_file"),
        )

    def spans(self) -> tuple[CachedTranscriptSpan, ...]:
        """Return all cached spans for this VOD, model and audio source, sorted by time."""
        self._refresh()
        return tuple(self._spans)

    def get_span(
        self,
        start_s: float,
        end_s: float,
        *,
        tolerance_s: float = SPAN_TOLERANCE_S,
    ) -> CachedTranscriptSpan | None:
        """Return the tightest cached span covering ``[start_s, end_s]``, or None on a miss."""
        if not (math.isfinite(start_s) and math.isfinite(end_s) and end_s > start_s):
            return None
        self._refresh()
        covering = [
            span for span in self._spans if span.covers(start_s, end_s, tolerance_s=tolerance_s)
        ]
        if not covering:
            return None
        return min(covering, key=lambda s: (s.end_s - s.start_s, s.start_s))

    def get_words(
        self,
        start_s: float,
        end_s: float,
        *,
        tolerance_s: float = SPAN_TOLERANCE_S,
    ) -> list[Word] | None:
        """Return VOD-timed words for ``[start_s, end_s]`` if covered by cache, else None."""
        span = self.get_span(start_s, end_s, tolerance_s=tolerance_s)
        if span is None:
            return None
        return slice_words_for_interval(span.words, start_s, end_s, clip_relative=False)

    def get_clip_words(
        self,
        start_s: float,
        end_s: float,
        *,
        tolerance_s: float = SPAN_TOLERANCE_S,
    ) -> list[Word] | None:
        """Return clip-relative words (0..duration_s) for ``[start_s, end_s]``, else None."""
        span = self.get_span(start_s, end_s, tolerance_s=tolerance_s)
        if span is None:
            return None
        return slice_words_for_interval(span.words, start_s, end_s, clip_relative=True)

    def put_span(
        self,
        start_s: float,
        end_s: float,
        words: Sequence[Word],
    ) -> CachedTranscriptSpan:
        """Persist one transcribed VOD span atomically and return the stored span."""
        if not (math.isfinite(start_s) and math.isfinite(end_s) and 0.0 <= start_s < end_s):
            raise ValueError(
                f"span must satisfy 0 <= start_s < end_s, got [{start_s!r}, {end_s!r}]"
            )
        self._refresh()
        new_span = CachedTranscriptSpan(
            start_s=float(start_s),
            end_s=float(end_s),
            words=_clean_words(words, start_s=float(start_s), end_s=float(end_s)),
        )
        # Drop any older span fully subsumed by the new span so repeated runs don't accumulate
        # duplicate entries.
        kept = [
            existing
            for existing in self._spans
            if not new_span.covers(existing.start_s, existing.end_s)
        ]
        kept.append(new_span)
        kept.sort(key=lambda s: (s.start_s, s.end_s))
        self._spans = kept
        self._write()
        return new_span

    def _refresh(self) -> None:
        try:
            stat = self.path.stat()
        except FileNotFoundError:
            self._loaded_stat = None
            self._spans = []
            return
        except OSError as exc:
            log.warning("transcript cache unreadable path=%s error=%s", self.path, exc)
            self._loaded_stat = None
            self._spans = []
            return
        signature = (stat.st_mtime_ns, stat.st_size)
        if self._loaded_stat == signature:
            return
        self._spans = _load_cache_file(
            self.path,
            expected_video_id=self.video_id,
            expected_model_name=self.model_name,
            expected_source_key=self.source_key,
            expected_source_identity=self.source_identity,
        )
        self._loaded_stat = signature

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": CACHE_SCHEMA_VERSION,
            "video_id": self.video_id,
            "model_name": self.model_name,
            "source_key": self.source_key,
            "source_identity": self.source_identity,
            "spans": [
                {
                    "start_s": span.start_s,
                    "end_s": span.end_s,
                    "words": [
                        {"text": w.text, "start_s": w.start_s, "end_s": w.end_s} for w in span.words
                    ],
                }
                for span in self._spans
            ],
        }
        tmp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="\n",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as tmp:
                tmp_path = Path(tmp.name)
                tmp.write(json.dumps(payload, indent=2) + "\n")
            tmp_path.replace(self.path)
            tmp_path = None
            stat = self.path.stat()
            self._loaded_stat = (stat.st_mtime_ns, stat.st_size)
        finally:
            if tmp_path is not None:
                with contextlib.suppress(OSError):
                    tmp_path.unlink(missing_ok=True)


def _load_cache_file(
    path: Path,
    *,
    expected_video_id: str,
    expected_model_name: str,
    expected_source_key: str,
    expected_source_identity: Mapping[str, object],
) -> list[CachedTranscriptSpan]:
    try:
        raw: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("transcript cache corrupt path=%s error=%s", path, exc)
        return []
    if not isinstance(raw, dict):
        log.warning("transcript cache malformed path=%s", path)
        return []
    if (
        raw.get("schema_version") != CACHE_SCHEMA_VERSION
        or raw.get("video_id") != expected_video_id
        or raw.get("model_name") != expected_model_name
        or raw.get("source_key") != expected_source_key
        or raw.get("source_identity") != expected_source_identity
    ):
        log.info("transcript cache metadata mismatch path=%s; ignoring", path)
        return []
    raw_spans = raw.get("spans")
    if not isinstance(raw_spans, list):
        return []
    parsed: list[CachedTranscriptSpan] = []
    for item in raw_spans:
        if not isinstance(item, dict):
            continue
        start_s = item.get("start_s")
        end_s = item.get("end_s")
        raw_words = item.get("words")
        if (
            isinstance(start_s, bool)
            or isinstance(end_s, bool)
            or not isinstance(start_s, (int, float))
            or not isinstance(end_s, (int, float))
            or not isinstance(raw_words, list)
        ):
            continue
        s_val = float(start_s)
        e_val = float(end_s)
        if not (math.isfinite(s_val) and math.isfinite(e_val) and 0.0 <= s_val < e_val):
            continue
        words: list[Word] = []
        for entry in raw_words:
            if not isinstance(entry, dict):
                continue
            text = entry.get("text")
            ws = entry.get("start_s")
            we = entry.get("end_s")
            if (
                isinstance(text, str)
                and not isinstance(ws, bool)
                and not isinstance(we, bool)
                and isinstance(ws, (int, float))
                and isinstance(we, (int, float))
            ):
                words.append(Word(text=text, start_s=float(ws), end_s=float(we)))
        parsed.append(
            CachedTranscriptSpan(
                start_s=s_val,
                end_s=e_val,
                words=_clean_words(words, start_s=s_val, end_s=e_val),
            )
        )
    parsed.sort(key=lambda s: (s.start_s, s.end_s))
    return parsed


def find_cache_for_media(
    data_dir: Path,
    media_path: Path,
    *,
    model_name: str,
    video_id: str | None = None,
) -> TranscriptCache | None:
    """Find an existing `TranscriptCache` matching `media_path` and `model_name`, if any.

    Used by caption export (`autoclipper.export.captions`) to reuse shortlist transcripts without
    requiring the caller to pass a `video_id`. Returns None when no matching cache file exists.
    """
    root = transcripts_root(data_dir)
    if not root.is_dir() or not media_path.is_file():
        return None
    identity = file_source_identity(media_path, kind="local_file")
    source_key = compute_source_key(identity)
    filename = f"{_model_slug(model_name)}_{source_key[:16]}.json"

    candidates: list[tuple[str, Path]] = []
    if video_id is not None:
        vid = validate_video_id(video_id)
        candidate_path = root / vid / filename
        if candidate_path.is_file():
            candidates.append((vid, candidate_path))
    else:
        for vod_dir in sorted(root.iterdir()):
            if not vod_dir.is_dir():
                continue
            candidate_path = vod_dir / filename
            if candidate_path.is_file():
                candidates.append((vod_dir.name, candidate_path))

    for vid, path in candidates:
        try:
            validated_vid = validate_video_id(vid)
        except ValueError:
            continue
        spans = _load_cache_file(
            path,
            expected_video_id=validated_vid,
            expected_model_name=model_name,
            expected_source_key=source_key,
            expected_source_identity=identity,
        )
        if spans:
            return TranscriptCache(
                data_dir=data_dir,
                video_id=validated_vid,
                model_name=model_name,
                source_identity=identity,
            )
    return None


__all__ = [
    "CACHE_SCHEMA_VERSION",
    "SPAN_TOLERANCE_S",
    "CachedTranscriptSpan",
    "TranscriptCache",
    "cache_file_path",
    "compute_source_key",
    "file_source_identity",
    "find_cache_for_media",
    "resolve_vod_audio_identity",
    "slice_words_for_interval",
    "transcripts_root",
    "vod_transcripts_dir",
]
