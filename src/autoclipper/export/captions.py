"""Burn word-timed captions into exported clips (docs/architecture/overview.md § 4.6, step 4).

`export_clip_with_captions` exports a clip like `autoclipper.export.encode.export_clip` and burns
in captions of what is said in it:

1. Once the export has planned the clip's actual interval, the clip's audio is cut to a 16 kHz
   mono WAV (through the shared FFmpeg runner) and transcribed with word timestamps by a
   `Transcriber`. The default, `FasterWhisperTranscriber`, runs faster-whisper on the CPU; the
   model size comes from ``AUTOCLIPPER_WHISPER_MODEL``. faster-whisper is an optional install
   (``uv sync --extra captions``); tests pass a fake transcriber.
2. `group_words` groups the words into short lines sized for a 1080x1920 frame: at most
   `MAX_LINE_CHARS` characters and `MAX_LINE_WORDS` words, broken at pauses, each shown from its
   first word's start to its last word's end.
3. `ass_subtitles` writes the lines as an ASS file in one clean style (bold white text with a
   black outline in the lower third), and FFmpeg's ``subtitles`` filter (libass) burns it in.

Times are seconds from the clip's start. A clip without speech is exported without captions.
"""

import contextlib
import functools
import importlib
import logging
import math
import re
import tempfile
import wave
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Protocol

import numpy as np
import numpy.typing as npt

from autoclipper.config import get_settings
from autoclipper.export.cut import Interval
from autoclipper.export.encode import EncodeSettings, ExportRequest, ExportResult, export_clip
from autoclipper.export.errors import ExportError
from autoclipper.export.reframe import OUTPUT_HEIGHT, OUTPUT_WIDTH
from autoclipper.ffmpeg import FFmpegError, Runner, ffmpeg_command, probe, run_command

log = logging.getLogger(__name__)

MAX_LINE_CHARS: Final = 24
MAX_LINE_WORDS: Final = 5
# A pause this long ends a line; a line never stays on screen longer than MAX_LINE_S.
PAUSE_S: Final = 0.6
MAX_LINE_S: Final = 3.0
# A line stays up until the next one if the gap is shorter than this, so captions don't flicker.
HOLD_S: Final = 0.3
SAMPLE_RATE_HZ: Final = 16_000

DEFAULT_MODEL: Final = "base"


class CaptioningError(ExportError):
    """Transcription failed, or faster-whisper isn't installed (api-contract.md § 6.3)."""


@dataclass(frozen=True, slots=True)
class Word:
    """A spoken word, in seconds from the start of the audio it was transcribed from."""

    text: str
    start_s: float
    end_s: float


@dataclass(frozen=True, slots=True)
class CaptionLine:
    """One caption on screen, in seconds from the clip's start."""

    text: str
    start_s: float
    end_s: float


class Transcriber(Protocol):
    """Turns a 16 kHz mono WAV into words with timestamps."""

    def __call__(self, wav: Path, /) -> Sequence[Word]:
        """The words spoken in `wav`, in seconds from its start."""
        ...


@dataclass
class FasterWhisperTranscriber:
    """faster-whisper on the CPU (int8), with word timestamps and voice-activity filtering.

    The model is loaded on first use and kept, so one instance can caption many clips.

    Args:
        model_size: A faster-whisper model name such as ``base`` or ``small``.
    """

    model_size: str = DEFAULT_MODEL
    _model: object = field(default=None, init=False, repr=False)

    def __call__(self, wav: Path, /) -> list[Word]:
        """Transcribe `wav`.

        Raises:
            CaptioningError: faster-whisper isn't installed, or the model or transcription
                failed.
        """
        model = self._load()
        try:
            # Any: faster-whisper ships no type information.
            # The samples, not the path: faster-whisper's own decoder (PyAV) breaks across PyAV
            # versions, and the WAV is already 16 kHz mono.
            segments, _ = model.transcribe(  # type: ignore[attr-defined]
                read_wav(wav), word_timestamps=True, vad_filter=True, beam_size=1
            )
            words = [
                Word(w.word.strip(), float(w.start), float(w.end))
                for segment in segments
                for w in (segment.words or ())
                if w.word.strip()
            ]
        except Exception as exc:  # faster-whisper raises plain RuntimeErrors and more
            raise CaptioningError(f"transcription failed: {exc}") from exc
        return words

    def _load(self) -> object:
        if self._model is None:
            try:
                module = importlib.import_module("faster_whisper")
            except ImportError as exc:
                raise CaptioningError(
                    "captions need faster-whisper; install it with `uv sync --extra captions`"
                ) from exc
            try:
                self._model = module.WhisperModel(
                    self.model_size, device="cpu", compute_type="int8"
                )
            except Exception as exc:  # model download or load failure
                raise CaptioningError(
                    f"can't load the faster-whisper model {self.model_size!r}: {exc}"
                ) from exc
        return self._model


