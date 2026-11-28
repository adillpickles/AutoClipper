"""Tests for facecam detection (#191) and how the stacked export uses it.

Frames are generated in the test: gameplay is a dark, blocky pattern that changes every frame,
the webcam overlay is a bright, steady box, and the "face" is a patch inside it that changes a
little from frame to frame. A fake face detector reports the known face box, so the tests pin
down the overlay search, the consistency checks and the fallbacks, not the Haar cascade.
"""

from collections.abc import Callable, Sequence
from pathlib import Path

import numpy as np
import pytest
from typer.testing import CliRunner

from autoclipper.cli import ExitCode, app
from autoclipper.export import clip as clip_module
from autoclipper.export.clip import (
    ClipSpec,
    LayoutWarning,
    NoFacecamConfiguredError,
    render_clip,
)
from autoclipper.export.cut import Interval
from autoclipper.export.encode import ExportRequest
from autoclipper.export.facecam import (
    FaceBox,
    FacecamCache,
    FacecamDetection,
    FacecamStatus,
    FaceDetector,
    GrayFrame,
    Region,
    _padded_corner_box,  # pyright: ignore[reportPrivateUsage]
    detect_facecam,
    haar_face_detector,
    locate_facecam,
    read_gray_frame,
    sample_times,
)
from autoclipper.export.reframe import CenterCrop, StackedLayout, stacked_layout_from_region
from autoclipper.ffmpeg import CommandResult, FFmpegCommand, FFmpegExitError

WIDTH, HEIGHT = 640, 360
BOTTOM_LEFT_CAM: Region = (0.0, 0.6, 0.3, 0.4)
BOTTOM_LEFT_FACE: Region = (0.09, 0.64, 0.12, 0.2)


def _px(region: Region) -> FaceBox:
    x, y, w, h = region
    return (round(x * WIDTH), round(y * HEIGHT), round(w * WIDTH), round(h * HEIGHT))


def make_frames(
    *,
    count: int = 7,
    cam: Region | None = BOTTOM_LEFT_CAM,
    faces: Sequence[Region | None] | None = None,
    live: bool = True,
    seed: int = 7,
) -> tuple[list[GrayFrame], list[FaceBox | None]]:
    """`count` frames with a steady webcam box at `cam` and a face patch per frame.

    `faces` gives each frame's face (None: no face); by default the face sits at
    `BOTTOM_LEFT_FACE` in every frame. With `cam` None the whole frame is a steady camera.
    """
    rng = np.random.default_rng(seed)
    per_frame = list(faces) if faces is not None else [BOTTOM_LEFT_FACE] * count
    frames: list[GrayFrame] = []
    boxes: list[FaceBox | None] = []
    still_face = rng.integers(140, 255, size=(HEIGHT, WIDTH), dtype=np.uint8)
    for face in per_frame:
        if cam is None:
            frame = np.full((HEIGHT, WIDTH), 200, dtype=np.uint8)
        else:
            blocks = rng.integers(0, 80, size=(9, 16), dtype=np.uint8)
            frame = np.kron(blocks, np.ones((40, 40), dtype=np.uint8))
            cx, cy, cw, ch = _px(cam)
            frame[cy : cy + ch, cx : cx + cw] = 200
        if face is not None:
            fx, fy, fw, fh = _px(face)
            patch = (
                still_face[fy : fy + fh, fx : fx + fw]
                if not live
                else rng.integers(140, 255, size=(fh, fw), dtype=np.uint8)
            )
            frame[fy : fy + fh, fx : fx + fw] = patch
            boxes.append((fx, fy, fw, fh))
        else:
            boxes.append(None)
        frames.append(frame)
    return frames, boxes


def fake_detector(frames: Sequence[GrayFrame], boxes: Sequence[FaceBox | None]) -> FaceDetector:
    """A face detector that reports each generated frame's known face box."""
    by_id = {id(frame): box for frame, box in zip(frames, boxes, strict=True)}

    def detect(frame: GrayFrame) -> list[FaceBox]:
        box = by_id[id(frame)]
        return [] if box is None else [box]

    return detect


def assert_region(actual: Region | None, expected: Region, tolerance: float = 0.01) -> None:
    assert actual is not None
    assert actual == pytest.approx(expected, abs=tolerance)


