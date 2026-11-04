"""Tests for burned-in captions: line grouping, the ASS file, and a real FFmpeg burn-in."""

import importlib
import importlib.util
import subprocess
import sys
import wave
from pathlib import Path

import pytest

from autoclipper.config import get_settings
from autoclipper.export import captions
from autoclipper.export.captions import (
    CaptioningError,
    CaptionLine,
    FasterWhisperTranscriber,
    Word,
    ass_subtitles,
    export_clip_with_captions,
    group_words,
    subtitles_filter,
)
from autoclipper.export.cut import Interval
from autoclipper.export.encode import ExportRequest
from autoclipper.ffmpeg import FFMPEG, ffmpeg_command, find_program, run_command


def words(*items: tuple[str, float, float]) -> list[Word]:
    return [Word(text, start, end) for text, start, end in items]


# --- Grouping ----------------------------------------------------------------------------------


def test_words_close_together_share_a_line_until_it_is_full() -> None:
    lines = group_words(
        words(
            ("no", 0.0, 0.2),
            ("way", 0.25, 0.5),
            ("that", 0.55, 0.7),
            ("just", 0.75, 0.9),
            ("happened", 0.95, 1.4),  # would make 25 characters: starts a new line
        )
    )

    assert [line.text for line in lines] == ["no way that just", "happened"]
    assert (lines[0].start_s, lines[1].end_s) == (0.0, 1.4)


def test_a_pause_starts_a_new_line() -> None:
    lines = group_words(words(("wait", 0.0, 0.3), ("what", 1.2, 1.5)))

    assert [line.text for line in lines] == ["wait", "what"]


def test_lines_hold_word_and_time_limits() -> None:
    many = [Word(f"w{i}", 0.1 * i, 0.1 * i + 0.08) for i in range(12)]

    lines = group_words(many)

    assert all(len(line.text.split()) <= captions.MAX_LINE_WORDS for line in lines)
    slow = group_words(
        [Word("a", 0.0, 0.5), Word("b", 0.6, 1.5), Word("c", 1.6, 2.6), Word("d", 2.7, 3.4)]
    )
    assert all(line.end_s - line.start_s <= captions.MAX_LINE_S + 1e-9 for line in slow)


def test_a_short_gap_keeps_the_line_up_until_the_next() -> None:
    lines = group_words(words(("one", 0.0, 0.5), ("two", 0.7, 1.0)), pause_s=0.1)

    assert lines[0].end_s == 0.7  # held over the 0.2 s gap
    assert lines[1].start_s == 0.7


def test_bad_and_empty_words_are_skipped() -> None:
    lines = group_words(
        words(("ok", 0.0, 0.3), (" ", 0.4, 0.5), ("bad", 2.0, 1.0), ("nan", float("nan"), 1.0))
    )

    assert [line.text for line in lines] == ["ok"]
    assert group_words([]) == []


# --- ASS and the filter ------------------------------------------------------------------------


def test_ass_document_has_the_frame_size_and_timed_lines() -> None:
    document = ass_subtitles(
        [CaptionLine("hello {there}", 0.5, 1.25), CaptionLine("bye", 61.0, 3725.5)]
    )

    assert "PlayResX: 1080" in document
    assert "PlayResY: 1920" in document
    assert "Dialogue: 0,0:00:00.50,0:00:01.25,Default,,0,0,0,,hello there" in document
    assert "Dialogue: 0,0:01:01.00,1:02:05.50,Default,,0,0,0,,bye" in document


def test_subtitles_filter_escapes_for_both_ffmpeg_parsers(tmp_path: Path) -> None:
    value = subtitles_filter(tmp_path / "it's [a], b; c.ass")

    assert value.startswith("subtitles=filename=")
    assert "it\\\\\\'s \\[a\\]\\, b\\; c.ass" in value


