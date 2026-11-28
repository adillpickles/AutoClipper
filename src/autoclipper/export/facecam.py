"""Find the streamer's facecam overlay in a clip, for the ``stacked`` layout (#191).

`detect_facecam` samples a few frames spread across the clip's interval with FFmpeg (small,
grayscale), finds faces in each with OpenCV's bundled Haar cascade, and keeps a face only when
it sits in the same place in most of the frames. Webcam overlays are a fixed box, so a face
that stays put marks one; a face that moves around (a full-screen camera, or the face only in
some scenes) does not. `locate_facecam` then grows the face to the overlay's rectangle: for each
side it looks for the nearest edge that stays in place across the sampled frames (the border
between the camera and the changing gameplay) and runs along most of that side, falls back to
the frame edge when the search reaches it (overlays live in corners), and otherwise pads the
face by one face size. A face whose pixels never change between frames is a picture, not a
camera, and is ignored.

Results:

- ``found``: a normalized ``(x, y, width, height)`` region for the facecam pane.
- ``not_found``: no face in enough frames (no camera, or a face the detector can't see).
- ``inconsistent``: faces, but not in one place (a moving or full-screen camera, scene changes).
- ``found_padded``: a steady face near a frame corner with no visible overlay border; the
  region is a webcam-shaped box estimated around the face, flush with that corner.
- ``full_camera``: one face in place, but it fills the frame, or it has no overlay box around it
  and isn't near a corner.

Detection costs about 0.3-1.5 s per clip on a laptop CPU: seven 640 px grayscale keyframes (the
seeks run in parallel) and a Haar cascade on each. `FacecamCache` keeps results per source file
and interval, in memory and optionally on disk, so exporting the same clip again doesn't detect
again.
"""

import hashlib
import json
import logging
import math
import threading
import uuid
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Final, Literal

import numpy as np
import numpy.typing as npt

from autoclipper.export.cut import Interval
from autoclipper.ffmpeg import (
    TIMELINE_OPTIONS,
    FFmpegError,
    Runner,
    ffmpeg_command,
    run_command,
)

log = logging.getLogger(__name__)

# Bump when the detection logic changes, so stale on-disk cache entries are ignored.
DETECTOR_VERSION: Final = 2

DEFAULT_SAMPLES: Final = 7
# Frames are scaled to this width before detection; 640 px keeps a corner webcam's face around
# 60-90 px wide, well above the cascade's 24 px window, and each frame costs ~30 ms to scan.
SAMPLE_WIDTH: Final = 640
FRAME_TIMEOUT_S: Final = 30.0
_PARALLEL_SEEKS: Final = 4

# A face counts as consistent when it shows up, in the same place, in at least this share of
# the decoded frames (and in at least `MIN_FACE_FRAMES`).
MIN_FACE_SHARE: Final = 0.6
MIN_FACE_FRAMES: Final = 3
# Two detections are "the same place" when their centers are within this many face widths.
SAME_PLACE_FACE_WIDTHS: Final = 0.5
# A camera's face area changes between frames seconds apart (median over consecutive frames of
# the mean absolute difference, gray levels); a face-like pattern in a still picture doesn't.
_MIN_FACE_CHANGE: Final = 3.0

# A face wider than this share of the frame is a full-screen (or near full-screen) camera.
MAX_OVERLAY_FACE_WIDTH: Final = 0.22
# An overlay larger than this share of the frame area isn't an overlay.
MAX_OVERLAY_AREA: Final = 0.35

