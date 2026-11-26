"""Tests for the per-VOD, per-span transcript cache (#64)."""

import json
from pathlib import Path

import pytest

from autoclipper.export.captions import Word
from autoclipper.features.transcript_cache import (
    TranscriptCache,
    compute_source_key,
    file_source_identity,
    find_cache_for_media,
    resolve_vod_audio_identity,
    slice_words_for_interval,
)
from autoclipper.ingest.media import normalized_audio_path
from autoclipper.ingest.vod import _local_file_identity, audio_source_path


def _write_bytes(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def test_file_source_identity_matches_ingest_local_file_identity(tmp_path: Path) -> None:
    small = _write_bytes(tmp_path / "small.mp4", b"hello-vod-bytes" * 100)
    large = _write_bytes(tmp_path / "large.mp4", b"A" * 80_000 + b"B" * 80_000 + b"C" * 80_000)

    assert file_source_identity(small, kind="local_file") == _local_file_identity(small)
    assert file_source_identity(large, kind="local_file") == _local_file_identity(large)


def test_cache_persists_spans_and_reuses_tightest_covering_span(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    wav = _write_bytes(normalized_audio_path(data_dir, "9000000001"), b"fake-wav-content")

    cache = TranscriptCache.for_vod(data_dir, "9000000001", model_name="base", wav_path=wav)
    assert cache.get_span(10.0, 40.0) is None

    # Store a wide padded span [8.0, 42.0] and a narrower span [15.0, 25.0] inside a separate run
    # (note: adding a wider span after a narrower span compacts the narrower one; adding narrower
    # after wider keeps both and selects the tightest covering span).
    cache.put_span(
        8.0,
        42.0,
        [
            Word("before", 8.5, 9.2),
            Word("No", 10.2, 10.5),
            Word("way!", 10.6, 11.1),
            Word("after", 40.5, 41.2),
        ],
    )
    cache.put_span(
        9.5,
        35.0,
        [
            Word("No", 10.2, 10.5),
            Word("way!", 10.6, 11.1),
        ],
    )

    reloaded = TranscriptCache.for_vod(data_dir, "9000000001", model_name="base", wav_path=wav)
    span = reloaded.get_span(10.0, 30.0)
    assert span is not None
    assert (span.start_s, span.end_s) == (9.5, 35.0)

    # Query extending to 40 s falls back to the wider span [8.0, 42.0].
    wider = reloaded.get_span(10.0, 40.0)
    assert wider is not None
    assert (wider.start_s, wider.end_s) == (8.0, 42.0)

    # Clip-relative words shift by start_s and exclude words outside [10.0, 40.0].
    clip_words = reloaded.get_clip_words(10.0, 40.0)
    assert clip_words is not None
    assert [w.text for w in clip_words] == ["No", "way!"]
    assert clip_words[0].start_s == pytest.approx(0.2)
    assert clip_words[1].end_s == pytest.approx(1.1)

    # Uncovered interval misses cleanly.
    assert reloaded.get_clip_words(5.0, 40.0) is None
    assert reloaded.get_clip_words(10.0, 45.0) is None


def test_cache_invalidation_on_audio_source_and_model_changes(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    video_id = "9000000001"
    wav = _write_bytes(normalized_audio_path(data_dir, video_id), b"downloaded-audio-v1")
    sidecar = audio_source_path(data_dir, video_id)

    # 1. Downloaded audio (no sidecar) with model "base".
    c_dl = TranscriptCache.for_vod(data_dir, video_id, model_name="base", wav_path=wav)
    c_dl.put_span(0.0, 30.0, [Word("downloaded", 5.0, 5.5)])
    assert c_dl.get_words(2.0, 28.0) is not None

    # 2. Different model name misses.
    c_small = TranscriptCache.for_vod(data_dir, video_id, model_name="small", wav_path=wav)
    assert c_small.get_words(2.0, 28.0) is None

    # 3. Replacing the downloaded WAV invalidates the wav_file identity.
    wav.write_bytes(b"downloaded-audio-v2-different-length")
    c_dl2 = TranscriptCache.for_vod(data_dir, video_id, model_name="base", wav_path=wav)
    assert c_dl2.source_key != c_dl.source_key
    assert c_dl2.get_words(2.0, 28.0) is None

    # 4. Switching to a local media file (.source.json written by #138) invalidates the cache.
    local_a = _write_bytes(tmp_path / "vod_a.mp4", b"local-video-A")
    sidecar.write_text(json.dumps(_local_file_identity(local_a)), encoding="utf-8")
    c_local_a = TranscriptCache.for_vod(data_dir, video_id, model_name="base", wav_path=wav)
    assert c_local_a.source_key != c_dl2.source_key
    assert c_local_a.get_words(2.0, 28.0) is None
    c_local_a.put_span(0.0, 30.0, [Word("localA", 5.0, 5.5)])

    # 5. Swapping to local file B invalidates the cache.
    local_b = _write_bytes(tmp_path / "vod_b.mp4", b"local-video-B-longer")
    sidecar.write_text(json.dumps(_local_file_identity(local_b)), encoding="utf-8")
    c_local_b = TranscriptCache.for_vod(data_dir, video_id, model_name="base", wav_path=wav)
    assert c_local_b.source_key != c_local_a.source_key
    assert c_local_b.get_words(2.0, 28.0) is None

    # 6. Deleting sidecar (back to downloaded audio) invalidates local_b cache.
    sidecar.unlink()
    c_back = TranscriptCache.for_vod(data_dir, video_id, model_name="base", wav_path=wav)
    assert c_back.source_key == c_dl2.source_key
    assert c_back.get_words(2.0, 28.0) is None


def test_find_cache_for_media_matches_local_source_and_invalidates_on_edit(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    video_id = "9000000002"
    wav = _write_bytes(normalized_audio_path(data_dir, video_id), b"wav-bytes")
    video = _write_bytes(tmp_path / "vod.mp4", b"video-bytes-v1")
    audio_source_path(data_dir, video_id).write_text(
        json.dumps(_local_file_identity(video)), encoding="utf-8"
    )

    cache = TranscriptCache.for_vod(data_dir, video_id, model_name="base", wav_path=wav)
    cache.put_span(10.0, 40.0, [Word("clutch!", 15.0, 15.5)])

    found = find_cache_for_media(data_dir, video, model_name="base")
    assert found is not None
    words = found.get_clip_words(12.0, 38.0)
    assert words == [Word("clutch!", 3.0, 3.5)]

    # Different model name does not match.
    assert find_cache_for_media(data_dir, video, model_name="tiny") is None

    # Editing the video file changes its identity -> cache miss.
    video.write_bytes(b"video-bytes-v2-edited")
    assert find_cache_for_media(data_dir, video, model_name="base") is None


def test_corrupt_sidecar_and_corrupt_cache_file_do_not_crash(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    video_id = "9000000003"
    wav = _write_bytes(normalized_audio_path(data_dir, video_id), b"wav-bytes")
    audio_source_path(data_dir, video_id).write_text("{not valid json", encoding="utf-8")

    # Falls back to WAV fingerprint instead of crashing.
    identity = resolve_vod_audio_identity(data_dir, video_id, wav_path=wav)
    assert identity["kind"] == "wav_file"

    cache = TranscriptCache.for_vod(data_dir, video_id, model_name="base", wav_path=wav)
    cache.path.parent.mkdir(parents=True, exist_ok=True)
    cache.path.write_text("corrupt json!!!", encoding="utf-8")
    assert cache.get_span(0.0, 10.0) is None
    assert compute_source_key(identity) == cache.source_key


def test_slice_words_for_interval_uses_midpoint_and_clamps_edges() -> None:
    words = [
        Word("mostly_before", 9.0, 10.2),  # midpoint 9.6 -> excluded
        Word("mostly_inside", 9.8, 10.6),  # midpoint 10.2 -> included, clamped to [10.0, 10.6]
        Word("middle", 12.0, 12.5),
        Word("mostly_after", 19.9, 21.0),  # midpoint 20.45 -> excluded
    ]

    sliced = slice_words_for_interval(words, 10.0, 20.0, clip_relative=True)

    assert [w.text for w in sliced] == ["mostly_inside", "middle"]
    assert sliced[0].start_s == pytest.approx(0.0)
    assert sliced[0].end_s == pytest.approx(0.6)
    assert sliced[1].start_s == pytest.approx(2.0)
    assert sliced[1].end_s == pytest.approx(2.5)