# --- locate_facecam ----------------------------------------------------------------------------


def test_a_steady_face_in_a_corner_overlay_is_found_with_the_overlay_box() -> None:
    frames, boxes = make_frames()

    found = locate_facecam(frames, face_detector=fake_detector(frames, boxes))

    assert found.status == "found"
    assert_region(found.region, BOTTOM_LEFT_CAM)
    assert_region(found.face, BOTTOM_LEFT_FACE)
    assert (found.frames, found.face_frames) == (7, 7)


def test_an_overlay_away_from_the_frame_edges_is_bounded_by_its_borders() -> None:
    cam: Region = (0.55, 0.1, 0.3, 0.45)
    face: Region = (0.64, 0.18, 0.12, 0.2)
    frames, boxes = make_frames(cam=cam, faces=[face] * 7)

    found = locate_facecam(frames, face_detector=fake_detector(frames, boxes))

    assert found.status == "found"
    assert_region(found.region, cam)


def test_a_face_missed_in_a_couple_of_frames_is_still_found() -> None:
    faces = [BOTTOM_LEFT_FACE, None, BOTTOM_LEFT_FACE, BOTTOM_LEFT_FACE, None] + [
        BOTTOM_LEFT_FACE
    ] * 2
    frames, boxes = make_frames(faces=faces)

    found = locate_facecam(frames, face_detector=fake_detector(frames, boxes))

    assert found.status == "found"
    assert found.face_frames == 5
    assert_region(found.region, BOTTOM_LEFT_CAM)


def test_no_face_is_not_found() -> None:
    frames, _ = make_frames(faces=[None] * 7)

    found = locate_facecam(frames, face_detector=lambda _frame: [])

    assert found.status == "not_found"
    assert found.region is None


def test_no_frames_is_not_found() -> None:
    assert locate_facecam([]).status == "not_found"


def test_a_face_that_moves_between_frames_is_inconsistent() -> None:
    positions = [(0.05 + 0.12 * i, 0.1 + 0.08 * (i % 3), 0.1, 0.18) for i in range(7)]
    frames, boxes = make_frames(cam=None, faces=positions)

    found = locate_facecam(frames, face_detector=fake_detector(frames, boxes))

    assert found.status == "inconsistent"
    assert found.region is None


def test_a_face_in_only_some_frames_is_not_found() -> None:
    faces: list[Region | None] = [BOTTOM_LEFT_FACE, BOTTOM_LEFT_FACE] + [None] * 5
    frames, boxes = make_frames(faces=faces)

    found = locate_facecam(frames, face_detector=fake_detector(frames, boxes))

    assert found.status == "not_found"
    assert found.face_frames == 2


def test_a_large_face_is_a_full_screen_camera() -> None:
    big_face: Region = (0.35, 0.15, 0.3, 0.5)
    frames, boxes = make_frames(cam=None, faces=[big_face] * 7)

    found = locate_facecam(frames, face_detector=fake_detector(frames, boxes))

    assert found.status == "full_camera"
    assert found.region is None
    assert_region(found.face, big_face)


def test_a_small_steady_face_in_a_corner_without_a_border_gets_a_padded_corner_box() -> None:
    face: Region = (0.82, 0.68, 0.12, 0.2)
    frames, boxes = make_frames(cam=None, faces=[face] * 7)

    found = locate_facecam(frames, face_detector=fake_detector(frames, boxes))

    assert found.status == "found_padded"
    # 3 face widths wide at 4:3, flush with the bottom-right corner.
    assert_region(found.region, (0.64, 0.52, 0.36, 0.48))
    assert found.region is not None
    x, y, w, h = found.region
    assert x <= face[0]
    assert y <= face[1]
    assert x + w >= face[0] + face[2]
    assert y + h >= face[1] + face[3]