# How far from the face's side, in face sizes, each side of the overlay is searched for. The top
# search starts a little inside the face box: Haar boxes include the forehead, and a webcam
# cropped tight to the head can have its border there.
_EDGE_SEARCH_START: Final = 0.1
_EDGE_SEARCH_START_TOP: Final = -0.3
_EDGE_SEARCH_END: Final = 2.5
# Where the search starts instead when the box it found would be barely bigger than the face.
_EDGE_SEARCH_OUTSIDE: Final = 0.25
# The overlay box is at least this many face sizes wide and high.
_MIN_BOX_FACES: Final = 1.5
# A border is a persistent step of at least this many gray levels covering this share of the
# side it bounds.
_BORDER_MIN_STRENGTH: Final = 4.0
_MIN_BORDER_COVERAGE: Final = 0.4
# Without an edge or a nearby frame edge, the box extends this many face sizes past the face.
_FALLBACK_PAD: Final = 1.0
# A persistent edge must be this much stronger than the typical column/row in its search range,
# and at least this many gray levels on average along the side.
_EDGE_PEAK_RATIO: Final = 2.5
_EDGE_MIN_STRENGTH: Final = 6.0
# Edges at least this share of the strongest one in range count; the nearest of them wins.
_NEAR_PEAK_SHARE: Final = 0.5
# A borderless webcam's estimated box: this many face widths wide (4:3), the face's center this
# far down it, flush with the frame edges it comes within the slack of (a corner is required).
_PAD_BOX_FACES: Final = 3.0
_PAD_FACE_DROP: Final = 0.45
_PAD_CORNER_SLACK: Final = 0.12
# Sides within this share of the frame edge are snapped to it.
_SNAP: Final = 0.03

type FacecamStatus = Literal["found", "found_padded", "not_found", "inconsistent", "full_camera"]
_STATUSES: Final = ("found", "found_padded", "not_found", "inconsistent", "full_camera")
type Region = tuple[float, float, float, float]
type GrayFrame = npt.NDArray[np.uint8]
# A face as (x, y, width, height) in frame pixels.
type FaceBox = tuple[int, int, int, int]
type FaceDetector = Callable[[GrayFrame], Sequence[FaceBox]]


@dataclass(frozen=True, slots=True)
class FacecamDetection:
    """What `detect_facecam` found.

    Attributes:
        status: ``found``, ``found_padded``, ``not_found``, ``inconsistent`` or
            ``full_camera`` (module docs).
        region: The facecam overlay as normalized ``(x, y, width, height)``; set iff the status
            is ``found`` or ``found_padded``.
        face: The consistent face as normalized ``(x, y, width, height)``, when there was one.
        frames: How many frames were decoded and searched.
        face_frames: How many of them showed the consistent face.
        message: One human-readable sentence about the result.
    """

    status: FacecamStatus
    region: Region | None
    face: Region | None
    frames: int
    face_frames: int
    message: str

    def to_json(self) -> dict[str, object]:
        """A JSON-ready dict (tuples become lists)."""
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict[str, object]) -> "FacecamDetection":
        """Read back `to_json`'s dict; raises ValueError, KeyError or TypeError if malformed."""

        def region(value: object) -> Region | None:
            if value is None:
                return None
            if not isinstance(value, list | tuple) or len(value) != 4:
                raise ValueError(f"bad region {value!r}")
            x, y, w, h = (float(v) for v in value)
            return (x, y, w, h)

        status = data["status"]
        if status not in _STATUSES:
            raise ValueError(f"bad status {status!r}")
        frames = data["frames"]
        face_frames = data["face_frames"]
        if not isinstance(frames, int) or not isinstance(face_frames, int):
            raise ValueError("bad frame counts")
        return cls(
            status=status,
            region=region(data.get("region")),
            face=region(data.get("face")),
            frames=frames,
            face_frames=face_frames,
            message=str(data.get("message", "")),
        )


# --- Faces -------------------------------------------------------------------------------------

_cascade_lock = threading.Lock()
_cascade: object | None = None


