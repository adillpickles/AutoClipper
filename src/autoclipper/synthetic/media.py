"""Synthetic media: a 16 kHz mono WAV per VOD (numpy) and a test-pattern video (FFmpeg).

Audio is synthesized in Python rather than with FFmpeg filters so that its content is exact and
checkable: bursts start where the scenario says, the muted segment is digital silence, and the
bytes depend only on the seed (and the numpy build), not on the FFmpeg version.
"""

import math
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from autoclipper.ffmpeg import FFmpegCommand, ffmpeg_command
from autoclipper.synthetic.scenario import VOD_PLANS, SyntheticVod

SAMPLE_RATE_HZ = 16_000
"""Sample rate of the synthetic WAVs, matching ``data/interim/audio`` (16 kHz mono 16-bit PCM)."""

NOISE_LEVEL = 0.01
"""RMS of the background noise, as a fraction of full scale (about -40 dBFS)."""


@dataclass(frozen=True, slots=True)
class VideoOptions:
    """How to generate each VOD's video (an FFmpeg ``testsrc2`` pattern with the VOD's audio).

    Attributes:
        duration_s: Video length in seconds; None means the VOD's own duration. A shorter value
            keeps tests fast; a longer one pads the audio with silence.
        size: ``(width, height)`` in pixels; both must be even (H.264 with yuv420p).
        frame_rate: Frames per second.
    """

    duration_s: float | None = None
    size: tuple[int, int] = (320, 180)
    frame_rate: int = 10

    def __post_init__(self) -> None:
        if self.duration_s is not None and not (
            math.isfinite(self.duration_s) and self.duration_s > 0
        ):
            raise ValueError(f"video duration_s must be positive, got {self.duration_s!r}")
        if any(side <= 0 or side % 2 for side in self.size):
            raise ValueError(f"video size must be positive even numbers, got {self.size!r}")
        if self.frame_rate <= 0:
            raise ValueError(f"video frame_rate must be positive, got {self.frame_rate!r}")


def render_audio(vod: SyntheticVod, seed: int) -> np.ndarray:
    """Synthesize a VOD's audio as 16-bit samples at `SAMPLE_RATE_HZ`.

    Background noise, a voice-like tone that talks in stretches of a few seconds, a loud burst
    at every planted highlight (``audio_burst_s``) and exact zeros in the muted segment.

    Returns:
        A 1-D little-endian ``int16`` array of ``round(duration_s * SAMPLE_RATE_HZ)`` samples.
    """
    rng = np.random.default_rng([seed, VOD_PLANS.index(vod.plan)])
    n_samples = round(vod.duration_s * SAMPLE_RATE_HZ)
    # A 150 Hz voice with a harmonic, modulated at a syllable rate of 4 Hz, and a 600 Hz scream.
    # Both repeat every 0.5 s, so one period is computed and tiled.
    phase = 2 * np.pi * 150.0 * np.arange(SAMPLE_RATE_HZ // 2) / SAMPLE_RATE_HZ
    voice_period = (np.sin(phase) + 0.5 * np.sin(2 * phase)) * (0.5 + 0.5 * np.sin(phase / 37.5))
    voice = np.resize(voice_period.astype(np.float32), n_samples)
    scream = np.resize(np.sin(4 * phase).astype(np.float32), n_samples)
    talking = np.zeros(n_samples, dtype=np.float32)
    position_s = float(rng.uniform(0, 2))
    while position_s < vod.duration_s:
        length_s = float(rng.uniform(2, 8))
        talking[_samples(position_s) : _samples(position_s + length_s)] = 0.08
        position_s += length_s + float(rng.uniform(1, 4))
    audio = rng.standard_normal(n_samples, dtype=np.float32) * NOISE_LEVEL + voice * talking
    for event in vod.events:
        start, end = (_samples(t) for t in event.audio_burst_s)
        t = np.arange(end - start) / SAMPLE_RATE_HZ
        envelope = 0.8 * event.strength * np.exp(-t / ((end - start) / SAMPLE_RATE_HZ / 2))
        envelope[: _samples(0.02)] *= np.linspace(0, 1, _samples(0.02))  # 20 ms attack
        burst = 0.6 * rng.standard_normal(end - start) + scream[start:end]
        audio[start:end] += (envelope * burst).astype(np.float32)
    if vod.plan.muted is not None:
        offset_s, duration_s = vod.plan.muted
        audio[_samples(offset_s) : _samples(offset_s + duration_s)] = 0.0
    return np.round(np.clip(audio, -1.0, 1.0) * 32767).astype("<i2")


def write_wav(samples: np.ndarray, path: Path) -> None:
    """Write mono 16-bit PCM samples at `SAMPLE_RATE_HZ`. The bytes depend only on `samples`."""
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE_HZ)
        wav.writeframes(samples.astype("<i2").tobytes())


def video_command(
    audio: Path, output: Path, duration_s: float, options: VideoOptions
) -> FFmpegCommand:
    """Build the FFmpeg command for a test-pattern video carrying `audio` as AAC.

    ``testsrc2`` shows a running clock, so a preview shows which VOD second it is playing. The
    bitexact flags and dropped metadata keep FFmpeg from writing build or time information, so
    one FFmpeg build gives the same bytes on every run (not guaranteed across builds).
    """
    width, height = options.size
    pattern = f"testsrc2=size={width}x{height}:rate={options.frame_rate}:duration={duration_s}"
    return ffmpeg_command(
        "-n", "-f", "lavfi", "-i", pattern, "-i", audio,
        "-map", "0:v:0", "-map", "1:a:0", "-af", "apad", "-t", str(duration_s),
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "64k",
        "-map_metadata", "-1", "-fflags", "+bitexact", "-flags", "+bitexact",
        output,
    )  # fmt: skip


def _samples(seconds: float) -> int:
    return round(seconds * SAMPLE_RATE_HZ)
