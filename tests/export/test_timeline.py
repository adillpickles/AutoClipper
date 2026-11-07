"""Export and analysis share one timeline: timestamp resets, forward jumps and audio gaps.

The media is generated with FFmpeg at test time. "Analysis time" is what `normalize_audio` gives:
second t of its WAV. An export of [a, b) must show what plays at analysis time a to b.
"""

import wave
from pathlib import Path

import numpy as np
import pytest

from autoclipper.export.captions import (
    Word,
    export_clip_with_captions,
    extract_audio,
    read_wav,
)
from autoclipper.export.clip import ClipSpec, render_clip
from autoclipper.export.cut import Interval, IntervalTooShortError
from autoclipper.export.encode import EncodeSettings, ExportRequest, export_clip
from autoclipper.ffmpeg import ffmpeg_command, run_command
from autoclipper.ingest.media import normalize_audio

pytestmark = pytest.mark.ffmpeg

FAST = EncodeSettings(preset="ultrafast")


def segment(path: Path, color: str, tone_hz: int, *, ts_offset_s: float = 0.0) -> Path:
    """A 4 s MPEG-TS segment: a plain `color` frame and a `tone_hz` tone, keyframe every 2 s."""
    run_command(
        ffmpeg_command(
            "-y",
            *("-f", "lavfi", "-i", f"color={color}:size=320x180:rate=25:duration=4"),
            *("-f", "lavfi", "-i", f"sine=frequency={tone_hz}:sample_rate=48000:duration=4"),
            *("-c:v", "libx264", "-preset", "ultrafast", "-g", "50", "-c:a", "aac"),
            *("-output_ts_offset", f"{ts_offset_s}", "-f", "mpegts", path),
        ),
        timeout_s=60,
    )
    return path


def joined(tmp_path: Path, *, second_offset_s: float = 0.0) -> Path:
    """Two segments joined byte for byte: black with 440 Hz, then white with 880 Hz.

    With no offset both segments' timestamps start at the same value, so they restart halfway.
    """
    first = segment(tmp_path / "part0.ts", "black", 440)
    second = segment(tmp_path / "part1.ts", "white", 880, ts_offset_s=second_offset_s)
    source = tmp_path / "joined.ts"
    source.write_bytes(first.read_bytes() + second.read_bytes())
    return source


def mean_brightness(video: Path, at_s: float, tmp_path: Path) -> float:
    """Mean gray level (0-255) of the frame at `at_s`."""
    raw = tmp_path / f"frame-{at_s}.gray"
    run_command(
        ffmpeg_command(
            "-y",
            *("-ss", f"{at_s}", "-i", video, "-frames:v", "1"),
            *("-vf", "format=gray", "-f", "rawvideo", raw),
        ),
        timeout_s=60,
    )
    return float(np.frombuffer(raw.read_bytes(), dtype=np.uint8).mean())


def bright_pixels(video: Path, at_s: float, tmp_path: Path) -> int:
    """Pixels brighter than 200 in the frame at `at_s`."""
    raw = tmp_path / f"bright-{at_s}.gray"
    run_command(
        ffmpeg_command(
            "-y",
            *("-ss", f"{at_s}", "-i", video, "-frames:v", "1"),
            *("-vf", "format=gray", "-f", "rawvideo", raw),
        ),
        timeout_s=60,
    )
    return int((np.frombuffer(raw.read_bytes(), dtype=np.uint8) > 200).sum())


def decoded_audio(video: Path, tmp_path: Path, *, fill_gaps: bool = False) -> np.ndarray:
    """The file's audio at 16 kHz mono, sample by sample as stored (gaps closed unless filled)."""
    wav = tmp_path / f"{video.stem}-audio.wav"
    filters = ("-af", "aresample=async=1:first_pts=0") if fill_gaps else ()
    run_command(
        ffmpeg_command(
            "-y",
            *("-i", video, "-vn", *filters, "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", wav),
        ),
        timeout_s=60,
    )
    return read_wav(wav)


