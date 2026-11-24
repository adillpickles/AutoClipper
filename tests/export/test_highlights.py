"""Tests for the highlights video (#71): resolution, order, duration, fades, captions."""

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from autoclipper.analysis import (
    AnalysisResult,
    AnalysisWarning,
    Candidate,
    ScorerInfo,
    VodSummary,
)
from autoclipper.cli import ExitCode, app
from autoclipper.export.captions import Word
from autoclipper.export.encode import EncodeSettings
from autoclipper.export.errors import ExportError
from autoclipper.export.highlights import (
    HIGHLIGHTS_HEIGHT,
    HIGHLIGHTS_WIDTH,
    MAX_HIGHLIGHTS_SEGMENTS,
    MAX_SEGMENT_DURATION_S,
    HighlightsRequest,
    InvalidHighlightsBoundsError,
    Segment,
    render_highlights,
    resolve_segments,
)
from autoclipper.ffmpeg import ffmpeg_command, probe, run_command

pytestmark = pytest.mark.ffmpeg

TIMEOUT_S = 240.0
TOLERANCE_S = EncodeSettings().duration_tolerance_s

# A luma ramp, like tests/export/test_export_ffmpeg.py: every frame's brightness (Y, 16-235)
# encodes its source time, so a cut's exactness, its order and its fades all read back
# from the output's pixels. Lossless encoding keeps the values exact.
LUMA_AT_0 = 16
LUMA_PER_S = 10
RAMP_DURATION_S = 20.0
LOSSLESS = EncodeSettings(crf=0, preset="ultrafast")


def make_ramp_video(path: Path, width: int = 320, height: int = 180) -> Path:
    """A silent 16:9 (or other) video whose brightness rises with time, 20 s."""
    ramp = (
        f"nullsrc=size={width}x{height}:rate=25:duration={RAMP_DURATION_S},"
        f"geq=lum='{LUMA_AT_0}+{LUMA_PER_S}*T':cb=128:cr=128"
    )
    run_command(
        ffmpeg_command("-n", "-f", "lavfi", "-i", ramp, "-c:v", "libx264", "-qp", "0", path),
        timeout_s=TIMEOUT_S,
    )
    return path


def frame_luma(video: Path, index: int, crop_x: int = 0, crop_y: int = 0) -> float:
    """The luma (Y, 16-235) of frame `index`, averaged over a 16x16 block at (crop_x, crop_y)."""
    pixels = video.with_name(f"{video.stem}-{index}-{crop_x}-{crop_y}.yuv")
    run_command(
        ffmpeg_command(
            "-n",
            "-i",
            video,
            "-vf",
            f"select=eq(n\\,{index}),crop=16:16:{crop_x}:{crop_y}",
            "-frames:v",
            "1",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "yuv420p",
            pixels,
        ),
        timeout_s=TIMEOUT_S,
    )
    return sum(pixels.read_bytes()[:256]) / 256


def black_video_with_tone(path: Path, seconds: float = 12.0) -> Path:
    """A black 16:9 video carrying a 440 Hz tone, like tests/export/test_captions.py's."""
    command = ffmpeg_command(
        "-y",
        *("-f", "lavfi", "-i", f"color=black:size=640x360:rate=25:duration={seconds}"),
        *(
            "-f",
            "lavfi",
            "-i",
            f"aevalsrc='0.5*sin(2*PI*440*t)':sample_rate=48000:duration={seconds}",
        ),
        *("-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac"),
        "-shortest",
        path,
    )
    run_command(command, timeout_s=TIMEOUT_S)
    return path


def bright_pixels(video: Path, at_s: float, tmp_path: Path) -> int:
    """Pixels brighter than 200 in the lower half of the frame at `at_s` (white caption text)."""
    gray = tmp_path / f"frame-{at_s:.2f}.gray"
    run_command(
        ffmpeg_command(
            "-y",
            *("-ss", f"{at_s:.3f}", "-i", video, "-frames:v", "1"),
            *("-vf", "crop=iw:ih/2:0:ih/2,format=gray", "-f", "rawvideo", gray),
        ),
        timeout_s=TIMEOUT_S,
    )
    return sum(1 for value in gray.read_bytes() if value > 200)