@pytest.mark.parametrize(
    "face",
    [
        pytest.param((0.18, 0.80, 0.06, 0.10), id="bottom-left-far-from-the-side"),
        pytest.param((0.15, 0.80, 0.06, 0.10), id="bottom-left"),
        pytest.param((0.10, 0.82, 0.06, 0.10), id="bottom-left-close"),
        pytest.param((0.80, 0.05, 0.06, 0.10), id="top-right"),
        pytest.param((0.82, 0.12, 0.06, 0.10), id="top-right-far-from-the-top"),
        pytest.param((0.14, 0.11, 0.06, 0.10), id="top-left"),
        pytest.param((0.78, 0.80, 0.06, 0.10), id="bottom-right"),
    ],
)
def test_a_padded_corner_box_always_holds_a_small_face_with_a_margin(face: Region) -> None:
    box = _padded_corner_box(face, WIDTH, HEIGHT)

    assert isinstance(box, tuple)
    x, y, w, h = box
    fx, fy, fw, fh = face
    assert x <= fx - 0.4 * fw or x == 0.0
    assert y <= fy - 0.4 * fh or y == 0.0
    assert x + w >= fx + fw + 0.4 * fw or x + w == pytest.approx(1.0)
    assert y + h >= fy + fh + 0.4 * fh or y + h == pytest.approx(1.0)
    assert x >= 0.0
    assert y >= 0.0
    assert x + w <= 1.0 + 1e-9
    assert y + h <= 1.0 + 1e-9


def test_a_padded_box_that_would_be_too_large_is_not_found() -> None:
    # A large face in the top-left corner needs a box over 35 % of the frame.
    assert _padded_corner_box((0.02, 0.15, 0.2, 0.35), WIDTH, HEIGHT) == "too_large"
    assert _padded_corner_box((0.42, 0.3, 0.12, 0.2), WIDTH, HEIGHT) == "not_in_corner"


def test_a_steady_face_without_an_overlay_box_is_a_full_screen_camera() -> None:
    face: Region = (0.42, 0.3, 0.12, 0.2)
    frames, boxes = make_frames(cam=None, faces=[face] * 7)

    found = locate_facecam(frames, face_detector=fake_detector(frames, boxes))

    assert found.status == "full_camera"


def test_a_face_that_never_changes_is_a_picture_not_a_camera() -> None:
    frames, boxes = make_frames(live=False)

    found = locate_facecam(frames, face_detector=fake_detector(frames, boxes))

    assert found.status == "not_found"
    assert "picture" in found.message


def test_the_haar_detector_runs_and_finds_nothing_in_a_flat_frame() -> None:
    assert haar_face_detector(np.full((HEIGHT, WIDTH), 90, dtype=np.uint8)) == []


# --- Frames, caching and detect_facecam ---------------------------------------------------------


def test_sample_times_spread_across_the_interval() -> None:
    assert sample_times(Interval(10.0, 24.0), 7) == pytest.approx([11, 13, 15, 17, 19, 21, 23])
    with pytest.raises(ValueError, match="at least 1"):
        sample_times(Interval(0.0, 1.0), 0)


class FrameRunner:
    """A fake FFmpeg runner that serves generated frames as raw grayscale output."""

    def __init__(self, frames: Sequence[GrayFrame]) -> None:
        self.frames = list(frames)
        self.commands: list[FFmpegCommand] = []

    def __call__(self, command: FFmpegCommand, /, *, timeout_s: float) -> CommandResult:
        self.commands.append(command)
        time_s = float(command.args[command.args.index("-ss") + 1])
        index = int(time_s) % len(self.frames)
        return CommandResult(0, stdout=self.frames[index].tobytes())


def test_read_gray_frame_seeks_to_a_keyframe_and_scales_down(tmp_path: Path) -> None:
    frames, _ = make_frames(count=1)
    fake = FrameRunner(frames)

    frame = read_gray_frame(tmp_path / "vod.mp4", 3.25, runner=fake)

    assert frame is not None
    assert frame.shape == (HEIGHT, WIDTH)
    args = fake.commands[0].args
    assert args[args.index("-skip_frame") + 1] == "nokey"
    assert args[args.index("-ss") + 1] == "3.250"
    assert "scale=640:-2:flags=area,format=gray" in args


def test_read_gray_frame_past_the_end_is_none(tmp_path: Path) -> None:
    def empty(command: FFmpegCommand, /, *, timeout_s: float) -> CommandResult:
        return CommandResult(0, stdout=b"")

    assert read_gray_frame(tmp_path / "vod.mp4", 99.0, runner=empty) is None