def dominant_hz(samples: np.ndarray, rate: int = 16_000) -> float:
    spectrum = np.abs(np.fft.rfft(samples * np.hanning(len(samples))))
    return float(np.fft.rfftfreq(len(samples), 1 / rate)[int(spectrum.argmax())])


def wav_seconds(path: Path) -> float:
    with wave.open(str(path), "rb") as audio:
        return audio.getnframes() / audio.getframerate()


# --- Timestamp resets ----------------------------------------------------------------------------


def test_footage_after_a_timestamp_reset_can_be_exported(tmp_path: Path) -> None:
    source = joined(tmp_path)
    analysis = normalize_audio(source, "9000000001", data_dir=tmp_path / "data")
    assert analysis.duration_s == pytest.approx(8.08, abs=0.1)  # resets joined, as for analysis

    result = render_clip(ClipSpec(source, Interval(6.0, 7.0), tmp_path / "clip.mp4"), settings=FAST)

    assert result.cut.actual == Interval(6.0, 7.0)
    assert result.duration_s == pytest.approx(1.0, abs=0.05)
    assert mean_brightness(result.output, 0.5, tmp_path) > 200  # the second, white segment
    assert dominant_hz(decoded_audio(result.output, tmp_path)) == pytest.approx(880, abs=20)


def test_a_clip_across_a_reset_matches_the_analysis_timeline(tmp_path: Path) -> None:
    source = joined(tmp_path)
    analysis = normalize_audio(source, "9000000001", data_dir=tmp_path / "data")
    samples = read_wav(analysis.path)
    # Where the analysis audio switches from 440 Hz to 880 Hz: a 0.1 s window either side of the
    # switch is dominated by one tone.
    step = 1600
    hz = [dominant_hz(samples[i : i + step]) for i in range(0, len(samples) - step, step)]
    switch_s = next(i for i, f in enumerate(hz) if f > 660) * step / 16_000

    result = export_clip(
        ExportRequest(source, Interval(3.0, 5.0), tmp_path / "clip.mp4"), settings=FAST
    )

    # The picture turns white where the analysis audio turns to 880 Hz (within a frame or two).
    before = switch_s - 3.0 - 0.15
    after = switch_s - 3.0 + 0.15
    assert mean_brightness(result.output, before, tmp_path) < 50
    assert mean_brightness(result.output, after, tmp_path) > 200
    clip_audio = decoded_audio(result.output, tmp_path)
    assert dominant_hz(clip_audio[: int(16_000 * (before - 0.05))]) == pytest.approx(440, abs=20)
    assert dominant_hz(clip_audio[int(16_000 * (after + 0.1)) :]) == pytest.approx(880, abs=20)


def test_caption_audio_after_a_reset_comes_from_the_right_segment(tmp_path: Path) -> None:
    source = joined(tmp_path)

    wav = extract_audio(source, Interval(6.0, 7.0), tmp_path / "clip.wav")

    assert dominant_hz(read_wav(wav)) == pytest.approx(880, abs=20)
    assert wav_seconds(wav) == pytest.approx(1.0, abs=0.05)


def test_a_reset_source_exports_from_its_very_start(tmp_path: Path) -> None:
    # The copy the cut comes from starts at the first keyframe, a little after the media start.
    source = joined(tmp_path)

    result = export_clip(
        ExportRequest(source, Interval(0.0, 1.0), tmp_path / "clip.mp4"), settings=FAST
    )
    wav = extract_audio(source, Interval(0.0, 1.0), tmp_path / "clip.wav")

    assert result.cut.actual.start_s == pytest.approx(0.0, abs=0.05)
    assert result.cut.actual.end_s == pytest.approx(1.0, abs=1e-6)
    assert dominant_hz(read_wav(wav)) == pytest.approx(440, abs=20)


def test_errors_on_a_reset_source_quote_the_requested_times(tmp_path: Path) -> None:
    source = joined(tmp_path)

    with pytest.raises(IntervalTooShortError, match=r"the interval 9-10 s"):
        export_clip(ExportRequest(source, Interval(9.0, 10.0), tmp_path / "c.mp4"), settings=FAST)


