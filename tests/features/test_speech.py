"""Tests for shortlist Whisper speech features (docs/ml/features.md § Phase 2, #64)."""

import json
import math
import subprocess
import sys
import wave
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import numpy.typing as npt
import polars as pl
import pytest
from typer.testing import CliRunner

from autoclipper.analysis import (
    AnalysisResult,
    Candidate,
    ScorerInfo,
    SpeechFeatures,
    VodSummary,
)
from autoclipper.analyze import analyze_vod
from autoclipper.cli import ExitCode, app
from autoclipper.config import get_settings
from autoclipper.export import captions
from autoclipper.export.captions import (
    CaptioningError,
    FasterWhisperTranscriber,
    Word,
    caption_lines_for,
    export_clip_with_captions,
)
from autoclipper.export.cut import Interval
from autoclipper.export.encode import ExportRequest
from autoclipper.features import speech
from autoclipper.features.audio import SAMPLE_RATE_HZ, AudioFormatError
from autoclipper.features.chat import load_lexicon
from autoclipper.features.speech import (
    FEATURE_COLUMNS,
    PEAK_LOUDNESS_DECAY_S,
    SpeechKeywordsError,
    attach_speech_features,
    compute_candidate_speech_features,
    default_speech_keywords,
    extract_speech_features,
    load_speech_keywords,
)
from autoclipper.features.transcript_cache import TranscriptCache
from autoclipper.ffmpeg import ffmpeg_command, run_command
from autoclipper.ingest.media import normalized_audio_path
from autoclipper.ingest.vod import IngestTools, _local_file_identity, audio_source_path

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_KEYWORDS = REPO_ROOT / "configs" / "speech_keywords.yaml"
CONFIG_LEXICON = REPO_ROOT / "configs" / "lexicon.yaml"
runner = CliRunner()