def test_the_default_transcriber_uses_the_configured_model(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("AUTOCLIPPER_WHISPER_MODEL", "small")
    get_settings.cache_clear()
    asked: list[str] = []

    def fake_transcriber_for(model_size: str) -> FakeTranscriber:
        asked.append(model_size)
        raise CaptioningError("stop here")

    monkeypatch.setattr(captions, "transcriber_for", fake_transcriber_for)

    with pytest.raises(CaptioningError, match="stop here"):
        export_clip_with_captions(
            ExportRequest(tmp_path / "vod.mp4", Interval(0, 1), tmp_path / "c.mp4")
        )

    assert asked == ["small"]


def test_one_transcriber_is_shared_per_model_size() -> None:
    assert captions.transcriber_for("tiny") is captions.transcriber_for("tiny")
    assert captions.transcriber_for("tiny") is not captions.transcriber_for("base")


def test_missing_faster_whisper_is_a_captioning_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    real = importlib.import_module

    def fake_import(name: str, package: str | None = None) -> object:
        if name == "faster_whisper":
            raise ImportError("No module named 'faster_whisper'")
        return real(name, package)

    monkeypatch.setattr(captions.importlib, "import_module", fake_import)

    with pytest.raises(CaptioningError, match="uv sync --extra captions"):
        FasterWhisperTranscriber()(tmp_path / "clip.wav")


# --- FFmpeg ------------------------------------------------------------------------------------


def black_video(path: Path, seconds: float = 6.0, *, tone_from_s: float = 0.0) -> Path:
    """A black video whose audio is silent until `tone_from_s`, then a 440 Hz tone."""
    tone = f"if(gte(t,{tone_from_s}),0.5*sin(2*PI*440*t),0)"
    command = ffmpeg_command(
        "-y",
        *("-f", "lavfi", "-i", f"color=black:size=640x360:rate=25:duration={seconds}"),
        *("-f", "lavfi", "-i", f"aevalsrc='{tone}':sample_rate=48000:duration={seconds}"),
        *("-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac"),
        "-shortest",
        path,
    )
    run_command(command, timeout_s=120)
    return path


def bright_pixels(video: Path, at_s: float, tmp_path: Path) -> int:
    """Pixels brighter than 200 in the lower half of the frame at `at_s` (white caption text)."""
    gray = tmp_path / f"frame-{at_s}.gray"
    command = ffmpeg_command(
        "-y",
        *("-ss", str(at_s), "-i", video, "-frames:v", "1"),
        *("-vf", "crop=iw:ih/2:0:ih/2,format=gray", "-f", "rawvideo", gray),
    )
    run_command(command, timeout_s=60)
    return sum(1 for value in gray.read_bytes() if value > 200)


def tone_onset_s(wav: Path) -> float:
    """Seconds from the start of `wav` to its first loud sample."""
    samples = captions.read_wav(wav)
    loud = (abs(samples) > 0.1).nonzero()[0]
    assert loud.size, "the clip's audio has no tone"
    return float(loud[0]) / captions.SAMPLE_RATE_HZ


class OnsetTranscriber:
    """Hears one word, "NOW", where the tone starts in the WAV it is given."""

    def __call__(self, wav: Path, /) -> list[Word]:
        onset = tone_onset_s(wav)
        return [Word("NOW", onset, onset + 0.6)]


class FakeTranscriber:
    """Returns fixed words (clip time) and records the WAVs it was given."""

    def __init__(self, result: list[Word]) -> None:
        self.result = result
        self.calls: list[Path] = []

    def __call__(self, wav: Path, /) -> list[Word]:
        self.calls.append(wav)
        assert wav.stat().st_size > 1000  # the clip's audio was extracted
        return self.result


@pytest.mark.ffmpeg
def test_captions_are_burned_in_while_their_words_are_spoken(tmp_path: Path) -> None:
    source = black_video(tmp_path / "vod.mp4")
    transcriber = FakeTranscriber(words(("HELLO", 0.5, 0.9), ("WORLD", 1.0, 1.6)))

    result = export_clip_with_captions(
        ExportRequest(source, Interval(1.0, 5.0), tmp_path / "clip.mp4"),
        transcriber=transcriber,
    )

    assert len(transcriber.calls) == 1
    assert (result.width, result.height) == (1080, 1920)
    shown = bright_pixels(result.output, 1.2, tmp_path)  # during "HELLO WORLD" (0.5-1.6 s)
    before = bright_pixels(result.output, 0.2, tmp_path)
    after = bright_pixels(result.output, 3.0, tmp_path)
    assert shown > 1000
    assert before == 0
    assert after == 0


@pytest.mark.ffmpeg
def test_captions_follow_the_clips_own_audio(tmp_path: Path) -> None:
    # The tone starts 2.0 s into the source, so 1.0 s into a clip cut from 1.0 s. A caption at
    # 2.0 s would mean the audio was taken from the wrong place.
    source = black_video(tmp_path / "vod.mp4", tone_from_s=2.0)

    result = export_clip_with_captions(
        ExportRequest(source, Interval(1.0, 5.0), tmp_path / "clip.mp4"),
        transcriber=OnsetTranscriber(),
    )

    assert bright_pixels(result.output, 0.8, tmp_path) == 0
    assert bright_pixels(result.output, 1.3, tmp_path) > 1000
    assert bright_pixels(result.output, 2.2, tmp_path) == 0


DIRECTORY_NAMES = ["it's", "a, b; c", "[x]", "100% =ok"] + (
    [] if sys.platform == "win32" else ["a:b", "back\\slash"]
)


@pytest.mark.ffmpeg
@pytest.mark.parametrize("name", DIRECTORY_NAMES)
def test_the_subtitles_filter_reads_paths_with_special_characters(
    name: str, tmp_path: Path
) -> None:
    ass = tmp_path / name / "captions.ass"
    ass.parent.mkdir()
    ass.write_text(captions.ass_subtitles([CaptionLine("HELLO", 0.0, 5.0)]), encoding="utf-8")
    command = [
        str(find_program(FFMPEG)),
        *("-v", "error", "-f", "lavfi", "-i", "color=black:size=1080x1920:duration=1"),
        *("-vf", f"{subtitles_filter(ass)},format=gray", "-frames:v", "1"),
        *("-f", "rawvideo", "-"),
    ]

    frame = subprocess.run(command, capture_output=True, check=True, timeout=60).stdout

    assert sum(1 for value in frame if value > 200) > 1000


@pytest.mark.ffmpeg
def test_failed_audio_extraction_is_a_captioning_error(tmp_path: Path) -> None:
    source = tmp_path / "silent.mp4"
    run_command(
        ffmpeg_command(
            "-y",
            *("-f", "lavfi", "-i", "color=black:size=640x360:rate=25:duration=2"),
            *("-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", source),
        ),
        timeout_s=60,
    )

    with pytest.raises(CaptioningError, match="extract the clip's audio"):
        export_clip_with_captions(
            ExportRequest(source, Interval(0.0, 1.0), tmp_path / "clip.mp4"),
            transcriber=FakeTranscriber([]),
            has_audio=lambda _: True,  # wrong on purpose: the source has no audio stream
        )

    assert not (tmp_path / "clip.mp4").exists()


@pytest.mark.ffmpeg
def test_a_clip_without_speech_is_exported_without_captions(tmp_path: Path) -> None:
    source = black_video(tmp_path / "vod.mp4", seconds=3.0)

    result = export_clip_with_captions(
        ExportRequest(source, Interval(0.0, 2.0), tmp_path / "clip.mp4"),
        transcriber=FakeTranscriber([]),
    )

    assert result.output.is_file()
    assert bright_pixels(result.output, 1.0, tmp_path) == 0


@pytest.mark.ffmpeg
def test_a_failed_transcription_writes_nothing(tmp_path: Path) -> None:
    source = black_video(tmp_path / "vod.mp4", seconds=3.0)

    def broken(wav: Path, /) -> list[Word]:
        raise CaptioningError("transcription failed: boom")

    with pytest.raises(CaptioningError):
        export_clip_with_captions(
            ExportRequest(source, Interval(0.0, 2.0), tmp_path / "clip.mp4"), transcriber=broken
        )

    assert not (tmp_path / "clip.mp4").exists()


def has_flite() -> bool:
    try:
        command = [str(find_program(FFMPEG)), "-hide_banner", "-filters"]
    except Exception:
        return False
    output = subprocess.run(command, capture_output=True, text=True, timeout=30).stdout
    return " flite " in output


@pytest.mark.live
@pytest.mark.skipif(
    importlib.util.find_spec("faster_whisper") is None, reason="needs `uv sync --extra captions`"
)
def test_the_real_model_captions_synthesized_speech(tmp_path: Path) -> None:
    # Downloads the tiny model on first run. FFmpeg's flite filter speaks a known sentence 1 s
    # into the clip; the model must hear those words, starting after the silence.
    if not has_flite():
        pytest.skip("needs an FFmpeg built with libflite")
    wav = tmp_path / "speech.wav"
    run_command(
        ffmpeg_command(
            "-y",
            *("-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono:d=1"),
            *("-f", "lavfi", "-i", "flite=text='no way that just happened':voice=kal16"),
            *("-filter_complex", "[0][1]concat=n=2:v=0:a=1", "-ac", "1", "-ar", "16000", wav),
        ),
        timeout_s=60,
    )
    with wave.open(str(wav)) as audio:
        assert audio.getframerate() == 16000

    result = FasterWhisperTranscriber("tiny")(wav)

    spoken = " ".join(word.text.lower().strip(".,!?") for word in result)
    assert "way" in spoken
    assert "happened" in spoken
    assert result[0].start_s >= 0.7
    lines = group_words(result)
    assert lines
    assert all(len(line.text) <= captions.MAX_LINE_CHARS for line in lines)