def test_detect_facecam_reads_frames_once_and_caches_on_disk(tmp_path: Path) -> None:
    source = tmp_path / "vod.mp4"
    source.write_bytes(b"video")
    frames, boxes = make_frames()
    fake = FrameRunner(frames)
    face_for = {frame.tobytes(): box for frame, box in zip(frames, boxes, strict=True)}

    def detector(frame: GrayFrame) -> list[FaceBox]:
        box = face_for[frame.tobytes()]
        return [] if box is None else [box]

    interval = Interval(0.0, 7.0)
    cache_dir = tmp_path / "cache"
    first = detect_facecam(
        source, interval, cache=FacecamCache(cache_dir), face_detector=detector, runner=fake
    )
    assert first.status == "found"
    assert len(fake.commands) == 7

    again = detect_facecam(
        source, interval, cache=FacecamCache(cache_dir), face_detector=detector, runner=fake
    )
    assert again == first
    assert len(fake.commands) == 7  # served from the JSON on disk by a fresh cache

    detect_facecam(
        source,
        Interval(10.0, 17.0),
        cache=FacecamCache(cache_dir),
        face_detector=detector,
        runner=fake,
    )
    assert len(fake.commands) == 14  # another interval is detected again


def test_a_broken_cache_entry_is_a_miss(tmp_path: Path) -> None:
    source = tmp_path / "vod.mp4"
    source.write_bytes(b"video")
    cache = FacecamCache(tmp_path / "cache")
    key = FacecamCache.key(source, Interval(0.0, 5.0), 7)
    (tmp_path / "cache").mkdir()
    (tmp_path / "cache" / f"{key}.json").write_text("{not json", encoding="utf-8")

    assert cache.get(key) is None


def test_detections_round_trip_through_json() -> None:
    detection = FacecamDetection("found", (0.0, 0.6, 0.3, 0.4), (0.1, 0.6, 0.1, 0.2), 7, 6, "ok")

    assert FacecamDetection.from_json(detection.to_json()) == detection


# --- The stacked export's precedence ------------------------------------------------------------


PRESET = stacked_layout_from_region((0.76, 0.04, 0.22, 0.28), facecam_fraction=0.38)


def _detection(status: FacecamStatus, region: Region | None = None) -> FacecamDetection:
    return FacecamDetection(status, region, None, 7, 7 if region else 0, f"{status} for the test")


@pytest.fixture
def rendered(monkeypatch: pytest.MonkeyPatch) -> list[ExportRequest]:
    """Capture the requests `render_clip` would encode, without running FFmpeg."""
    requests: list[ExportRequest] = []

    def fake_export(request: ExportRequest, **_: object) -> object:
        requests.append(request)
        return request

    monkeypatch.setattr(clip_module, "export_clip", fake_export)
    return requests


def _render(
    tmp_path: Path,
    rendered: list[ExportRequest],
    detector: Callable[[Path, Interval], FacecamDetection],
    *,
    warnings: list[str] | None = None,
    facecam_region: Region | None = None,
    stacked_layout: StackedLayout | None = None,
    stacked_fallback: StackedLayout | None = None,
    detect_facecam: bool = True,
) -> object:
    source = tmp_path / "vod.mp4"
    source.write_bytes(b"video")
    spec = ClipSpec(
        source,
        Interval(10.0, 40.0),
        tmp_path / "out.mp4",
        layout="stacked",
        stacked_layout=stacked_layout,
        stacked_fallback=stacked_fallback,
        detect_facecam=detect_facecam,
    )
    render_clip(
        spec,
        facecam_region=facecam_region,
        facecam_detector=detector,
        on_warning=None if warnings is None else (lambda w: warnings.append(w.message)),
    )
    return rendered[-1].layout


def _never(_source: Path, _interval: Interval) -> FacecamDetection:
    raise AssertionError("detection must not run")


def test_an_explicit_layout_or_region_skips_detection(
    tmp_path: Path, rendered: list[ExportRequest]
) -> None:
    assert _render(tmp_path, rendered, _never, stacked_layout=PRESET) == PRESET
    layout = _render(tmp_path, rendered, _never, facecam_region=(0.0, 0.0, 0.25, 0.3))
    assert isinstance(layout, StackedLayout)
    assert layout.facecam.width == pytest.approx(0.25)