def analysis_result(
    candidates: list[tuple[str, float, float]],
    *,
    duration_s: float = RAMP_DURATION_S,
) -> AnalysisResult:
    """A minimal valid AnalysisResult whose candidates are given in rank order (best first)."""
    return AnalysisResult(
        generated_at=datetime(2026, 11, 19, 12, 0, tzinfo=UTC),
        vod=VodSummary(
            video_id="9000000100",
            title="Synthetic test stream",
            broadcaster_id="9000000000",
            broadcaster_display_name="Streamer_000",
            duration_s=duration_s,
            created_at=datetime(2026, 10, 1, 18, 0, tzinfo=UTC),
        ),
        scorer=ScorerInfo(kind="heuristic", name="chat_loudness"),
        n_viewer_clips_total=0,
        warnings=(AnalysisWarning(code="chat_missing", message="x"),),
        candidates=tuple(
            Candidate(
                candidate_id=candidate_id,
                rank=rank,
                score=float(-rank),
                start_s=start_s,
                end_s=end_s,
                source="discovered",
                n_viewer_clips=0,
                top_signals=(),
            )
            for rank, (candidate_id, start_s, end_s) in enumerate(candidates, start=1)
        ),
    )


# --- Resolving and ordering (§ 7.1) --------------------------------------------------------------


def test_resolution_orders_generated_and_explicit_segments() -> None:
    # The best candidate starts later in the stream, so rank and stream order differ.
    result = analysis_result([("c002", 6.0, 9.0), ("c001", 1.0, 4.0)])

    chronological = resolve_segments(result, count=2, segments=None, order="chronological")
    assert [s.candidate_id for s in chronological] == ["c001", "c002"]

    rank = resolve_segments(result, count=1, segments=None, order="rank")
    assert [(s.candidate_id, s.start_s, s.end_s) for s in rank] == [("c002", 6.0, 9.0)]

    explicit = resolve_segments(
        result,
        count=2,
        segments=[Segment("c002", 6.5, 8.0), Segment("c001", 1.0, 4.0)],
        order="chronological",
    )
    assert [(s.candidate_id, s.start_s) for s in explicit] == [("c001", 1.0), ("c002", 6.5)]


def test_resolution_rejects_unknown_candidates_and_empty_results() -> None:
    result = analysis_result([("c001", 1.0, 4.0)])

    with pytest.raises(ValueError, match="c009"):
        resolve_segments(result, count=2, segments=[Segment("c009", 1.0, 2.0)], order="rank")

    empty = analysis_result([])
    with pytest.raises(InvalidHighlightsBoundsError) as excinfo:
        resolve_segments(empty, count=2, segments=None, order="chronological")
    assert excinfo.value.details == {"segment_index": None}


def test_resolution_rejects_segments_outside_the_vod() -> None:
    """A trim must lie inside the whole VOD (§ 7.1's export bounds), named by its index in
    the ordered list."""
    result = analysis_result([("c001", 1.0, 4.0)])  # a 20 s VOD

    beyond = [Segment("c001", 5.0, 21.0)]  # ends after the VOD
    with pytest.raises(InvalidHighlightsBoundsError) as excinfo:
        resolve_segments(result, count=2, segments=beyond, order="chronological")
    assert excinfo.value.details["segment_index"] == 0
    assert "0-20 s" in str(excinfo.value)

    negative = [Segment("c001", 1.0, 2.0), Segment("c001", -1.0, 2.0)]
    with pytest.raises(InvalidHighlightsBoundsError) as negative_info:
        resolve_segments(result, count=2, segments=negative, order="chronological")
    # Ordering sorts the -1.0 start first, so that is the first offender.
    assert negative_info.value.details["segment_index"] == 0


def test_resolution_caps_explicit_segments_at_twenty() -> None:
    result = analysis_result([("c001", 1.0, 4.0)])

    with pytest.raises(InvalidHighlightsBoundsError, match="1-20"):
        resolve_segments(
            result,
            count=2,
            segments=[Segment("c001", 1.0, 1.5)] * (MAX_HIGHLIGHTS_SEGMENTS + 1),
            order="rank",
        )


def test_rank_order_keeps_supplied_order_between_equal_keys() -> None:
    """§ 7.1 sorts by (rank, start_s); segments that tie on both keep their supplied order
    (no end_s tie breaker)."""
    result = analysis_result([("c001", 1.0, 4.0)])

    resolved = resolve_segments(
        result,
        count=2,
        segments=[
            Segment("c001", 2.0, 2.5),
            Segment("c001", 2.0, 3.0),
        ],
        order="rank",
    )

    assert [s.end_s for s in resolved] == [2.5, 3.0]