def test_sources_without_resets_are_used_as_they_are(tmp_path: Path) -> None:
    from autoclipper.export.timeline import read_reset_timeline

    plain = segment(tmp_path / "plain.ts", "black", 440)

    assert read_reset_timeline(plain) is None


# --- Forward jumps -----------------------------------------------------------------------------


def test_a_clip_across_a_long_forward_jump_keeps_the_gap_as_in_analysis(tmp_path: Path) -> None:
    # The second segment's timestamps start 15 s after the first's: an 11 s gap after 4 s.
    # FFmpeg's default would join a jump over 10 s; analysis keeps it as silence.
    source = joined(tmp_path, second_offset_s=15.0)
    analysis = normalize_audio(source, "9000000001", data_dir=tmp_path / "data")
    assert analysis.duration_s == pytest.approx(19.0, abs=0.2)

    result = render_clip(
        ClipSpec(source, Interval(3.0, 17.0), tmp_path / "clip.mp4"), settings=FAST
    )

    assert result.duration_s == pytest.approx(14.0, abs=0.06)
    assert mean_brightness(result.output, 6.0, tmp_path) < 50  # in the gap: still black
    assert mean_brightness(result.output, 13.5, tmp_path) > 200  # 16.5 s: the white segment
    clip_audio = decoded_audio(result.output, tmp_path)
    assert np.abs(clip_audio[16_000 * 2 : 16_000 * 11]).max() < 0.01  # the gap is silence
    assert dominant_hz(clip_audio[16_000 * 12 + 800 :]) == pytest.approx(880, abs=20)


# --- Audio gaps --------------------------------------------------------------------------------


def audio_gap_source(tmp_path: Path) -> Path:
    """6 s of black; silence, then a 440 Hz tone from 4 s; audio packets of 1-3 s dropped."""
    source = tmp_path / "audio-gap.mp4"
    tone = r"if(gte(t\,4)\,0.5*sin(2*PI*440*t)\,0)"
    run_command(
        ffmpeg_command(
            "-y",
            *("-f", "lavfi", "-i", "color=black:size=320x180:rate=25:duration=6"),
            *("-f", "lavfi", "-i", f"aevalsrc={tone}:s=48000:d=6"),
            *("-af", r"aselect=not(between(t\,1\,3))"),
            *("-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", source),
        ),
        timeout_s=60,
    )
    return source


def onset_s(samples: np.ndarray) -> float:
    return float(np.flatnonzero(np.abs(samples) > 0.1)[0]) / 16_000


def test_caption_audio_keeps_a_timestamp_gap_as_silence(tmp_path: Path) -> None:
    source = audio_gap_source(tmp_path)

    wav = extract_audio(source, Interval(0.0, 6.0), tmp_path / "clip.wav")

    assert onset_s(read_wav(wav)) == pytest.approx(4.0, abs=0.05)
    assert wav_seconds(wav) == pytest.approx(6.0, abs=0.05)


def test_captions_after_an_audio_gap_appear_when_the_word_is_spoken(tmp_path: Path) -> None:
    source = audio_gap_source(tmp_path)

    def hears_the_tone(wav: Path, /) -> list[Word]:
        start = onset_s(read_wav(wav))
        return [Word("NOW", start, start + 0.5)]

    result = export_clip_with_captions(
        ExportRequest(source, Interval(0.0, 6.0), tmp_path / "clip.mp4"),
        transcriber=hears_the_tone,
        settings=FAST,
    )

    # The caption is white text on a black frame.
    assert bright_pixels(result.output, 2.2, tmp_path) == 0
    assert bright_pixels(result.output, 4.2, tmp_path) > 1000


def test_an_exported_clip_keeps_an_audio_gap_as_real_silence(tmp_path: Path) -> None:
    source = audio_gap_source(tmp_path)

    result = export_clip(
        ExportRequest(source, Interval(0.0, 6.0), tmp_path / "clip.mp4"), settings=FAST
    )

    # Decoded sample by sample, without filling gaps: the silence must be in the file itself.
    assert onset_s(decoded_audio(result.output, tmp_path)) == pytest.approx(4.0, abs=0.05)