def test_a_detected_facecam_beats_the_channel_preset(
    tmp_path: Path, rendered: list[ExportRequest]
) -> None:
    calls: list[Interval] = []

    def detector(_source: Path, interval: Interval) -> FacecamDetection:
        calls.append(interval)
        return _detection("found", BOTTOM_LEFT_CAM)

    layout = _render(tmp_path, rendered, detector, stacked_fallback=PRESET)

    assert calls == [Interval(10.0, 40.0)]
    assert isinstance(layout, StackedLayout)
    assert (layout.facecam.x, layout.facecam.y) == pytest.approx((0.0, 0.6))
    assert layout.facecam_fraction == pytest.approx(PRESET.facecam_fraction)


def test_a_detected_facecam_carries_its_face_to_the_layout(
    tmp_path: Path, rendered: list[ExportRequest]
) -> None:
    found = FacecamDetection("found", BOTTOM_LEFT_CAM, BOTTOM_LEFT_FACE, 7, 7, "found")
    padded = _padded((0.82, 0.68, 0.12, 0.2))

    layout = _render(tmp_path, rendered, lambda *_: found, stacked_fallback=PRESET)
    estimated = _render(tmp_path, rendered, lambda *_: padded)

    assert isinstance(layout, StackedLayout)
    assert layout.face == BOTTOM_LEFT_FACE
    assert isinstance(estimated, StackedLayout)
    assert estimated.face == (0.82, 0.68, 0.12, 0.2)


def test_a_preset_or_explicit_region_has_no_face(
    tmp_path: Path, rendered: list[ExportRequest]
) -> None:
    explicit = _render(tmp_path, rendered, _never, facecam_region=(0.0, 0.0, 0.25, 0.3))
    preset = _render(
        tmp_path, rendered, lambda *_: _detection("not_found"), stacked_fallback=PRESET
    )

    assert isinstance(explicit, StackedLayout)
    assert explicit.face is None
    assert preset == PRESET
    assert PRESET.face is None


def test_without_a_face_the_channel_preset_is_used_with_a_warning(
    tmp_path: Path, rendered: list[ExportRequest]
) -> None:
    warnings: list[str] = []

    layout = _render(
        tmp_path,
        rendered,
        lambda *_: _detection("not_found"),
        warnings=warnings,
        stacked_fallback=PRESET,
    )

    assert layout == PRESET
    assert len(warnings) == 1
    assert "preset" in warnings[0]


@pytest.mark.parametrize("status", ["not_found", "inconsistent", "full_camera"])
def test_without_a_face_or_preset_the_clip_is_center_cropped_with_a_warning(
    tmp_path: Path, rendered: list[ExportRequest], status: FacecamStatus
) -> None:
    warnings: list[str] = []

    layout = _render(tmp_path, rendered, lambda *_: _detection(status), warnings=warnings)

    assert isinstance(layout, CenterCrop)
    assert len(warnings) == 1
    assert "center_crop" in warnings[0]


def test_a_full_screen_camera_ignores_the_preset(
    tmp_path: Path, rendered: list[ExportRequest]
) -> None:
    layout = _render(
        tmp_path, rendered, lambda *_: _detection("full_camera"), stacked_fallback=PRESET
    )

    assert isinstance(layout, CenterCrop)


def _padded(face: Region) -> FacecamDetection:
    return FacecamDetection("found_padded", (0.64, 0.52, 0.36, 0.48), face, 7, 7, "padded")


def test_a_padded_box_loses_to_a_preset_that_holds_the_face(
    tmp_path: Path, rendered: list[ExportRequest]
) -> None:
    # PRESET's facecam is the top-right corner (0.76, 0.04, 0.22, 0.28).
    in_preset = _padded((0.82, 0.08, 0.1, 0.18))

    layout = _render(tmp_path, rendered, lambda *_: in_preset, stacked_fallback=PRESET)

    assert isinstance(layout, StackedLayout)
    assert layout.facecam == PRESET.facecam
    assert layout.gameplay == PRESET.gameplay
    assert layout.facecam_fraction == PRESET.facecam_fraction
    # The detected face comes along, so the preset's pane is trimmed around it.
    assert layout.face == in_preset.face