def haar_face_detector(frame: GrayFrame) -> list[FaceBox]:
    """Frontal faces in a grayscale frame, with OpenCV's bundled Haar cascade."""
    import cv2

    global _cascade
    with _cascade_lock:
        if _cascade is None:
            # cv2.data ships with the wheel but has no type stubs.
            folder: str = cv2.data.haarcascades  # pyright: ignore[reportAttributeAccessIssue]
            path = Path(folder) / "haarcascade_frontalface_default.xml"
            cascade = cv2.CascadeClassifier(str(path))
            if cascade.empty():
                raise RuntimeError(f"couldn't load the face cascade {path}")
            _cascade = cascade
        cascade = _cascade
    assert isinstance(cascade, cv2.CascadeClassifier)
    found = cascade.detectMultiScale(frame, scaleFactor=1.1, minNeighbors=5, minSize=(20, 20))
    return [(int(x), int(y), int(w), int(h)) for x, y, w, h in found]


# --- Frames ------------------------------------------------------------------------------------


def sample_times(interval: Interval, count: int = DEFAULT_SAMPLES) -> list[float]:
    """`count` times spread evenly across `interval`, each in the middle of its share."""
    if count < 1:
        raise ValueError(f"count must be at least 1, got {count}")
    return [interval.start_s + interval.duration_s * (i + 0.5) / count for i in range(count)]


