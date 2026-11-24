"""Pretrained audio-event probabilities per time window (docs/ml/features.md § Phase 2, #63).

`compute_audio_event_features` reads a VOD's normalized 16 kHz mono 16-bit WAV and its
``windows``, runs a CPU audio-event classifier on all windows or a chosen subset (such as the
top ~100 windows by an existing scorer), and returns the per-window maximum probability for four
event families:

- ``audio_evt_laughter``
- ``audio_evt_scream``
- ``audio_evt_cheer``
- ``audio_evt_music``

Default model: Google's YAMNet (Apache-2.0, trained on AudioSet-521) exported to ONNX and run
with ``onnxruntime`` on CPU (installed via the optional ``audio-events`` extra:
``uv sync --extra audio-events``). Weights are downloaded at runtime to the cache directory and
verified against a pinned SHA-256 digest; they are never committed to git.

When run on a subset of windows (via ``window_ids`` or ``scores`` + ``top_k``), only the
selected windows are read from the WAV and scored; unselected windows stay in the returned table
with ``null`` feature values so callers can join 1-to-1 on ``window_id`` without confusing an
unscored window for a 0.0 prediction (and both ``logreg`` and ``lightgbm`` handle missing values).
"""

import hashlib
import importlib
import importlib.util
import logging
import tempfile
import wave
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Protocol

import httpx
import numpy as np
import numpy.typing as npt
import polars as pl

from autoclipper.config import get_settings
from autoclipper.features.audio import SAMPLE_RATE_HZ, AudioFormatError, _checked_windows

log = logging.getLogger(__name__)

AUDIO_EVENTS_FEATURE_VERSION: Final = "audio-events-v1"
DEFAULT_TOP_K: Final = 100
YAMNET_NUM_CLASSES: Final = 521

# Pinned ONNX export of Google's official YAMNet SavedModel (Apache-2.0).
YAMNET_REVISION: Final = "ac2ca3bd45d12ec1f19f1144205ea529b4e9dedf"
YAMNET_MODEL_URL: Final = (
    f"https://huggingface.co/zeropointnine/yamnet-onnx/resolve/{YAMNET_REVISION}/yamnet.onnx"
)
YAMNET_MODEL_SHA256: Final = "1510041dce24a2e9e84ec546807ac408ae496da6d1ed41bc3ccba649623f8e19"
YAMNET_MODEL_FILENAME: Final = "yamnet.onnx"
_DOWNLOAD_CHUNK_BYTES: Final = 1 << 20  # 1 MiB


@dataclass(frozen=True, slots=True)
class AudioSetClass:
    """One AudioSet-521 class entry from YAMNet's official ``yamnet_class_map.csv``."""

    index: int
    mid: str
    display_name: str


YAMNET_LABEL_MAP: Final[dict[str, tuple[AudioSetClass, ...]]] = {
    "audio_evt_laughter": (
        AudioSetClass(13, "/m/01j3sz", "Laughter"),
        AudioSetClass(14, "/t/dd00001", "Baby laughter"),
        AudioSetClass(15, "/m/07r660_", "Giggle"),
        AudioSetClass(16, "/m/07s04w4", "Snicker"),
        AudioSetClass(17, "/m/07sq110", "Belly laugh"),
        AudioSetClass(18, "/m/07rgt08", "Chuckle, chortle"),
    ),
    "audio_evt_scream": (
        AudioSetClass(6, "/m/07p6fty", "Shout"),
        AudioSetClass(7, "/m/07q4ntr", "Bellow"),
        AudioSetClass(9, "/m/07sr1lc", "Yell"),
        AudioSetClass(11, "/m/03qc9zr", "Screaming"),
    ),
    "audio_evt_cheer": (
        AudioSetClass(8, "/m/07rwj3x", "Whoop"),
        AudioSetClass(58, "/m/0l15bq", "Clapping"),
        AudioSetClass(61, "/m/053hz1", "Cheering"),
        AudioSetClass(62, "/m/028ght", "Applause"),
    ),
    "audio_evt_music": (AudioSetClass(132, "/m/04rlf", "Music"),),
}
"""Explicit mapping from each AutoClipper feature column to its YAMNet AudioSet-521 classes."""

FEATURE_COLUMNS: Final[tuple[str, ...]] = tuple(YAMNET_LABEL_MAP)
"""The audio-event feature columns, all Float64, in output order."""