def test_a_padded_box_beats_a_preset_elsewhere_with_a_warning(
    tmp_path: Path, rendered: list[ExportRequest]
) -> None:
    warnings: list[str] = []
    bottom_right = _padded((0.82, 0.68, 0.12, 0.2))

    layout = _render(
        tmp_path, rendered, lambda *_: bottom_right, warnings=warnings, stacked_fallback=PRESET
    )

    assert isinstance(layout, StackedLayout)
    assert (layout.facecam.x, layout.facecam.y) == pytest.approx((0.64, 0.52))
    assert "isn't in the channel's stacked_layout preset" in warnings[0]


def test_without_a_preset_the_padded_box_is_used_with_a_warning(
    tmp_path: Path, rendered: list[ExportRequest]
) -> None:
    warnings: list[str] = []
    padded = _detection("found_padded", (0.64, 0.52, 0.36, 0.48))

    layout = _render(tmp_path, rendered, lambda *_: padded, warnings=warnings)

    assert isinstance(layout, StackedLayout)
    assert (layout.facecam.x, layout.facecam.y) == pytest.approx((0.64, 0.52))
    assert len(warnings) == 1
    assert "estimate" in warnings[0]


def _raise(exc: BaseException) -> Callable[[Path, Interval], FacecamDetection]:
    def detector(_source: Path, _interval: Interval) -> FacecamDetection:
        raise exc

    return detector


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(FFmpegExitError(FFmpegCommand(("ffmpeg",)), 1, b"bad frame"), id="ffmpeg"),
        pytest.param(RuntimeError("couldn't load the face cascade"), id="cascade"),
        pytest.param(ImportError("No module named 'cv2'"), id="no-opencv"),
        pytest.param(FileNotFoundError("vod.mp4"), id="file-gone"),
    ],
)
def test_a_failing_detector_never_fails_the_render(
    tmp_path: Path, rendered: list[ExportRequest], error: Exception
) -> None:
    warnings: list[str] = []
    layout = _render(tmp_path, rendered, _raise(error), warnings=warnings, stacked_fallback=PRESET)
    assert layout == PRESET
    assert "facecam detection failed" in warnings[0]

    assert isinstance(_render(tmp_path, rendered, _raise(error)), CenterCrop)


def test_only_a_detection_error_is_flagged_as_failed(
    tmp_path: Path, rendered: list[ExportRequest]
) -> None:
    source = tmp_path / "vod.mp4"
    source.write_bytes(b"video")
    spec = ClipSpec(source, Interval(0.0, 5.0), tmp_path / "out.mp4", layout="stacked")
    seen: list[LayoutWarning] = []

    render_clip(spec, facecam_detector=_raise(RuntimeError("boom")), on_warning=seen.append)
    render_clip(spec, facecam_detector=lambda *_: _detection("not_found"), on_warning=seen.append)

    assert [w.detection_failed for w in seen] == [True, False]


def test_with_detection_off_the_preset_is_used_or_the_layout_refused(
    tmp_path: Path, rendered: list[ExportRequest]
) -> None:
    assert (
        _render(tmp_path, rendered, _never, detect_facecam=False, stacked_fallback=PRESET) == PRESET
    )
    with pytest.raises(NoFacecamConfiguredError):
        _render(tmp_path, rendered, _never, detect_facecam=False)


def test_the_export_command_falls_back_to_center_crop_without_a_facecam(
    make_test_media: Callable[..., Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTOCLIPPER_ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    from autoclipper.config import get_settings

    get_settings.cache_clear()
    try:
        video = make_test_media("vod.mp4", duration_s=4.0)
        result = CliRunner().invoke(
            app,
            ["export", str(video), "--start", "0.5", "--end", "3", "--layout", "stacked"],
        )
    finally:
        get_settings.cache_clear()

    assert result.exit_code == ExitCode.SUCCESS, result.output
    assert "Warning: no facecam detected" in result.output
    assert "center_crop" in result.output
    assert list((tmp_path / "artifacts" / "cache" / "facecam").glob("*.json"))