def read_gray_frame(
    source: Path, time_s: float, *, width: int = SAMPLE_WIDTH, runner: Runner | None = None
) -> GrayFrame | None:
    """The frame of `source` at `time_s`, scaled to `width` and grayscale; None past the end."""
    # Keyframes only, and the keyframe at or before `time_s` (within a couple of seconds on
    # Twitch): decoding stops after one frame instead of running up to the exact time.
    command = ffmpeg_command(
        *TIMELINE_OPTIONS,
        *("-skip_frame", "nokey", "-noaccurate_seek"),
        *("-ss", f"{max(0.0, time_s):.3f}"),
        *("-i", source),
        *("-map", "0:v:0", "-frames:v", "1", "-an", "-sn", "-dn"),
        *("-vf", f"scale={width}:-2:flags=area,format=gray"),
        *("-f", "rawvideo", "-"),
    )
    data = run_command(command, timeout_s=FRAME_TIMEOUT_S, runner=runner).stdout
    if not data or len(data) % width:
        return None
    return np.frombuffer(data, dtype=np.uint8).reshape(len(data) // width, width)


def read_gray_frames(
    source: Path, times: Sequence[float], *, runner: Runner | None = None
) -> list[GrayFrame]:
    """The frames at `times` (missing ones left out), read with a few FFmpeg seeks in parallel."""
    with ThreadPoolExecutor(max_workers=_PARALLEL_SEEKS) as pool:
        frames = list(pool.map(lambda t: read_gray_frame(source, t, runner=runner), times))
    shapes = {frame.shape for frame in frames if frame is not None}
    if len(shapes) > 1:
        raise FFmpegError(f"frames of {source} came back in different sizes: {sorted(shapes)}")
    return [frame for frame in frames if frame is not None]


# --- Locating the overlay ----------------------------------------------------------------------


def locate_facecam(
    frames: Sequence[GrayFrame], *, face_detector: FaceDetector = haar_face_detector
) -> FacecamDetection:
    """Find the facecam overlay in `frames` (same-size grayscale frames of one clip)."""
    if not frames:
        return FacecamDetection("not_found", None, None, 0, 0, "no frames could be read")
    height, width = frames[0].shape
    detections = [list(face_detector(frame)) for frame in frames]
    n = len(frames)
    needed = max(MIN_FACE_FRAMES, math.ceil(MIN_FACE_SHARE * n))
    if sum(1 for faces in detections if faces) < needed:
        return FacecamDetection(
            "not_found",
            None,
            None,
            n,
            sum(1 for faces in detections if faces),
            f"a face was found in {sum(1 for f in detections if f)} of {n} frames; "
            f"{needed} are needed",
        )
    groups = _face_groups(detections)
    steady = [group for group in groups if len(group) >= needed]
    if not steady:
        best = len(groups[0]) if groups else 0
        return FacecamDetection(
            "inconsistent",
            None,
            None,
            n,
            best,
            f"faces were found but not in one place (at most {best} of {n} frames agree); "
            "the camera may be full-screen or move between scenes",
        )
    support = next((group for group in steady if _is_live(frames, group)), None)
    if support is None:
        return FacecamDetection(
            "not_found",
            None,
            None,
            n,
            len(steady[0]),
            "the only steady face never changes between frames, so it's a picture, not a camera",
        )
    fx, fy, fw, fh = _median_box(support)
    face = (fx / width, fy / height, fw / width, fh / height)
    if face[2] > MAX_OVERLAY_FACE_WIDTH:
        return FacecamDetection(
            "full_camera",
            None,
            _rounded(face),
            n,
            len(support),
            f"the face is {face[2]:.0%} of the frame width, so the camera fills the frame",
        )
    edges_x, edges_y = _persistent_edges(frames)
    edges_yt = edges_y.T
    face_rows = (round(fy), round(fy + fh))
    face_cols = (round(fx), round(fx + fw))

    def sides(start: float, top_start: float) -> tuple[_Side, _Side, _Side, _Side]:
        return (
            _side(edges_x, fx, fw, face_rows, width, direction=-1, start=start),
            _side(edges_x, fx + fw, fw, face_rows, width, direction=1, start=start),
            _side(edges_yt, fy, fh, face_cols, height, direction=-1, start=top_start),
            _side(edges_yt, fy + fh, fh, face_cols, height, direction=1, start=start),
        )

    left, right, top, bottom = sides(_EDGE_SEARCH_START, _EDGE_SEARCH_START_TOP)
    # A webcam shows the head and shoulders, so its box is clearly larger than the face; edges
    # that hug the face belong to something else (a poster, a HUD panel).
    if right.position - left.position < _MIN_BOX_FACES * fw:
        outside = sides(_EDGE_SEARCH_OUTSIDE, _EDGE_SEARCH_OUTSIDE)
        left, right = outside[0], outside[1]
    if bottom.position - top.position < _MIN_BOX_FACES * fh:
        outside = sides(_EDGE_SEARCH_OUTSIDE, _EDGE_SEARCH_OUTSIDE)
        top, bottom = outside[2], outside[3]
    # A border must run along most of the side of the box, as an overlay's does.
    rows = (round(top.position), round(bottom.position))
    cols = (round(left.position), round(right.position))
    left = left.checked(edges_x, rows)
    right = right.checked(edges_x, rows)
    top = top.checked(edges_yt, cols)
    bottom = bottom.checked(edges_yt, cols)
    region = _snap(
        (
            left.position / width,
            top.position / height,
            (right.position - left.position) / width,
            (bottom.position - top.position) / height,
        )
    )
    # An overlay away from the frame edges shows a border on both axes; one in a corner shows
    # its two inner borders. Without one each way, the box is estimated around the face.
    if "edge" not in (left.kind, right.kind) or "edge" not in (top.kind, bottom.kind):
        padded = _padded_corner_box(face, width, height)
        if padded is None:
            return FacecamDetection(
                "full_camera",
                None,
                _rounded(face),
                n,
                len(support),
                "the face has no overlay box around it and isn't in a corner, so the camera "
                "likely fills the frame",
            )
        return FacecamDetection(
            "found_padded",
            _rounded(padded),
            _rounded(face),
            n,
            len(support),
            f"a steady face in a corner in {len(support)} of {n} frames, but no overlay border; "
            "the box is estimated around the face",
        )
    if region[2] * region[3] > MAX_OVERLAY_AREA:
        return FacecamDetection(
            "full_camera",
            None,
            _rounded(face),
            n,
            len(support),
            f"the camera covers {region[2] * region[3]:.0%} of the frame, so it isn't an overlay",
        )
    return FacecamDetection(
        "found",
        _rounded(region),
        _rounded(face),
        n,
        len(support),
        f"facecam found in {len(support)} of {n} frames",
    )


def _padded_corner_box(face: Region, width: int, height: int) -> Region | None:
    """A webcam-shaped box around a face near a frame corner, or None away from the corners.

    The box is `_PAD_BOX_FACES` face widths wide at 4:3 (in pixels), centered on the face across
    and with the face's center `_PAD_FACE_DROP` of the way down, then moved flush with the frame
    edges it comes within `_PAD_CORNER_SLACK` of. It must touch a side edge and the top or
    bottom: borderless webcams sit in corners, and a face elsewhere is a full-screen camera.
    """
    fx, fy, fw, fh = face
    w = min(1.0, _PAD_BOX_FACES * fw)
    h = min(1.0, w * width * 3 / 4 / height)
    x = fx + fw / 2 - w / 2
    y = fy + fh / 2 - _PAD_FACE_DROP * h
    if x <= _PAD_CORNER_SLACK:
        x, horizontal = 0.0, True
    elif x + w >= 1 - _PAD_CORNER_SLACK:
        x, horizontal = 1.0 - w, True
    else:
        horizontal = False
    if y <= _PAD_CORNER_SLACK:
        y, vertical = 0.0, True
    elif y + h >= 1 - _PAD_CORNER_SLACK:
        y, vertical = 1.0 - h, True
    else:
        vertical = False
    if not (horizontal and vertical) or w * h > MAX_OVERLAY_AREA:
        return None
    return (x, y, w, h)


def _face_groups(detections: Sequence[Sequence[FaceBox]]) -> list[list[FaceBox]]:
    """Sets of detections around one place, at most one per frame, largest first."""
    groups: list[list[FaceBox]] = []
    for faces in detections:
        for anchor in faces:
            ax, ay = anchor[0] + anchor[2] / 2, anchor[1] + anchor[3] / 2
            limit = SAME_PLACE_FACE_WIDTHS * anchor[2]

            def distance(face: FaceBox, ax: float = ax, ay: float = ay) -> float:
                return math.hypot(face[0] + face[2] / 2 - ax, face[1] + face[3] / 2 - ay)

            group: list[FaceBox] = []
            for other_faces in detections:
                near = [f for f in other_faces if distance(f) <= limit]
                if near:
                    group.append(min(near, key=distance))
            groups.append(group)
    return sorted(groups, key=len, reverse=True)


def _median_box(group: Sequence[FaceBox]) -> tuple[float, float, float, float]:
    x, y, w, h = (float(np.median([box[i] for box in group])) for i in range(4))
    return (x, y, w, h)


def _is_live(frames: Sequence[GrayFrame], group: Sequence[FaceBox]) -> bool:
    """Whether the face area changes from frame to frame, as a camera's does.

    A face-like pattern in a still picture or a game board changes only now and then (a piece
    moves), so the typical change between consecutive frames is what counts.
    """
    x, y, w, h = (round(v) for v in _median_box(group))
    crops = np.stack([frame[y : y + h, x : x + w] for frame in frames]).astype(np.float32)
    if crops.shape[0] < 2 or crops[0].size == 0:
        return False
    changes = np.abs(np.diff(crops, axis=0)).mean(axis=(1, 2))
    return float(np.median(changes)) >= _MIN_FACE_CHANGE


def _persistent_edges(
    frames: Sequence[GrayFrame],
) -> tuple[npt.NDArray[np.float32], npt.NDArray[np.float32]]:
    """Horizontal and vertical gradients that stay strong in (nearly) every frame.

    A low quantile across frames keeps an edge only if it's there in most frames, so moving
    gameplay averages away while the overlay's border stays. Both maps are frame-sized; column
    ``c`` of the first holds the step between columns ``c - 1`` and ``c``.
    """
    stack = np.stack(frames).astype(np.float32)
    dx = np.zeros_like(stack)
    dy = np.zeros_like(stack)
    dx[:, :, 1:] = np.abs(np.diff(stack, axis=2))
    dy[:, 1:, :] = np.abs(np.diff(stack, axis=1))
    quantile = 0.25 if len(frames) >= 4 else 0.0
    return (
        np.quantile(dx, quantile, axis=0).astype(np.float32),
        np.quantile(dy, quantile, axis=0).astype(np.float32),
    )


type _SideKind = Literal["edge", "frame", "pad"]


@dataclass(frozen=True, slots=True)
class _Side:
    """One side of the overlay box, in pixels along its axis.

    ``edge``: a persistent border; ``frame``: the frame edge (the search reached it);
    ``pad``: a fixed distance past the face. `fallback` is the frame-or-pad position to use if
    the border turns out not to run along the whole box.
    """

    position: float
    kind: _SideKind
    fallback: tuple[float, _SideKind]

    def checked(self, edges: npt.NDArray[np.float32], span: tuple[int, int]) -> "_Side":
        """This side, or its fallback if it's an edge that doesn't cover most of `span`."""
        if self.kind != "edge" or _coverage(edges, self.position, span) >= _MIN_BORDER_COVERAGE:
            return self
        position, kind = self.fallback
        return _Side(position, kind, self.fallback)


def _coverage(edges: npt.NDArray[np.float32], position: float, span: tuple[int, int]) -> float:
    """The share of `span` (indices across `edges`) where a border is at `position` (±1 px)."""
    a0, a1 = max(0, span[0]), min(edges.shape[0], span[1])
    c = round(position)
    c0, c1 = max(0, c - 1), min(edges.shape[1], c + 2)
    if a1 <= a0 or c1 <= c0:
        return 0.0
    strip = edges[a0:a1, c0:c1].max(axis=1)
    return float((strip >= _BORDER_MIN_STRENGTH).mean())


def _side(
    edges: npt.NDArray[np.float32],
    face_edge: float,
    face_size: float,
    face_span: tuple[int, int],
    limit: int,
    *,
    direction: Literal[-1, 1],
    start: float,
) -> _Side:
    """Where one side of the overlay is, searching outward from the face's side.

    `edges` is indexed ``[across, along]``: for the left and right sides, rows by columns (the
    x gradient); for top and bottom, the transposed y gradient. `face_span` is the face's extent
    across (rows for left and right). The search runs from `start` to `_EDGE_SEARCH_END` face
    sizes past the face's side; the strength profile averages the edge over the face's span
    widened by half a face either way.
    """
    near = face_edge + direction * start * face_size
    far = face_edge + direction * _EDGE_SEARCH_END * face_size
    if (direction < 0 and far <= 0) or (direction > 0 and far >= limit):
        fallback: tuple[float, _SideKind] = (0.0 if direction < 0 else float(limit), "frame")
    else:
        padded = face_edge + direction * _FALLBACK_PAD * face_size
        fallback = (float(min(max(padded, 0.0), limit)), "pad")
    lo, hi = sorted((near, far))
    lo_i, hi_i = max(1, round(lo)), min(limit - 1, round(hi))
    half = (face_span[1] - face_span[0]) / 2
    a0 = max(0, round(face_span[0] - half))
    a1 = min(edges.shape[0], round(face_span[1] + half))
    if hi_i - lo_i >= 3 and a1 > a0:
        profile = edges[a0:a1, lo_i:hi_i].mean(axis=0)
        floor = max(
            _EDGE_MIN_STRENGTH,
            _EDGE_PEAK_RATIO * max(float(np.median(profile)), 1.0),
            _NEAR_PEAK_SHARE * float(profile.max()),
        )
        candidates = np.flatnonzero(profile >= floor)
        if candidates.size:
            # The overlay's border is the strong edge closest to the face; stronger lines
            # farther out are usually HUD elements or another overlay.
            index = int(candidates[0] if direction > 0 else candidates[-1])
            return _Side(float(lo_i + index), "edge", fallback)
    return _Side(fallback[0], fallback[1], fallback)


def _snap(region: Region) -> Region:
    x, y, w, h = region
    right, bottom = x + w, y + h
    x = 0.0 if x < _SNAP else x
    y = 0.0 if y < _SNAP else y
    right = 1.0 if right > 1 - _SNAP else right
    bottom = 1.0 if bottom > 1 - _SNAP else bottom
    return (x, y, right - x, bottom - y)


def _rounded(region: Region) -> Region:
    x, y, w, h = (round(v, 4) for v in region)
    # Rounding must not push the region past the frame edge.
    return (x, y, min(w, round(1 - x, 4)), min(h, round(1 - y, 4)))


# --- Caching and the entry point ---------------------------------------------------------------


class FacecamCache:
    """Detections per source file and interval, in memory and optionally as JSON in `directory`.

    The key is the resolved path, size and modification time of the source plus the interval
    (to the millisecond), the sample count and `DETECTOR_VERSION`, so an edited or replaced file
    is detected again.
    """

    def __init__(self, directory: Path | None = None) -> None:
        self.directory = directory
        self._memory: dict[str, FacecamDetection] = {}
        self._lock = threading.Lock()

    @staticmethod
    def key(source: Path, interval: Interval, samples: int) -> str:
        """The cache key for one detection; raises OSError if `source` can't be read."""
        stat = source.stat()
        raw = json.dumps(
            [
                str(source.resolve()),
                stat.st_size,
                stat.st_mtime_ns,
                round(interval.start_s * 1000),
                round(interval.end_s * 1000),
                samples,
                DETECTOR_VERSION,
            ]
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]

    def get(self, key: str) -> FacecamDetection | None:
        """The cached detection for `key`, or None (an unreadable file counts as a miss)."""
        with self._lock:
            hit = self._memory.get(key)
        if hit is not None or self.directory is None:
            return hit
        path = self.directory / f"{key}.json"
        try:
            detection = FacecamDetection.from_json(json.loads(path.read_text(encoding="utf-8")))
        except FileNotFoundError:
            return None
        except (OSError, ValueError, KeyError, TypeError) as exc:
            log.warning("ignoring unreadable facecam cache entry %s: %s", path, exc)
            return None
        with self._lock:
            self._memory[key] = detection
        return detection

    def put(self, key: str, detection: FacecamDetection) -> None:
        """Keep `detection` under `key`; a failed disk write is logged, not raised."""
        with self._lock:
            self._memory[key] = detection
        if self.directory is None:
            return
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            path = self.directory / f"{key}.json"
            # A unique temporary name, so two processes writing one entry don't collide.
            tmp = path.with_name(f"{path.stem}.{uuid.uuid4().hex}.tmp")
            tmp.write_text(json.dumps(detection.to_json()), encoding="utf-8")
            tmp.replace(path)
        except OSError as exc:
            log.warning("couldn't write facecam cache entry in %s: %s", self.directory, exc)


# Shared by every export in this process (the API server exports many clips of one VOD).
_DEFAULT_CACHE = FacecamCache()


def detect_facecam(
    source: Path,
    interval: Interval,
    *,
    samples: int = DEFAULT_SAMPLES,
    cache: FacecamCache | None = None,
    face_detector: FaceDetector = haar_face_detector,
    runner: Runner | None = None,
) -> FacecamDetection:
    """Find the facecam overlay in `source` over `interval` (file times), cached.

    Args:
        source: The video file.
        interval: The clip's interval, in seconds of the file.
        samples: How many frames to search.
        cache: Where results are kept; defaults to one in-memory cache per process.
        face_detector: Finds faces in one frame; tests pass a fake.
        runner: Executes FFmpeg.

    Raises:
        FFmpegError: A frame couldn't be read.
    """
    store = _DEFAULT_CACHE if cache is None else cache
    key = FacecamCache.key(source, interval, samples)
    cached = store.get(key)
    if cached is not None:
        log.debug("facecam cache hit source=%s status=%s", source, cached.status)
        return cached
    frames = read_gray_frames(source, sample_times(interval, samples), runner=runner)
    detection = locate_facecam(frames, face_detector=face_detector)
    log.info(
        "facecam detection source=%s start_s=%.3f end_s=%.3f status=%s region=%s",
        source,
        interval.start_s,
        interval.end_s,
        detection.status,
        detection.region,
    )
    store.put(key, detection)
    return detection