EVENT_CLASS_INDICES: Final[dict[str, tuple[int, ...]]] = {
    column: tuple(entry.index for entry in classes) for column, classes in YAMNET_LABEL_MAP.items()
}
"""AudioSet-521 class indices aggregated (by max probability) for each feature column."""


class AudioEventsError(RuntimeError):
    """Audio-event inference failed, or the optional ``audio-events`` extra isn't installed."""


class AudioEventDetector(Protocol):
    """Turns a 16 kHz mono float32 waveform into per-frame AudioSet class probabilities."""

    def score_frames(self, waveform: npt.NDArray[np.float32], /) -> npt.NDArray[np.float32]:
        """Return a 2D array of shape ``(n_frames, n_classes)`` with probabilities in [0, 1]."""
        ...


def audio_events_available() -> bool:
    """Return True when the optional ``audio-events`` runtime (``onnxruntime``) is installed."""
    return importlib.util.find_spec("onnxruntime") is not None


def default_cache_dir(artifacts_dir: Path | None = None) -> Path:
    """Return the default directory for cached audio-event model weights."""
    root = artifacts_dir if artifacts_dir is not None else get_settings().artifacts_dir
    return root / "cache" / "audio_events"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_DOWNLOAD_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def ensure_yamnet_model(
    cache_dir: Path,
    *,
    url: str = YAMNET_MODEL_URL,
    expected_sha256: str = YAMNET_MODEL_SHA256,
    timeout_s: float = 120.0,
    client: httpx.Client | None = None,
) -> Path:
    """Download and checksum-verify the YAMNet ONNX model into `cache_dir` if not cached.

    Args:
        cache_dir: Directory where ``yamnet.onnx`` is stored. Created if needed.
        url: Download URL for the ONNX model.
        expected_sha256: Lowercase hex SHA-256 digest the downloaded bytes must match.
        timeout_s: HTTP request timeout in seconds.
        client: Optional preconfigured ``httpx.Client`` (used by tests).

    Returns:
        Path to the verified cached ONNX model file.

    Raises:
        AudioEventsError: Download failed, HTTP status was non-2xx, or the checksum mismatched.
    """
    target = cache_dir / YAMNET_MODEL_FILENAME
    if target.is_file():
        actual = _sha256_file(target)
        if actual == expected_sha256:
            return target
        log.warning(
            "cached yamnet model checksum mismatch path=%s expected=%s actual=%s; re-downloading",
            target,
            expected_sha256,
            actual,
        )

    cache_dir.mkdir(parents=True, exist_ok=True)
    tmp_path: Path | None = None
    owns_client = client is None
    http = client if client is not None else httpx.Client(follow_redirects=True, timeout=timeout_s)
    try:
        with tempfile.NamedTemporaryFile(
            dir=cache_dir, prefix=".yamnet-", suffix=".tmp", delete=False
        ) as tmp:
            tmp_path = Path(tmp.name)
            digest = hashlib.sha256()
            log.info("downloading yamnet model url=%s dest=%s", url, target)
            with http.stream("GET", url) as response:
                response.raise_for_status()
                for chunk in response.iter_bytes(_DOWNLOAD_CHUNK_BYTES):
                    tmp.write(chunk)
                    digest.update(chunk)
        actual = digest.hexdigest()
        if actual != expected_sha256:
            raise AudioEventsError(
                f"downloaded YAMNet weights failed SHA-256 check "
                f"(expected {expected_sha256}, got {actual})"
            )
        tmp_path.replace(target)
        tmp_path = None
        return target
    except AudioEventsError:
        raise
    except (httpx.HTTPError, OSError) as exc:
        raise AudioEventsError(f"failed to download YAMNet model from {url}: {exc}") from exc
    finally:
        if owns_client:
            http.close()
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)