def test_bounds_name_the_offending_segment_and_the_limits() -> None:
    too_long = [Segment("c001", 0.0, MAX_SEGMENT_DURATION_S + 0.5)]
    with pytest.raises(InvalidHighlightsBoundsError) as excinfo:
        render_highlights(
            HighlightsRequest(
                source=Path("never-opened.mp4"), segments=too_long, output=Path("out.mp4")
            )
        )
    assert excinfo.value.details == {
        "segment_index": 0,
        "max_duration_s": MAX_SEGMENT_DURATION_S,
        "max_total_duration_s": 900.0,
    }

    # Every segment fits; together they don't. Nothing is opened to find out.
    total_too_long = [Segment(f"c{i:02d}", i * 90.0, i * 90.0 + 82.0) for i in range(11)]
    with pytest.raises(InvalidHighlightsBoundsError) as total:
        render_highlights(
            HighlightsRequest(
                source=Path("never-opened.mp4"),
                segments=total_too_long,
                output=Path("out.mp4"),
            )
        )
    assert total.value.details["segment_index"] is None
    assert total.value.details["max_total_duration_s"] == 900.0


# --- Rendering (order, duration, resolution, fades) -----------------------------------------------


def test_rank_order_cuts_each_segment_exactly_and_fades_between_them(
    tmp_path: Path,
) -> None:
    """The segments appear in the requested order, each starting exactly where asked, with
    a 0.5 s fade through black at the join and the whole video lasting their sum."""
    source = make_ramp_video(tmp_path / "ramp.mp4")
    # The best candidate (c002) starts later, so rank order is the reverse of stream order.
    result = analysis_result([("c002", 6.0, 9.0), ("c001", 1.0, 4.0)])
    chosen = resolve_segments(result, count=2, segments=None, order="rank")

    rendered = render_highlights(
        HighlightsRequest(
            source=source, segments=chosen, output=tmp_path / "out.mp4", order="rank"
        ),
        settings=LOSSLESS,
    )

    assert (rendered.width, rendered.height) == (HIGHLIGHTS_WIDTH, HIGHLIGHTS_HEIGHT)
    assert (rendered.video_codec, rendered.audio_codec) == ("h264", "aac")
    assert [s.candidate_id for s in rendered.segments] == ["c002", "c001"]
    frame_rate = LOSSLESS.frame_rate
    assert rendered.duration_s == pytest.approx(6.0, abs=2 * TOLERANCE_S)

    # Segment 1 (c002) starts exactly at 6.0 s: frame 0 shows the source's ramp there.
    assert frame_luma(rendered.output, 0) == pytest.approx(LUMA_AT_0 + LUMA_PER_S * 6.0, abs=1)
    # Mid-segment, unfaded: 1.5 s into a 3 s segment starting at 6.0.
    assert frame_luma(rendered.output, int(1.5 * frame_rate)) == pytest.approx(
        LUMA_AT_0 + LUMA_PER_S * 7.5, abs=1
    )

    # The last 0.25 s of segment 1 fades to black; the boundary frame is fully dark.
    boundary = int(3.0 * frame_rate)
    assert frame_luma(rendered.output, boundary - 1) < LUMA_AT_0 + LUMA_PER_S * 6.0 + 10
    assert frame_luma(rendered.output, boundary) == pytest.approx(LUMA_AT_0, abs=2)
    # The first 0.25 s of segment 2 fades back in; after it, its own exact start (1.0 s).
    assert frame_luma(rendered.output, int(3.3 * frame_rate)) == pytest.approx(
        LUMA_AT_0 + LUMA_PER_S * 1.3, abs=1
    )
    # The video's very start and end are not faded (only neighbours fade).
    assert frame_luma(rendered.output, int(5.7 * frame_rate)) == pytest.approx(
        LUMA_AT_0 + LUMA_PER_S * 3.7, abs=1
    )