def write_wav(
    path: Path,
    samples: npt.NDArray[np.floating],
    *,
    rate: int = SAMPLE_RATE_HZ,
    channels: int = 1,
    width: int = 2,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
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


def make_candidate(
    rank: int,
    start_s: float,
    end_s: float,
    *,
    score: float | None = None,
) -> Candidate:
    return Candidate(
        candidate_id=f"c{rank:03d}",
        rank=rank,
        score=float(100 - rank) if score is None else score,
        start_s=start_s,
        end_s=end_s,
        source="discovered",
        n_viewer_clips=0,
        top_signals=("chat_msg_rate_z",),
    )


def make_result(
    candidates: list[Candidate], *, video_id: str = "9000000001", duration_s: float = 120.0
) -> AnalysisResult:
    return AnalysisResult(
        generated_at=datetime(2026, 11, 25, 12, 0, 0, tzinfo=UTC),
        vod=VodSummary(
            video_id=video_id,
            title="Synthetic stream",
            broadcaster_id="9100000001",
            broadcaster_display_name="streamer",
            duration_s=duration_s,
            created_at=datetime(2026, 11, 25, 10, 0, 0, tzinfo=UTC),
        ),
        scorer=ScorerInfo(kind="heuristic", name="chat_loudness"),
        n_viewer_clips_total=0,
        warnings=(),
        candidates=tuple(candidates),
    )


class FakeTranscriber:
    """Records every WAV slice it receives and returns configurable local-time words."""

    def __init__(self, words: list[Word] | None = None) -> None:
        self._words = (
            list(words)
            if words is not None
            else [
                Word("No", 3.0, 3.3),
                Word("way!", 3.4, 3.9),
                Word("What?", 5.0, 5.5),
            ]
        )
        self.durations_s: list[float] = []
        self.call_count = 0

    def __call__(self, wav: Path, /) -> list[Word]:
        self.call_count += 1
        with wave.open(str(wav), "rb") as reader:
            self.durations_s.append(reader.getnframes() / reader.getframerate())
        return list(self._words)


# --- Keywords config -------------------------------------------------------------------------


def test_load_speech_keywords_reads_repo_config() -> None:
    kw = load_speech_keywords(CONFIG_KEYWORDS)

    assert kw.version >= 1
    assert kw.feature_version == f"speech-v1+keywords-v{kw.version}"
    assert "no way" in kw.words
    assert "clip it" in kw.words
    assert kw.pattern is not None
    assert kw.pattern.search("OH MY GOD, let\u2019s go! Clip it!") is not None
    # Word boundaries: "bro" must not match inside "broken".
    assert kw.pattern.search("that bridge is broken") is None


def test_load_speech_keywords_rejects_malformed_files(tmp_path: Path) -> None:
    with pytest.raises(SpeechKeywordsError, match="can't read"):
        load_speech_keywords(tmp_path / "missing.yaml")

    bad_ver = tmp_path / "bad_ver.yaml"
    bad_ver.write_text("version: 0\nwords: [bro]\n", encoding="utf-8")
    with pytest.raises(SpeechKeywordsError, match="version"):
        load_speech_keywords(bad_ver)

    bad_words = tmp_path / "bad_words.yaml"
    bad_words.write_text("version: 1\nwords: ['']\n", encoding="utf-8")
    with pytest.raises(SpeechKeywordsError, match="words"):
        load_speech_keywords(bad_words)


# --- Feature math ----------------------------------------------------------------------------


def test_compute_candidate_speech_features_all_six_metrics() -> None:
    kw = default_speech_keywords()
    # Candidate is [10.0, 20.0] (10 s). Include padding words outside [10, 20] that must be ignored.
    words = [
        Word("outside!", 8.5, 9.0),
        Word("No", 11.0, 11.5),
        # Overlaps previous word by 0.1 s -> union is [11.0, 12.0] = 1.0 s.
        Word("way!", 11.4, 12.0),
        Word("What?", 14.0, 15.0),  # +1.0 s
        Word("Let\u2019s", 16.0, 16.5),
        Word("go!", 16.5, 17.5),  # +1.5 s -> total speech = 3.5 s / 10 s = 0.35
        Word("after!", 21.0, 21.5),
    ]
    # Audio: loud burst at 11.5 s (inside "way!" -> distance 0 -> alignment 1.0).
    samples = np.zeros(10 * SAMPLE_RATE_HZ, dtype=np.float32)
    t = np.arange(SAMPLE_RATE_HZ, dtype=np.float32) / SAMPLE_RATE_HZ
    samples[int(1.0 * SAMPLE_RATE_HZ) : int(2.0 * SAMPLE_RATE_HZ)] = 0.8 * np.sin(
        2 * np.pi * 440 * t
    )

    sf = compute_candidate_speech_features(
        words,
        start_s=10.0,
        end_s=20.0,
        keywords=kw,
        samples=samples,
        samples_start_s=10.0,
    )

    assert sf.speech_word_rate == pytest.approx(5 / 10.0)
    assert sf.speech_exclaim_density == pytest.approx(2 / 5.0)
    assert sf.speech_question_density == pytest.approx(1 / 5.0)
    # "No way" + "Let's go" -> 2 matches in 10 s.
    assert sf.speech_keyword_rate == pytest.approx(2 / 10.0)
    assert sf.speech_frac == pytest.approx(3.5 / 10.0)
    assert sf.speech_peak_loudness_align == pytest.approx(1.0)


def test_peak_loudness_alignment_decays_away_from_speech_and_zero_when_silent() -> None:
    kw = default_speech_keywords()
    # One word at [10.0, 11.0] inside [10.0, 20.0].
    words = [Word("hello", 10.0, 11.0)]

    # Loud burst centered around 13.0 s -> 2.0 s after word end -> exp(-2.0 / 2.0) = exp(-1).
    samples = np.zeros(10 * SAMPLE_RATE_HZ, dtype=np.float32)
    center = int(3.0 * SAMPLE_RATE_HZ)
    samples[center - 512 : center + 512] = 0.9

    sf = compute_candidate_speech_features(
        words, start_s=10.0, end_s=20.0, keywords=kw, samples=samples, samples_start_s=10.0
    )
    assert sf.speech_peak_loudness_align == pytest.approx(
        math.exp(-2.0 / PEAK_LOUDNESS_DECAY_S), abs=0.05
    )

    # Silent audio -> 0.0.
    silent = np.zeros(10 * SAMPLE_RATE_HZ, dtype=np.float32)
    sf_silent = compute_candidate_speech_features(
        words, start_s=10.0, end_s=20.0, keywords=kw, samples=silent, samples_start_s=10.0
    )
    assert sf_silent.speech_peak_loudness_align == 0.0

    # No words -> all zeros.
    sf_empty = compute_candidate_speech_features(
        [], start_s=10.0, end_s=20.0, keywords=kw, samples=samples, samples_start_s=10.0
    )
    assert sf_empty == SpeechFeatures(
        speech_word_rate=0.0,
        speech_exclaim_density=0.0,
        speech_question_density=0.0,
        speech_keyword_rate=0.0,
        speech_frac=0.0,
        speech_peak_loudness_align=0.0,
    )


# --- Shortlist extraction, padding, caching and optional extra -------------------------------


def test_extract_speech_features_transcribes_only_top_n_with_padding_and_caches(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    video_id = "9000000001"
    wav = write_wav(
        normalized_audio_path(data_dir, video_id),
        np.full(120 * SAMPLE_RATE_HZ, 0.2, dtype=np.float32),
    )
    candidates = [
        make_candidate(1, 10.0, 30.0),  # padded [8, 32] -> 24 s
        make_candidate(2, 50.0, 70.0),  # padded [48, 72] -> 24 s
        make_candidate(3, 85.0, 105.0),  # outside top_n=2
    ]
    fake = FakeTranscriber()

    first = extract_speech_features(
        candidates,
        wav,
        video_id=video_id,
        vod_duration_s=120.0,
        top_n=2,
        pad_s=2.0,
        model_name="base",
        transcriber=fake,
        data_dir=data_dir,
    )

    assert fake.call_count == 2
    assert fake.durations_s == [pytest.approx(24.0), pytest.approx(24.0)]
    assert first.transcribed_candidates == 2
    assert first.cached_candidates == 0
    assert first.skipped_unavailable is False
    assert first.warning is None
    assert set(first.by_candidate_id) == {"c001", "c002"}
    assert first.features.height == 3
    assert first.features.filter(pl.col("candidate_id") == "c003")["speech_word_rate"][0] is None
    assert first.features.filter(pl.col("candidate_id") == "c001")["speech_word_rate"][0] > 0.0

    # Second run with the same source identity & model hits the transcript cache completely.
    fake2 = FakeTranscriber()
    second = extract_speech_features(
        candidates,
        wav,
        video_id=video_id,
        vod_duration_s=120.0,
        top_n=2,
        pad_s=2.0,
        model_name="base",
        transcriber=fake2,
        data_dir=data_dir,
    )

    assert fake2.call_count == 0
    assert second.transcribed_candidates == 0
    assert second.cached_candidates == 2
    assert second.by_candidate_id == first.by_candidate_id


def test_extract_speech_features_reuses_shared_captions_transcriber(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    wav = write_wav(tmp_path / "vod.wav", np.zeros(30 * SAMPLE_RATE_HZ, dtype=np.float32))
    asked_models: list[str] = []
    fake = FakeTranscriber()

    def fake_transcriber_for(model_size: str) -> FakeTranscriber:
        asked_models.append(model_size)
        return fake

    monkeypatch.setattr(speech, "speech_available", lambda: True)
    monkeypatch.setattr(speech, "transcriber_for", fake_transcriber_for)

    extraction = extract_speech_features(
        [make_candidate(1, 2.0, 20.0)],
        wav,
        video_id="9000000001",
        model_name="tiny",
    )

    assert asked_models == ["tiny"]
    assert fake.call_count == 1
    assert extraction.transcribed_candidates == 1


def test_missing_captions_extra_skips_with_clear_warning_without_crashing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    wav = write_wav(tmp_path / "vod.wav", np.zeros(30 * SAMPLE_RATE_HZ, dtype=np.float32))
    monkeypatch.setattr(speech, "speech_available", lambda: False)

    extraction = extract_speech_features(
        [make_candidate(1, 2.0, 20.0)],
        wav,
        video_id="9000000001",
    )

    assert extraction.skipped_unavailable is True
    assert extraction.warning is not None
    assert "uv sync --extra captions" in extraction.warning
    assert extraction.by_candidate_id == {}
    assert extraction.features["speech_word_rate"][0] is None


def test_cached_transcripts_work_even_without_captions_extra(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    data_dir = tmp_path / "data"
    video_id = "9000000001"
    wav = write_wav(
        normalized_audio_path(data_dir, video_id),
        np.full(40 * SAMPLE_RATE_HZ, 0.1, dtype=np.float32),
    )
    cache = TranscriptCache.for_vod(data_dir, video_id, model_name="base", wav_path=wav)
    cache.put_span(3.0, 27.0, [Word("insane!", 10.0, 10.6)])

    monkeypatch.setattr(speech, "speech_available", lambda: False)
    extraction = extract_speech_features(
        [make_candidate(1, 5.0, 25.0)],
        wav,
        video_id=video_id,
        data_dir=data_dir,
    )

    assert extraction.skipped_unavailable is False
    assert extraction.warning is None
    assert extraction.cached_candidates == 1
    assert extraction.by_candidate_id["c001"].speech_keyword_rate == pytest.approx(1 / 20.0)


def test_candidate_past_audio_end_and_bad_wav_format(tmp_path: Path) -> None:
    short_wav = write_wav(tmp_path / "short.wav", np.zeros(5 * SAMPLE_RATE_HZ, dtype=np.float32))
    fake = FakeTranscriber()
    extraction = extract_speech_features(
        [make_candidate(1, 10.0, 20.0)],
        short_wav,
        video_id="9000000001",
        transcriber=fake,
    )
    assert fake.call_count == 0
    assert extraction.empty_audio_candidates == 1
    assert extraction.by_candidate_id["c001"].speech_word_rate == 0.0

    stereo_wav = write_wav(
        tmp_path / "stereo.wav", np.zeros(SAMPLE_RATE_HZ, dtype=np.float32), channels=2
    )
    with pytest.raises(AudioFormatError, match="expected 16000 Hz mono 16-bit"):
        extract_speech_features(
            [make_candidate(1, 0.0, 1.0)],
            stereo_wav,
            video_id="9000000001",
            transcriber=fake,
        )


# --- Caption export cache reuse --------------------------------------------------------------


def test_caption_lines_for_reuses_transcript_cache_and_falls_back_on_miss(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    data_dir = tmp_path / "data"
    video = tmp_path / "vod.mp4"
    video.write_bytes(b"fake-video-content")
    cache = TranscriptCache.for_media(data_dir, "9000000001", video, model_name="base")
    cache.put_span(8.0, 22.0, [Word("NO", 10.5, 10.8), Word("WAY!", 10.9, 11.4)])

    extract_called = False

    def fail_extract(*_args: object, **_kwargs: object) -> Path:
        nonlocal extract_called
        extract_called = True
        raise AssertionError("extract_audio should not run on a transcript cache hit")

    monkeypatch.setattr(captions, "extract_audio", fail_extract)
    fake = FakeTranscriber()

    lines = caption_lines_for(
        video,
        Interval(10.0, 20.0),
        fake,
        tmp_path,
        has_audio=True,
        transcript_cache=cache,
    )

    assert not extract_called
    assert fake.call_count == 0
    assert [line.text for line in lines] == ["NO WAY!"]
    assert lines[0].start_s == pytest.approx(0.5)
    assert lines[0].end_s == pytest.approx(1.4)

    # An interval outside the cached [8.0, 22.0] span falls back to extract_audio.
    with pytest.raises(AssertionError, match="extract_audio should not run"):
        caption_lines_for(
            video,
            Interval(5.0, 20.0),
            fake,
            tmp_path,
            has_audio=True,
            transcript_cache=cache,
        )


def _black_video(path: Path, seconds: float = 6.0) -> Path:
    command = ffmpeg_command(
        "-y",
        *("-f", "lavfi", "-i", f"color=black:size=640x360:rate=25:duration={seconds}"),
        *(
            "-f",
            "lavfi",
            "-i",
            f"aevalsrc='0.2*sin(2*PI*440*t)':sample_rate=48000:duration={seconds}",
        ),
        *("-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac"),
        "-shortest",
        path,
    )
    run_command(command, timeout_s=120)
    return path


def _bright_pixels(video: Path, at_s: float, tmp_path: Path) -> int:
    gray = tmp_path / f"frame-{at_s}.gray"
    command = ffmpeg_command(
        "-y",
        *("-ss", str(at_s), "-i", video, "-frames:v", "1"),
        *("-vf", "crop=iw:ih/2:0:ih/2,format=gray", "-f", "rawvideo", gray),
    )
    run_command(command, timeout_s=60)
    return sum(1 for value in gray.read_bytes() if value > 200)


@pytest.mark.ffmpeg
def test_export_clip_with_captions_auto_reuses_cached_transcript(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    data_dir = tmp_path / "data"
    monkeypatch.setenv("AUTOCLIPPER_DATA_DIR", str(data_dir))
    monkeypatch.setenv("AUTOCLIPPER_WHISPER_MODEL", "base")
    get_settings.cache_clear()

    source = _black_video(tmp_path / "vod.mp4", seconds=6.0)
    video_id = "9000000001"
    wav = write_wav(
        normalized_audio_path(data_dir, video_id),
        np.zeros(6 * SAMPLE_RATE_HZ, dtype=np.float32),
    )
    audio_source_path(data_dir, video_id).write_text(
        json.dumps(_local_file_identity(source)), encoding="utf-8"
    )
    # Populate the transcript cache via speech feature extraction (with a fake transcriber).
    extract_speech_features(
        [make_candidate(1, 1.0, 5.0)],
        wav,
        video_id=video_id,
        vod_duration_s=6.0,
        pad_s=1.0,
        model_name="base",
        transcriber=FakeTranscriber([Word("CACHED", 1.5, 2.2), Word("WORDS", 2.3, 3.0)]),
        data_dir=data_dir,
    )

    # Make FasterWhisperTranscriber fail if called: caption export must hit the cache instead.
    def fail_load(self: FasterWhisperTranscriber) -> object:
        raise CaptioningError("model should not be loaded on cache hit")

    monkeypatch.setattr(FasterWhisperTranscriber, "_load", fail_load)

    result = export_clip_with_captions(
        ExportRequest(source, Interval(1.0, 5.0), tmp_path / "clip.mp4")
    )

    # Words were at slice-local 1.5-3.0 s on padded [0, 6], so VOD 1.5-3.0 s -> clip 0.5-2.0 s.
    assert _bright_pixels(result.output, 1.0, tmp_path) > 1000
    assert _bright_pixels(result.output, 3.2, tmp_path) == 0


# --- Result attachment, analyze opt-in and CLI -----------------------------------------------


def test_attach_speech_features_is_backward_compatible() -> None:
    base_result = make_result([make_candidate(1, 10.0, 30.0), make_candidate(2, 50.0, 70.0)])
    # Without speech features, the JSON omits `speech_features`.
    raw_before = json.loads(base_result.model_dump_json())
    assert "speech_features" not in raw_before["candidates"][0]

    sf = SpeechFeatures(
        speech_word_rate=2.5,
        speech_exclaim_density=0.2,
        speech_question_density=0.1,
        speech_keyword_rate=0.05,
        speech_frac=0.65,
        speech_peak_loudness_align=0.9,
    )
    enriched = attach_speech_features(base_result, {"c001": sf})
    raw_after = json.loads(enriched.model_dump_json())

    assert raw_after["candidates"][0]["speech_features"]["speech_word_rate"] == pytest.approx(2.5)
    assert "speech_features" not in raw_after["candidates"][1]
    assert AnalysisResult.model_validate_json(enriched.model_dump_json()) == enriched


def test_cli_speech_features_writes_parquet_and_updates_result(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    data_dir = tmp_path / "data"
    artifacts_dir = tmp_path / "artifacts"
    monkeypatch.setenv("AUTOCLIPPER_DATA_DIR", str(data_dir))
    monkeypatch.setenv("AUTOCLIPPER_ARTIFACTS_DIR", str(artifacts_dir))
    get_settings.cache_clear()

    video_id = "9000000001"
    write_wav(
        normalized_audio_path(data_dir, video_id),
        np.full(90 * SAMPLE_RATE_HZ, 0.15, dtype=np.float32),
    )
    result_file = tmp_path / f"{video_id}.json"
    result_file.write_text(
        make_result(
            [make_candidate(1, 5.0, 25.0), make_candidate(2, 40.0, 60.0)], duration_s=90.0
        ).model_dump_json(indent=2),
        encoding="utf-8",
    )

    fake = FakeTranscriber()
    monkeypatch.setattr(speech, "speech_available", lambda: True)
    monkeypatch.setattr(speech, "transcriber_for", lambda _model: fake)

    out = runner.invoke(
        app,
        [
            "speech-features",
            str(result_file),
            "--top-n",
            "1",
            "--keywords",
            str(CONFIG_KEYWORDS),
            "--update-result",
        ],
    )

    assert out.exit_code == 0, out.output
    parquet_path = artifacts_dir / "features" / f"{video_id}_speech.parquet"
    assert parquet_path.is_file()
    df = pl.read_parquet(parquet_path)
    assert list(df.columns) == [
        "candidate_id",
        "video_id",
        "start_s",
        "end_s",
        *FEATURE_COLUMNS,
        "feature_version",
    ]
    assert df.height == 2
    assert df.filter(pl.col("candidate_id") == "c001")["speech_word_rate"][0] > 0.0
    assert df.filter(pl.col("candidate_id") == "c002")["speech_word_rate"][0] is None

    updated = AnalysisResult.model_validate_json(result_file.read_text(encoding="utf-8"))
    assert updated.candidates[0].speech_features is not None
    assert updated.candidates[1].speech_features is None


def test_cli_speech_features_skips_cleanly_without_captions_extra(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    data_dir = tmp_path / "data"
    artifacts_dir = tmp_path / "artifacts"
    monkeypatch.setenv("AUTOCLIPPER_DATA_DIR", str(data_dir))
    monkeypatch.setenv("AUTOCLIPPER_ARTIFACTS_DIR", str(artifacts_dir))
    get_settings.cache_clear()

    video_id = "9000000001"
    write_wav(
        normalized_audio_path(data_dir, video_id),
        np.zeros(40 * SAMPLE_RATE_HZ, dtype=np.float32),
    )
    result_file = tmp_path / f"{video_id}.json"
    result_file.write_text(
        make_result([make_candidate(1, 5.0, 25.0)], duration_s=40.0).model_dump_json(),
        encoding="utf-8",
    )
    monkeypatch.setattr(speech, "speech_available", lambda: False)

    out = runner.invoke(
        app,
        ["speech-features", str(result_file), "--keywords", str(CONFIG_KEYWORDS)],
    )

    assert out.exit_code == 0, out.output
    assert "uv sync --extra captions" in out.output
    assert not (artifacts_dir / "features" / f"{video_id}_speech.parquet").exists()


def test_cli_analyze_rejects_speech_top_n_without_flag() -> None:
    out = runner.invoke(app, ["analyze", "9000000001", "--speech-top-n", "10"])
    assert out.exit_code == ExitCode.USER_ERROR
    assert "--speech-top-n requires --speech-features" in out.output


def test_analyze_vod_speech_features_opt_in_and_graceful_skip(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Default analyze skips speech features; opt-in attaches them or warns cleanly."""
    from autoclipper.synthetic.dataset import generate_dataset

    data_dir = tmp_path / "data"
    artifacts_dir = tmp_path / "artifacts"
    synth_dir = tmp_path / "synth"
    generate_dataset(synth_dir, seed=1)

    # Seed data_dir with one synthetic VOD's metadata, chat, audio and catalog rows so ingest skips.
    from autoclipper.ingest.catalog import CatalogEntry, record
    from autoclipper.ingest.vod import chat_path, vods_path

    vods_table = pl.read_parquet(synth_dir / "processed" / "vods.parquet")
    video_id = str(vods_table["video_id"][0])
    vods_path(data_dir).parent.mkdir(parents=True, exist_ok=True)
    (vods_path(data_dir)).write_bytes((synth_dir / "processed" / "vods.parquet").read_bytes())
    chat_dst = chat_path(data_dir, video_id)
    chat_dst.parent.mkdir(parents=True, exist_ok=True)
    chat_dst.write_bytes((synth_dir / "interim" / "chat" / f"{video_id}.parquet").read_bytes())
    wav_dst = normalized_audio_path(data_dir, video_id)
    wav_dst.parent.mkdir(parents=True, exist_ok=True)
    wav_dst.write_bytes((synth_dir / "interim" / "audio" / f"{video_id}.wav").read_bytes())

    now = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)
    for step in ("vod_meta", "chat", "audio"):
        record(data_dir, CatalogEntry(step, video_id, "ok", "test", "1", now, None))
    record(data_dir, CatalogEntry("clips", video_id, "unavailable", "test", "1", now, "none"))

    lexicon = load_lexicon(CONFIG_LEXICON)
    tools = IngestTools()

    # 1. Default analyze: never touches speech features.
    default_res = analyze_vod(
        video_id,
        data_dir=data_dir,
        artifacts_dir=artifacts_dir,
        tools=tools,
        lexicon=lexicon,
    )
    assert default_res.candidates
    assert all(c.speech_features is None for c in default_res.candidates)
    assert all(w.code != "speech_unavailable" for w in default_res.warnings)

    # 2. Opt-in analyze without captions extra: warns cleanly, never crashes.
    monkeypatch.setattr(speech, "speech_available", lambda: False)
    warn_res = analyze_vod(
        video_id,
        data_dir=data_dir,
        artifacts_dir=artifacts_dir,
        tools=tools,
        lexicon=lexicon,
        include_speech_features=True,
        speech_top_n=2,
    )
    assert any(w.code == "speech_unavailable" for w in warn_res.warnings)
    assert all(c.speech_features is None for c in warn_res.candidates)

    # 3. Opt-in analyze with fake transcriber: attaches features only to top_n candidates.
    fake = FakeTranscriber()
    opt_res = analyze_vod(
        video_id,
        data_dir=data_dir,
        artifacts_dir=artifacts_dir,
        tools=tools,
        lexicon=lexicon,
        include_speech_features=True,
        speech_top_n=1,
        speech_transcriber=fake,
    )
    assert fake.call_count == 1
    assert opt_res.candidates[0].speech_features is not None
    if len(opt_res.candidates) > 1:
        assert opt_res.candidates[1].speech_features is None


# --- Live faster-whisper test ----------------------------------------------------------------


def _synthesize_speech_wav(path: Path, text: str) -> bool:
    """Generate a tiny 16 kHz mono WAV of spoken English via the OS TTS engine, if available."""
    raw = path.with_name("tts.wav")
    if sys.platform == "win32":
        script = (
            "Add-Type -AssemblyName System.Speech; "
            "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
            f"$s.SetOutputToWaveFile('{raw}'); $s.Speak('{text}'); $s.Dispose()"
        )
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True,
            timeout=60,
            check=False,
        )
        if completed.returncode != 0 or not raw.is_file():
            return False
    else:
        return False
    run_command(
        ffmpeg_command(
            "-y",
            "-i",
            raw,
            "-ac",
            "1",
            "-ar",
            str(SAMPLE_RATE_HZ),
            "-c:a",
            "pcm_s16le",
            path,
        ),
        timeout_s=60,
    )
    return path.is_file()


@pytest.mark.live
@pytest.mark.filterwarnings("ignore::UserWarning:huggingface_hub")
@pytest.mark.filterwarnings("ignore::ResourceWarning")
def test_live_faster_whisper_tiny_speech_features(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Real faster-whisper smoke test on a tiny generated clip (deselected by default)."""
    if not speech.speech_available():
        pytest.skip("faster-whisper not installed (install with `uv sync --extra captions`)")
    monkeypatch.setenv("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    wav = tmp_path / "clip.wav"
    if not _synthesize_speech_wav(wav, "No way! Oh my god, clip it right now!"):
        # Fall back to a 4-second synthetic tone WAV if OS TTS isn't available.
        t = np.arange(4 * SAMPLE_RATE_HZ, dtype=np.float32) / SAMPLE_RATE_HZ
        write_wav(wav, 0.25 * np.sin(2 * np.pi * 440 * t))

    with wave.open(str(wav), "rb") as reader:
        dur = reader.getnframes() / reader.getframerate()

    extraction = extract_speech_features(
        [make_candidate(1, 0.0, dur)],
        wav,
        video_id="9000000099",
        vod_duration_s=dur,
        top_n=1,
        pad_s=0.0,
        model_name="tiny",
        keywords=load_speech_keywords(CONFIG_KEYWORDS),
        data_dir=tmp_path / "data",
    )

    assert extraction.skipped_unavailable is False
    assert extraction.transcribed_candidates == 1
    assert "c001" in extraction.by_candidate_id
    sf = extraction.by_candidate_id["c001"]
    assert 0.0 <= sf.speech_frac <= 1.0
    assert 0.0 <= sf.speech_peak_loudness_align <= 1.0