@dataclass
class YamnetEventDetector:
    """YAMNet AudioSet-521 event detector running on CPU via ``onnxruntime``.

    The ONNX session is loaded lazily on first call and reused across windows.

    Args:
        cache_dir: Where to cache ``yamnet.onnx`` when downloading at runtime. Defaults to
            ``<artifacts_dir>/cache/audio_events``.
        model_path: Optional path to an existing ``yamnet.onnx`` file (skips downloading).
        intra_op_num_threads: Optional ONNX Runtime CPU thread cap (0 lets ORT choose).
    """

    cache_dir: Path | None = None
    model_path: Path | None = None
    intra_op_num_threads: int = 0
    _session: object = field(default=None, init=False, repr=False)
    _input_name: str = field(default="waveform", init=False, repr=False)
    _output_name: str = field(default="output_0", init=False, repr=False)

    def score_frames(self, waveform: npt.NDArray[np.float32], /) -> npt.NDArray[np.float32]:
        """Score a 1D 16 kHz mono float32 waveform into ``(n_frames, 521)`` probabilities.

        Raises:
            AudioEventsError: ``onnxruntime`` is missing, weights cannot be loaded, or inference
                fails.
        """
        samples = np.asarray(waveform, dtype=np.float32)
        if samples.ndim != 1:
            raise ValueError(f"waveform must be 1D, got shape {samples.shape}")
        session = self._load()
        try:
            # onnxruntime ships incomplete stubs across versions.
            outputs = session.run(  # type: ignore[attr-defined]
                [self._output_name], {self._input_name: samples}
            )
            scores = np.asarray(outputs[0], dtype=np.float32)
        except Exception as exc:
            raise AudioEventsError(f"YAMNet inference failed: {exc}") from exc
        if scores.ndim != 2 or scores.shape[1] != YAMNET_NUM_CLASSES:
            raise AudioEventsError(
                f"unexpected YAMNet output shape {scores.shape}; "
                f"expected (n_frames, {YAMNET_NUM_CLASSES})"
            )
        return scores

    def _load(self) -> object:
        if self._session is None:
            try:
                ort = importlib.import_module("onnxruntime")
            except ImportError as exc:
                raise AudioEventsError(
                    "audio event detection needs onnxruntime; install it with "
                    "`uv sync --extra audio-events`"
                ) from exc
            weights = (
                self.model_path
                if self.model_path is not None
                else ensure_yamnet_model(
                    self.cache_dir if self.cache_dir is not None else default_cache_dir()
                )
            )
            if not weights.is_file():
                raise AudioEventsError(f"YAMNet model file not found: {weights}")
            try:
                options = ort.SessionOptions()
                if self.intra_op_num_threads > 0:
                    options.intra_op_num_threads = self.intra_op_num_threads
                session = ort.InferenceSession(
                    str(weights), sess_options=options, providers=["CPUExecutionProvider"]
                )
                self._input_name = session.get_inputs()[0].name
                self._output_name = session.get_outputs()[0].name
                self._session = session
            except Exception as exc:
                raise AudioEventsError(f"failed to load YAMNet ONNX model {weights}: {exc}") from (
                    exc
                )
        return self._session


def aggregate_event_probabilities(
    frame_scores: npt.NDArray[np.floating],
    label_indices: Mapping[str, Sequence[int]] = EVENT_CLASS_INDICES,
) -> dict[str, float]:
    """Collapse frame-level class probabilities into per-window max event probabilities.

    Args:
        frame_scores: 2D array of shape ``(n_frames, n_classes)``.
        label_indices: Mapping from output feature column name to class indices.

    Returns:
        Dictionary mapping each column name in `label_indices` to a float in ``[0.0, 1.0]``.

    Raises:
        ValueError: `frame_scores` is not 2D, any mapped index is out of range, a group has no
            indices, or the mapped probabilities contain NaN/inf.
    """
    scores = np.asarray(frame_scores, dtype=np.float64)
    if scores.ndim != 2:
        raise ValueError(f"frame_scores must be 2D (n_frames, n_classes), got shape {scores.shape}")
    n_frames, n_classes = scores.shape
    for column, indices in label_indices.items():
        if not indices:
            raise ValueError(f"label mapping for {column!r} has no class indices")
        for idx in indices:
            if not 0 <= idx < n_classes:
                raise ValueError(
                    f"class index {idx} for {column!r} is out of range for {n_classes} classes"
                )
    if n_frames == 0:
        return dict.fromkeys(label_indices, 0.0)

    out: dict[str, float] = {}
    for column, indices in label_indices.items():
        subset = scores[:, list(indices)]
        if not np.all(np.isfinite(subset)):
            raise ValueError(f"non-finite probabilities in frame_scores for {column!r}")
        out[column] = float(np.clip(subset.max(), 0.0, 1.0))
    return out