def test_a_non_16_9_source_is_letterboxed_not_stretched(tmp_path: Path) -> None:
    """A 4:3 source keeps its shape: pillarboxed with black bars left and right."""
    source = make_ramp_video(tmp_path / "ramp.mp4", width=320, height=240)
    chosen = [Segment("c001", 1.0, 4.0)]

    rendered = render_highlights(
        HighlightsRequest(source=source, segments=chosen, output=tmp_path / "out.mp4"),
        settings=LOSSLESS,
    )

    assert (rendered.width, rendered.height) == (HIGHLIGHTS_WIDTH, HIGHLIGHTS_HEIGHT)
    # The bars: black columns where the 4:3 frame doesn't reach (scaled to 1440x1080).
    assert frame_luma(rendered.output, int(1.5 * LOSSLESS.frame_rate)) == pytest.approx(16, abs=2)
    # The content: the ramp's value at 2.5 s, in the middle of the frame.
    assert frame_luma(
        rendered.output, int(1.5 * LOSSLESS.frame_rate), crop_x=952, crop_y=532
    ) == pytest.approx(LUMA_AT_0 + LUMA_PER_S * 2.5, abs=1)


def test_captions_are_burned_into_each_segment(tmp_path: Path) -> None:
    """With captions on, each segment's speech is burned into the lower half of the 16:9
    frame at its own time in the video; nothing shows where nobody speaks."""

    class FakeTranscriber:
        def __call__(self, wav: Path) -> list[Word]:
            return [Word(start_s=0.4, end_s=1.4, text="HIGHLIGHTS CAPTION TEST")]

    source = black_video_with_tone(tmp_path / "vod.mp4")
    chosen = [Segment("c001", 1.0, 4.0), Segment("c002", 6.0, 9.0)]

    rendered = render_highlights(
        HighlightsRequest(
            source=source, segments=chosen, output=tmp_path / "out.mp4", captions=True
        ),
        transcriber=FakeTranscriber(),
    )

    # The word at 0.4-1.4 s of each 3 s segment: 0.4-1.4 and 3.4-4.4 in the video.
    assert bright_pixels(rendered.output, 0.8, tmp_path) > 1000
    assert bright_pixels(rendered.output, 3.8, tmp_path) > 1000
    assert bright_pixels(rendered.output, 2.6, tmp_path) == 0


def test_captions_fade_out_with_the_picture_at_a_join(tmp_path: Path) -> None:
    """A caption that runs to the end of a segment dims with the fade through black instead
    of staying bright over the transition (the fade applies after the subtitles)."""

    class LongTranscriber:
        def __call__(self, wav: Path) -> list[Word]:
            return [Word(start_s=0.2, end_s=3.0, text="WORDS ALL THE WAY TO THE END")]

    source = black_video_with_tone(tmp_path / "vod.mp4")
    chosen = [Segment("c001", 1.0, 4.0), Segment("c002", 6.0, 9.0)]

    rendered = render_highlights(
        HighlightsRequest(
            source=source, segments=chosen, output=tmp_path / "out.mp4", captions=True
        ),
        transcriber=LongTranscriber(),
    )

    # Mid-segment the caption is bright; at the segment's last frames the fade has taken
    # the caption down with the picture (~13% brightness at the boundary).
    assert bright_pixels(rendered.output, 1.5, tmp_path) > 1000
    faded = bright_pixels(rendered.output, 2.967, tmp_path)
    assert faded < 100  # mostly dark: the caption went out with the picture


def test_a_segment_the_video_does_not_cover_is_invalid(
    tmp_path: Path, make_test_media: Callable[..., Path]
) -> None:
    source = make_test_media("vod.mp4", duration_s=6.0, video_size=(640, 360))
    outside = [Segment("c001", 10.0, 12.0)]  # past the 6 s video

    with pytest.raises(InvalidHighlightsBoundsError, match="isn't covered"):
        render_highlights(
            HighlightsRequest(source=source, segments=outside, output=tmp_path / "out.mp4")
        )


def test_a_source_without_video_is_rejected(
    tmp_path: Path, make_test_media: Callable[..., Path]
) -> None:
    audio_only = make_test_media("voice.m4a", duration_s=6.0, video_size=None)

    with pytest.raises(ExportError, match="no video stream"):
        render_highlights(
            HighlightsRequest(
                source=audio_only,
                segments=[Segment("c001", 0.0, 2.0)],
                output=tmp_path / "out.mp4",
            )
        )


