"""Tests for burned-in captions: line grouping, the ASS file, and a real FFmpeg burn-in."""

import importlib
import importlib.util
from pathlib import Path

import pytest

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
from autoclipper.ffmpeg import ffmpeg_command, run_command


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


def test_subtitles_filter_escapes_colons_and_quotes(tmp_path: Path) -> None:
    path = tmp_path / "it's a: test.ass"

    value = subtitles_filter(path)

    assert value.startswith("subtitles=filename='")
    assert "\\'" in value
    assert "\\:" in value


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


def black_video(path: Path, seconds: float = 6.0) -> Path:
    command = ffmpeg_command(
        "-y",
        *("-f", "lavfi", "-i", f"color=black:size=640x360:rate=25:duration={seconds}"),
        *("-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:duration={seconds}"),
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


@pytest.mark.live
@pytest.mark.skipif(
    importlib.util.find_spec("faster_whisper") is None, reason="needs `uv sync --extra captions`"
)
def test_the_real_model_transcribes_a_clip(tmp_path: Path) -> None:
    # Downloads the tiny model on first run. A tone has no words; this checks the model runs.
    source = black_video(tmp_path / "vod.mp4", seconds=3.0)
    wav = captions.extract_audio(source, Interval(0.0, 3.0), tmp_path / "clip.wav")

    result = FasterWhisperTranscriber("tiny")(wav)

    assert all(word.end_s >= word.start_s for word in result)