def select_top_windows(
    windows: pl.DataFrame,
    scores: pl.DataFrame | Sequence[float] | npt.NDArray[np.floating] | pl.Series,
    *,
    top_k: int = DEFAULT_TOP_K,
) -> list[str]:
    """Choose the top `top_k` windows by descending score (deterministic tie-breaking).

    Args:
        windows: Windows table containing at least ``window_id`` and ``start_s``.
        scores: Either a ``pl.DataFrame`` with ``window_id`` and ``score`` columns, or a 1D
            sequence/array/Series of floats aligned with `windows` rows.
        top_k: Maximum number of windows to return (must be at least 1).

    Returns:
        List of selected ``window_id`` strings ordered by descending score, breaking ties by
        ascending ``start_s`` and then ``window_id``.

    Raises:
        ValueError: `top_k` is less than 1, `windows` or `scores` is malformed, lengths or IDs
            mismatch, or any score is null or non-finite.
    """
    if isinstance(top_k, bool) or top_k < 1:
        raise ValueError(f"top_k must be an integer >= 1, got {top_k!r}")
    missing = sorted({"window_id", "start_s"} - set(windows.columns))
    if missing:
        raise ValueError(f"windows is missing columns: {', '.join(missing)}")
    if windows["window_id"].null_count() or windows["start_s"].null_count():
        raise ValueError("windows has nulls in window_id or start_s")
    if windows["window_id"].n_unique() != windows.height:
        raise ValueError("windows lists a window_id more than once")

    if isinstance(scores, pl.DataFrame):
        score_missing = sorted({"window_id", "score"} - set(scores.columns))
        if score_missing:
            raise ValueError(f"scores DataFrame is missing columns: {', '.join(score_missing)}")
        if scores["window_id"].null_count() or scores["score"].null_count():
            raise ValueError("scores DataFrame has nulls in window_id or score")
        if scores["window_id"].n_unique() != scores.height:
            raise ValueError("scores DataFrame lists a window_id more than once")
        known = set(windows["window_id"].to_list())
        unknown = [wid for wid in scores["window_id"].to_list() if wid not in known]
        if unknown:
            raise ValueError(f"scores names unknown window_id(s), e.g. {unknown[0]!r}")
        joined = windows.select("window_id", "start_s").join(
            scores.select("window_id", pl.col("score").cast(pl.Float64)),
            on="window_id",
            how="inner",
        )
    else:
        arr = np.asarray(scores, dtype=np.float64)
        if arr.ndim != 1 or len(arr) != windows.height:
            raise ValueError(
                f"scores must be 1D with one value per window ({windows.height}), got {arr.shape}"
            )
        joined = windows.select("window_id", "start_s").with_columns(
            pl.Series("score", arr, dtype=pl.Float64)
        )

    if joined.height == 0:
        return []
    score_values = joined["score"].to_numpy()
    if not np.all(np.isfinite(score_values)):
        raise ValueError("scores must be finite numbers (no NaN or inf)")

    ranked = joined.sort(["score", "start_s", "window_id"], descending=[True, False, False])
    return ranked.head(top_k)["window_id"].to_list()