def test_the_output_can_never_be_the_source(
    tmp_path: Path, make_test_media: Callable[..., Path]
) -> None:
    """--out pointing at the VOD itself is refused: overwriting it would destroy the VOD."""
    source = make_test_media("vod.mp4", duration_s=6.0, video_size=(640, 360))

    with pytest.raises(ExportError, match="the output path is the source file"):
        render_highlights(
            HighlightsRequest(
                source=source,
                segments=[Segment("c001", 1.0, 3.0)],
                output=source,
                overwrite=True,
            )
        )
    assert source.stat().st_size > 0  # the VOD is untouched


def test_mixed_violations_report_the_first_ordered_offender() -> None:
    """Segment 0 over the 90 s maximum and segment 1 outside the VOD: the details name
    segment 0, whichever rule it breaks."""
    result = analysis_result([("c001", 0.0, 4.0), ("c002", 6.0, 9.0)], duration_s=120.0)

    with pytest.raises(InvalidHighlightsBoundsError) as excinfo:
        resolve_segments(
            result,
            count=2,
            segments=[Segment("c001", 0.0, 91.0), Segment("c002", 119.0, 121.0)],
            order="chronological",
        )

    assert excinfo.value.details["segment_index"] == 0
    assert "at most 90" in str(excinfo.value)


def _reset_source(tmp_path: Path) -> Path:
    """Two MPEG-TS segments joined byte for byte, whose timestamps restart halfway
    (the shape of tests/export/test_timeline.py's fixture)."""
    first = tmp_path / "part0.ts"
    second = tmp_path / "part1.ts"
    for path, color, tone in ((first, "black", 440), (second, "white", 880)):
        run_command(
            ffmpeg_command(
                "-y",
                *("-f", "lavfi", "-i", f"color={color}:size=320x180:rate=25:duration=4"),
                *("-f", "lavfi", "-i", f"sine=frequency={tone}:sample_rate=48000:duration=4"),
                *("-c:v", "libx264", "-preset", "ultrafast", "-g", "50", "-c:a", "aac"),
                *("-output_ts_offset", "0", "-f", "mpegts", path),
            ),
            timeout_s=TIMEOUT_S,
        )
    source = tmp_path / "reset.ts"
    source.write_bytes(first.read_bytes() + second.read_bytes())
    return source


def test_a_timestamp_reset_source_cuts_exactly(tmp_path: Path) -> None:
    """A source whose timestamps restart is cut on the corrected timeline, and the
    corrected times' float noise doesn't reject a valid segment (one frame of slack)."""
    source = _reset_source(tmp_path)
    segment = Segment("c001", 3.169, 3.879)  # the corrected end carries float noise

    rendered = render_highlights(
        HighlightsRequest(source=source, segments=[segment], output=tmp_path / "out.mp4"),
        settings=LOSSLESS,
    )

    assert rendered.duration_s == pytest.approx(0.71, abs=2 * TOLERANCE_S)


def test_captions_on_a_silent_source_render_without_speech(
    tmp_path: Path, make_test_media: Callable[..., Path]
) -> None:
    """A source with no audio stream has nothing to caption; the render succeeds rather
    than failing to extract a nonexistent stream."""

    class NeverTranscriber:
        def __call__(self, wav: Path) -> list[Word]:
            raise AssertionError("nothing to transcribe in a silent source")

    source = make_ramp_video(tmp_path / "silent.mp4")  # the ramp is silent video

    rendered = render_highlights(
        HighlightsRequest(
            source=source,
            segments=[Segment("c001", 1.0, 4.0)],
            output=tmp_path / "out.mp4",
            captions=True,
        ),
        transcriber=NeverTranscriber(),  # type: ignore[arg-type]
    )

    assert rendered.duration_s == pytest.approx(3.0, abs=2 * TOLERANCE_S)
    assert bright_pixels(rendered.output, 1.5, tmp_path) == 0  # no captions burned


def test_a_covered_quarter_second_segment_renders(
    tmp_path: Path, make_test_media: Callable[..., Path]
) -> None:
    """The contract bounds a segment only by start_s < end_s (§ 7.1): a fully covered
    0.25 s moment renders, without the clip exporter's 0.5 s floor."""
    source = make_test_media("vod.mp4", duration_s=6.0, video_size=(640, 360))

    rendered = render_highlights(
        HighlightsRequest(
            source=source,
            segments=[Segment("c001", 1.0, 1.25)],
            output=tmp_path / "out.mp4",
        ),
        settings=LOSSLESS,
    )

    assert rendered.duration_s == pytest.approx(0.25, abs=2 * TOLERANCE_S)