def read_wav(path: Path) -> "npt.NDArray[np.float32]":
    """The samples of a 16 kHz mono 16-bit WAV as float32 in [-1, 1].

    Raises:
        CaptioningError: The file isn't 16 kHz mono 16-bit PCM.
    """
    with wave.open(str(path), "rb") as reader:
        if (reader.getframerate(), reader.getnchannels(), reader.getsampwidth()) != (
            SAMPLE_RATE_HZ,
            1,
            2,
        ):
            raise CaptioningError(f"{path} isn't a 16 kHz mono 16-bit WAV")
        data = reader.readframes(reader.getnframes())
    return np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0


def group_words(
    words: Sequence[Word],
    *,
    max_chars: int = MAX_LINE_CHARS,
    max_words: int = MAX_LINE_WORDS,
    pause_s: float = PAUSE_S,
    max_line_s: float = MAX_LINE_S,
) -> list[CaptionLine]:
    """Group words into short caption lines, in time order.

    A line ends before a word that would make it longer than `max_chars` characters or
    `max_words` words, that starts `pause_s` or more after the previous word, or that would keep
    the line up longer than `max_line_s`. A line lasts from its first word's start to its last
    word's end, extended up to the next line when the gap is under `HOLD_S`. Words with a
    non-finite or reversed time are skipped.
    """
    clean = sorted(
        (
            w
            for w in words
            if w.text.strip()
            and math.isfinite(w.start_s)
            and math.isfinite(w.end_s)
            and w.end_s >= w.start_s >= 0
        ),
        key=lambda w: (w.start_s, w.end_s),
    )
    groups: list[list[Word]] = []
    for word in clean:
        if groups:
            line = groups[-1]
            text = " ".join(w.text.strip() for w in [*line, word])
            if (
                len(text) <= max_chars
                and len(line) < max_words
                and word.start_s - line[-1].end_s < pause_s
                and word.end_s - line[0].start_s <= max_line_s
            ):
                line.append(word)
                continue
        groups.append([word])
    lines = [
        CaptionLine(
            " ".join(w.text.strip() for w in g), g[0].start_s, max(g[-1].end_s, g[0].start_s + 0.2)
        )
        for g in groups
    ]
    for i in range(len(lines) - 1):
        gap = lines[i + 1].start_s - lines[i].end_s
        if 0 < gap < HOLD_S:
            lines[i] = CaptionLine(lines[i].text, lines[i].start_s, lines[i + 1].start_s)
        elif gap < 0:  # overlapping word times: end where the next line starts
            lines[i] = CaptionLine(
                lines[i].text, lines[i].start_s, max(lines[i].start_s + 0.05, lines[i + 1].start_s)
            )
    return lines