def compute_audio_event_features(
    windows: pl.DataFrame,
    wav_path: Path,
    *,
    detector: AudioEventDetector | None = None,
    window_ids: Collection[str] | None = None,
    scores: pl.DataFrame | Sequence[float] | npt.NDArray[np.floating] | pl.Series | None = None,
    top_k: int | None = None,
    cache_dir: Path | None = None,
) -> pl.DataFrame:
    """Compute per-window audio-event probabilities for one VOD's normalized WAV.

    Args:
        windows: Rows of the ``windows`` table of one VOD (`autoclipper.labels.windows`).
        wav_path: The VOD's 16 kHz mono 16-bit WAV (``data/interim/audio/<video_id>.wav``).
        detector: Optional `AudioEventDetector` (defaults to `YamnetEventDetector`). Tests pass
            a fake detector so CI runs without ``onnxruntime`` or model weights.
        window_ids: Optional explicit subset of ``window_id``s to score. Unselected windows stay
            in the output with ``null`` probabilities.
        scores: Optional window scores used with `top_k` (default `DEFAULT_TOP_K`) to select the
            highest-scoring windows to run through the model. Cannot be combined with `window_ids`.
        top_k: Number of top-scoring windows to run when `scores` is passed.
        cache_dir: Model cache directory used by the default `YamnetEventDetector`.

    Returns:
        One row per window in ascending ``start_s`` order: ``window_id``, the four
        `FEATURE_COLUMNS` (``Float64``; ``null`` for unselected windows), and
        ``feature_version`` (``audio-events-v1``).

    Raises:
        ValueError: Invalid windows table, invalid subset arguments, or non-finite probabilities.
        AudioFormatError: The WAV is missing, unreadable, truncated or not 16 kHz mono 16-bit PCM.
        AudioEventsError: ``onnxruntime`` is not installed or the model failed to load/run.
    """
    if window_ids is not None and scores is not None:
        raise ValueError("pass either window_ids or scores, not both")
    if top_k is not None and scores is None:
        raise ValueError("top_k requires scores")

    # Resolve positional scores against caller's input order before `_checked_windows` sorts rows.
    selected: set[str] | None = None
    if scores is not None:
        effective_k = DEFAULT_TOP_K if top_k is None else top_k
        checked_for_selection = _checked_windows(windows)
        if isinstance(scores, pl.DataFrame):
            selected = set(select_top_windows(checked_for_selection, scores, top_k=effective_k))
        else:
            selected = set(select_top_windows(windows, scores, top_k=effective_k))
        windows = checked_for_selection
    else:
        windows = _checked_windows(windows)
        if window_ids is not None:
            known = set(windows["window_id"].to_list())
            unknown = [wid for wid in window_ids if wid not in known]
            if unknown:
                raise ValueError(f"window_ids contains unknown window_id(s), e.g. {unknown[0]!r}")
            selected = set(window_ids)

    if windows.height == 0:
        return pl.DataFrame(
            schema={
                "window_id": pl.String,
                **dict.fromkeys(FEATURE_COLUMNS, pl.Float64),
                "feature_version": pl.String,
            }
        )

    try:
        reader = wave.open(str(wav_path), "rb")  # noqa: SIM115 (closed below)
    except (OSError, wave.Error, EOFError) as exc:
        hint = " (RF64 WAVs over 4 GiB aren't supported)" if "RIFF" in str(exc) else ""
        raise AudioFormatError(f"can't read WAV {wav_path}: {exc}{hint}") from exc

    rows_by_col: dict[str, list[float | None]] = {column: [] for column in FEATURE_COLUMNS}
    empty_selected = 0
    scored_count = 0
    active_detector = detector

    with reader:
        layout = (reader.getframerate(), reader.getnchannels(), reader.getsampwidth())
        if layout != (SAMPLE_RATE_HZ, 1, 2):
            raise AudioFormatError(
                f"{wav_path} is {layout[0]} Hz, {layout[1]} channel(s), {8 * layout[2]}-bit; "
                f"expected {SAMPLE_RATE_HZ} Hz mono 16-bit (run normalize_audio first)"
            )
        total_frames = reader.getnframes()

        for wid, start_s, end_s in zip(
            windows["window_id"].to_list(),
            windows["start_s"].to_list(),
            windows["end_s"].to_list(),
            strict=True,
        ):
            if selected is not None and wid not in selected:
                for column in FEATURE_COLUMNS:
                    rows_by_col[column].append(None)
                continue

            start_sample = max(0, round(start_s * SAMPLE_RATE_HZ))
            end_sample = min(total_frames, round(end_s * SAMPLE_RATE_HZ))
            if start_sample >= end_sample:
                empty_selected += 1
                for column in FEATURE_COLUMNS:
                    rows_by_col[column].append(0.0)
                continue

            n_read = end_sample - start_sample
            reader.setpos(start_sample)
            raw = reader.readframes(n_read)
            if len(raw) != 2 * n_read:
                raise AudioFormatError(f"{wav_path} ended early: its header promised more audio")
            waveform = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
            if active_detector is None:
                active_detector = YamnetEventDetector(cache_dir=cache_dir)
            frame_scores = active_detector.score_frames(waveform)
            event_probs = aggregate_event_probabilities(frame_scores)
            scored_count += 1
            for column in FEATURE_COLUMNS:
                rows_by_col[column].append(event_probs[column])

    if empty_selected:
        log.warning(
            "audio-event windows without audio video_id=%s n=%d of=%d path=%s",
            windows["video_id"][0],
            empty_selected,
            windows.height,
            wav_path,
        )
    log.debug(
        "audio-event features video_id=%s scored=%d total=%d path=%s",
        windows["video_id"][0],
        scored_count,
        windows.height,
        wav_path,
    )

    return pl.DataFrame(
        {
            "window_id": windows["window_id"],
            **{
                column: pl.Series(column, values, dtype=pl.Float64)
                for column, values in rows_by_col.items()
            },
            "feature_version": pl.Series(
                "feature_version", [AUDIO_EVENTS_FEATURE_VERSION] * windows.height, dtype=pl.String
            ),
        }
    )