def test_render_highlights_cuts_and_captions_per_segment_sources(tmp_path: Path) -> None:
    """When segments carry their own `source` files, each segment is cut (and captioned)
    from its own file rather than `request.source`."""
    dark = tmp_path / "seg-a.mp4"
    bright = tmp_path / "seg-b.mp4"
    for path, luma, freq in ((dark, 40, 440), (bright, 200, 880)):
        run_command(
            ffmpeg_command(
                "-n",
                "-f",
                "lavfi",
                "-i",
                f"nullsrc=size=320x180:rate=25:duration=4,geq=lum='{luma}':cb=128:cr=128",
                "-f",
                "lavfi",
                "-i",
                f"sine=frequency={freq}:sample_rate=48000:duration=4",
                "-c:v",
                "libx264",
                "-qp",
                "0",
                "-c:a",
                "aac",
                "-shortest",
                path,
            ),
            timeout_s=TIMEOUT_S,
        )

    transcribed_peaks_hz: list[float] = []

    class FakeTranscriber:
        def __call__(self, wav: Path) -> list[Word]:
            import wave

            import numpy as np

            with wave.open(str(wav), "rb") as reader:
                rate = reader.getframerate()
                samples = np.frombuffer(reader.readframes(reader.getnframes()), dtype=np.int16)
            spectrum = np.abs(np.fft.rfft(samples.astype(np.float64)))
            freqs = np.fft.rfftfreq(len(samples), d=1.0 / rate)
            transcribed_peaks_hz.append(float(freqs[int(np.argmax(spectrum))]))
            return [Word(text="hello", start_s=0.5, end_s=1.5)]

    rendered = render_highlights(
        HighlightsRequest(
            source=dark,
            segments=[
                Segment("c001", 0.5, 2.5, source=dark),
                Segment("c002", 1.0, 3.0, source=bright),
            ],
            output=tmp_path / "out.mp4",
            captions=True,
        ),
        transcriber=FakeTranscriber(),  # type: ignore[arg-type]
        settings=LOSSLESS,
    )

    assert rendered.duration_s == pytest.approx(4.0, abs=3 * TOLERANCE_S)
    # Midpoint of segment 0 (t=1.0 s -> frame 30 at 30 fps) is dark (luma ~40);
    # midpoint of segment 1 (t=3.0 s -> frame 90 at 30 fps) is bright (luma ~200).
    assert frame_luma(rendered.output, 30) == pytest.approx(40.0, abs=4.0)
    assert frame_luma(rendered.output, 90) == pytest.approx(200.0, abs=4.0)
    # Each segment's captions must transcribe that segment's own audio (440 Hz then 880 Hz).
    assert len(transcribed_peaks_hz) == 2
    assert transcribed_peaks_hz[0] == pytest.approx(440.0, abs=5.0)
    assert transcribed_peaks_hz[1] == pytest.approx(880.0, abs=5.0)

    with pytest.raises(FileNotFoundError):
        render_highlights(
            HighlightsRequest(
                source=dark,
                segments=[
                    Segment("c001", 0.5, 2.0, source=dark),
                    Segment("c002", 0.5, 2.0, source=tmp_path / "missing.mp4"),
                ],
                output=tmp_path / "missing-out.mp4",
            )
        )


# --- The CLI ----------------------------------------------------------------------------------


runner = CliRunner()