def _ass_time(seconds: float) -> str:
    centis = max(0, round(seconds * 100))
    h, rest = divmod(centis, 360_000)
    m, rest = divmod(rest, 6000)
    s, cs = divmod(rest, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _ass_text(text: str) -> str:
    # ASS treats braces as override blocks and backslashes as escapes.
    return re.sub(r"[{}\\]", "", text).replace("\n", " ")


def ass_subtitles(lines: Sequence[CaptionLine]) -> str:
    """An ASS document showing `lines` on a 1080x1920 frame, in one clean default style."""
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {OUTPUT_WIDTH}
PlayResY: {OUTPUT_HEIGHT}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,84,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,7,2,2,80,80,460,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""  # noqa: E501 - ASS format lines can't be wrapped
    events = [
        f"Dialogue: 0,{_ass_time(line.start_s)},{_ass_time(line.end_s)},Default,,0,0,0,,"
        f"{_ass_text(line.text)}"
        for line in lines
        if line.end_s > line.start_s
    ]
    return header + "\n".join(events) + ("\n" if events else "")


def subtitles_filter(ass_path: Path) -> str:
    r"""The FFmpeg filter that burns `ass_path` in, with the path escaped for a filter graph.

    FFmpeg unescapes a filter's options twice: once as the option value (special: ``\ ' :``)
    and once as part of the filter graph (special: ``\ ' [ ] , ;``). The path is escaped for
    both, so a temp directory under a name like ``O'Brien`` or a drive letter (``C:``) works.
    Forward slashes work on Windows too.
    """
    path = ass_path.resolve().as_posix()
    value = "".join(f"\\{c}" if c in "\\':" else c for c in path)
    escaped = "".join(f"\\{c}" if c in "\\'[],;" else c for c in value)
    return f"subtitles=filename={escaped}"


def extract_audio(
    source: Path, interval: Interval, wav: Path, *, runner: Runner | None = None
) -> Path:
    """Cut `interval` of `source`'s first audio stream to a 16 kHz mono 16-bit WAV."""
    command = ffmpeg_command(
        "-y",
        *("-ss", f"{interval.start_s:.6f}", "-t", f"{interval.duration_s:.6f}"),
        *("-i", source, "-map", "0:a:0", "-vn"),
        *("-ac", "1", "-ar", str(SAMPLE_RATE_HZ), "-c:a", "pcm_s16le", wav),
    )
    run_command(command, timeout_s=max(60.0, 4 * interval.duration_s), runner=runner)
    return wav


def caption_lines_for(
    source: Path,
    interval: Interval,
    transcriber: Transcriber,
    work_dir: Path,
    *,
    has_audio: bool = True,
    runner: Runner | None = None,
) -> list[CaptionLine]:
    """Transcribe `interval` of `source` and group it into caption lines (clip time)."""
    if not has_audio:
        return []
    try:
        wav = extract_audio(source, interval, work_dir / "clip.wav", runner=runner)
    except FFmpegError as exc:
        raise CaptioningError(f"could not extract the clip's audio for captions: {exc}") from exc
    words = [
        w for w in transcriber(wav) if w.start_s < interval.duration_s
    ]  # nothing past the clip's end
    lines = group_words(words)
    return [
        CaptionLine(line.text, line.start_s, min(line.end_s, interval.duration_s)) for line in lines
    ]


@functools.cache
def transcriber_for(model_size: str) -> FasterWhisperTranscriber:
    """One shared `FasterWhisperTranscriber` per model size, so the model loads once per process."""
    return FasterWhisperTranscriber(model_size)


def export_clip_with_captions(
    request: ExportRequest,
    *,
    transcriber: Transcriber | None = None,
    settings: EncodeSettings | None = None,
    runner: Runner | None = None,
    has_audio: Callable[[Path], bool] | None = None,
) -> ExportResult:
    """Export `request` with burned-in captions of its speech.

    Args:
        request: As for `export_clip`.
        transcriber: Defaults to the shared faster-whisper transcriber for the model set with
            ``AUTOCLIPPER_WHISPER_MODEL`` (`transcriber_for`).
        settings: Encoder settings.
        runner: Executes FFmpeg and FFprobe.
        has_audio: Whether the source has an audio stream; defaults to probing it. A source
            without audio is exported without captions.

    Raises:
        CaptioningError: The audio couldn't be extracted or transcribed; nothing is written
            (the contract never delivers an uncaptioned file when captions were asked for).
        The exceptions of `export_clip`.
    """
    transcribe = (
        transcriber if transcriber is not None else transcriber_for(get_settings().whisper_model)
    )
    audio = (
        has_audio(request.source)
        if has_audio is not None
        else bool(probe(request.source, runner=runner).audio_streams)
    )
    with tempfile.TemporaryDirectory(prefix="autoclipper-captions-") as temp:
        work = Path(temp)

        def filters(actual: Interval) -> list[str]:
            lines = caption_lines_for(
                request.source, actual, transcribe, work, has_audio=audio, runner=runner
            )
            log.info("captions lines=%d start_s=%.3f", len(lines), actual.start_s)
            if not lines:
                return []
            ass = work / "captions.ass"
            ass.write_text(ass_subtitles(lines), encoding="utf-8")
            return [subtitles_filter(ass)]

        result = export_clip(request, settings=settings, filters_for_cut=filters, runner=runner)
        with contextlib.suppress(OSError):  # TemporaryDirectory removes the rest
            (work / "clip.wav").unlink(missing_ok=True)
    return result