def test_the_cli_rejects_missing_inputs(
    tmp_path: Path, make_test_media: Callable[..., Path]
) -> None:
    video = make_test_media("vod.mp4", duration_s=120.0)
    missing_result = runner.invoke(app, ["highlights", "missing.json", "--video", str(video)])
    result = tmp_path / "result.json"
    result.write_text("{}", encoding="utf-8")
    bad_result = runner.invoke(app, ["highlights", str(result), "--video", str(video)])
    no_video = runner.invoke(app, ["highlights", str(result), "--video", "missing.mp4"])
    segments = tmp_path / "segments.json"
    segments.write_text(
        json.dumps({"segments": [{"candidate_id": "c001", "start_s": 0.0, "end_s": 91.0}]}),
        encoding="utf-8",
    )
    outside = tmp_path / "outside.json"
    outside.write_text(
        json.dumps({"segments": [{"candidate_id": "c001", "start_s": 100.0, "end_s": 121.0}]}),
        encoding="utf-8",
    )

    assert missing_result.exit_code == ExitCode.USER_ERROR
    assert bad_result.exit_code == ExitCode.USER_ERROR
    assert "not a valid analysis result" in bad_result.output
    assert no_video.exit_code == ExitCode.USER_ERROR
    assert "video file not found" in no_video.output

    analysis = analysis_result([("c001", 0.0, 3.0), ("c002", 3.5, 5.5)], duration_s=120.0)
    result.write_text(analysis.model_dump_json(), encoding="utf-8")
    bounds = runner.invoke(
        app,
        ["highlights", str(result), "--video", str(video), "--segments", str(segments)],
    )
    beyond_vod = runner.invoke(
        app,
        ["highlights", str(result), "--video", str(video), "--segments", str(outside)],
    )
    too_many = tmp_path / "too-many.json"
    too_many.write_text(
        json.dumps(
            {
                "segments": [
                    {"candidate_id": "c001", "start_s": float(i), "end_s": float(i) + 0.5}
                    for i in range(MAX_HIGHLIGHTS_SEGMENTS + 1)
                ]
            }
        ),
        encoding="utf-8",
    )
    twenty_one = runner.invoke(
        app,
        ["highlights", str(result), "--video", str(video), "--segments", str(too_many)],
    )

    assert bounds.exit_code == ExitCode.USER_ERROR
    assert "invalid_highlights_bounds" in bounds.output
    assert "at most 90" in bounds.output  # inside the VOD, over the per-segment maximum
    assert beyond_vod.exit_code == ExitCode.USER_ERROR
    assert "must be inside the VOD's 0-120 s" in beyond_vod.output
    assert twenty_one.exit_code == ExitCode.USER_ERROR
    assert "1-20" in twenty_one.output  # the segment-count cap (section 7.1)


def test_the_cli_writes_the_highlights_video(
    tmp_path: Path,
    make_test_media: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    video = make_test_media("vod.mp4", duration_s=6.0, video_size=(640, 360))
    result = analysis_result([("c002", 3.5, 5.5), ("c001", 1.0, 3.0)], duration_s=6.0)
    result_path = tmp_path / "result.json"
    result_path.write_text(result.model_dump_json(), encoding="utf-8")
    artifacts = tmp_path / "artifacts"
    monkeypatch.setenv("AUTOCLIPPER_ARTIFACTS_DIR", str(artifacts))

    done = runner.invoke(
        app,
        [
            "highlights",
            str(result_path),
            "--video",
            str(video),
            "--count",
            "2",
            "--overwrite",
        ],
    )

    assert done.exit_code == ExitCode.SUCCESS, done.output
    expected = artifacts / "highlights" / "autoclipper_9000000100_highlights.mp4"
    assert expected.is_file()
    info = probe(expected)
    assert (info.video_streams[0].width, info.video_streams[0].height) == (1920, 1080)
    assert info.duration_s == pytest.approx(4.0, abs=2 * TOLERANCE_S)
    assert "Wrote" in done.output

    # The default output name is fixed; a second run needs --overwrite.
    again = runner.invoke(
        app, ["highlights", str(result_path), "--video", str(video), "--count", "2"]
    )
    assert again.exit_code == ExitCode.USER_ERROR
    assert "exists" in again.output


def test_the_cli_accepts_a_source_whose_timestamps_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ffprobe reports only the first span of a reset source, so the video_mismatch check
    re-measures on the analysis timeline; a segment after the reset then renders."""
    video = _reset_source(tmp_path)  # two 4 s parts joined; ffprobe calls it ~4 s
    result = analysis_result([("c001", 5.0, 7.0)], duration_s=8.0)
    result_path = tmp_path / "result.json"
    result_path.write_text(result.model_dump_json(), encoding="utf-8")
    monkeypatch.setenv("AUTOCLIPPER_ARTIFACTS_DIR", str(tmp_path / "artifacts"))

    done = runner.invoke(app, ["highlights", str(result_path), "--video", str(video)])

    assert done.exit_code == ExitCode.SUCCESS, done.output
    expected = tmp_path / "artifacts" / "highlights" / "autoclipper_9000000100_highlights.mp4"
    assert expected.is_file()
    assert probe(expected).duration_s == pytest.approx(2.0, abs=2 * TOLERANCE_S)


def test_the_cli_reports_unexpected_failures_as_internal_error(
    tmp_path: Path,
    make_test_media: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bug in the renderer exits 3 with a generic message (§ 4.6)."""
    video = make_test_media("vod.mp4", duration_s=6.0)
    result = analysis_result([("c001", 1.0, 3.0)], duration_s=6.0)
    result_path = tmp_path / "result.json"
    result_path.write_text(result.model_dump_json(), encoding="utf-8")
    monkeypatch.setenv("AUTOCLIPPER_ARTIFACTS_DIR", str(tmp_path / "artifacts"))

    def broken(*args: object, **kwargs: object) -> object:
        raise RuntimeError("a bug")

    monkeypatch.setattr("autoclipper.export.highlights.render_highlights", broken)

    done = runner.invoke(app, ["highlights", str(result_path), "--video", str(video)])

    assert done.exit_code == ExitCode.INTERNAL_ERROR
    error_lines = [line for line in done.output.splitlines() if "internal_error" in line]
    assert error_lines
    assert all("a bug" not in line for line in error_lines)


def test_render_highlights_probes_regular_source_only_once_across_segments(
    tmp_path: Path, make_test_media: Callable[..., Path]
) -> None:
    from autoclipper.ffmpeg import CommandResult, FFmpegCommand, run_subprocess

    video = make_test_media("vod.mp4", duration_s=12.0)
    commands: list[FFmpegCommand] = []

    def recording_runner(command: FFmpegCommand, /, *, timeout_s: float) -> CommandResult:
        commands.append(command)
        return run_subprocess(command, timeout_s=timeout_s)

    segments = [
        Segment("c001", 1.0, 3.0),
        Segment("c002", 4.0, 6.0),
        Segment("c003", 7.0, 9.0),
    ]
    out = tmp_path / "highlights.mp4"
    render_highlights(
        HighlightsRequest(source=video, segments=segments, output=out),
        settings=LOSSLESS,
        runner=recording_runner,
    )

    source_probes = [
        c for c in commands if c.program == "ffprobe" and c.args[-1] == str(video.absolute())
    ]
    # 1 combined media+times probe + 1 stream_ends probe + 1 keyframe probe per segment (3).
    assert len(source_probes) == 2 + len(segments)


def test_render_highlights_reprobes_when_source_is_replaced_between_segments(
    tmp_path: Path, make_test_media: Callable[..., Path]
) -> None:
    """Replacing a video-only source with one that has audio between segments picks up the audio."""
    import numpy as np

    from autoclipper.ffmpeg import CommandResult, FFmpegCommand, run_subprocess

    source = tmp_path / "vod.mp4"
    run_command(
        ffmpeg_command(
            "-y",
            *("-f", "lavfi", "-i", "color=black:size=320x180:rate=25:duration=8"),
            *("-c:v", "libx264", "-preset", "ultrafast", "-g", "50", source),
        ),
        timeout_s=TIMEOUT_S,
    )
    with_audio = make_test_media("vod_with_audio.mp4", duration_s=8.0, tone_hz=880.0)
    swapped = False

    def swap_after_first_segment(command: FFmpegCommand, /, *, timeout_s: float) -> CommandResult:
        nonlocal swapped
        result = run_subprocess(command, timeout_s=timeout_s)
        if (
            not swapped
            and command.program == "ffprobe"
            and command.args[-1].endswith("segment-000.mp4")
        ):
            swapped = True
            with_audio.replace(source)
        return result

    out = tmp_path / "highlights.mp4"
    render_highlights(
        HighlightsRequest(
            source=source,
            segments=[Segment("c001", 1.0, 3.0), Segment("c002", 5.0, 7.0)],
            output=out,
        ),
        settings=LOSSLESS,
        runner=swap_after_first_segment,
    )

    assert swapped
    pcm = run_command(
        ffmpeg_command("-ss", "3", "-i", out, "-t", "0.5", "-vn", "-ac", "1", "-f", "s16le", "-"),
        timeout_s=TIMEOUT_S,
    ).stdout
    samples = np.frombuffer(pcm, dtype="<i2")
    assert int(np.abs(samples).max()) > 500
