"""The chat delay and clip alignment study over a collection (#36, ``experiments/exp-001``).

`load_collection` reads the listed VODs from a data directory (the ingest layout:
``processed/vods.parquet``, ``processed/broadcasters.parquet``, one clip snapshot per VOD under
``processed/clips/``, ``interim/chat/`` and ``interim/audio/``) and builds each VOD's per-second
series, cached under a directory of the caller's choice. `run_study` measures, in VOD time:

1. **Chat delay.** The average chat signal around *loud events*: the loudest separate seconds
   of the streamer's audio (`autoclipper.labels.alignment.event_times`), which need no clips.
   Where the smoothed profile peaks is the delay; where it stays above half its height is how
   long chat reacts. The whole-VOD `lagged_correlation` is reported as a weaker second estimate.
2. **Where clips sit.** Chat and loudness around moment starts and around the starts and ends of
   each VOD's most-viewed clips, for live clips and clips made after the stream (``created_at``
   past the VOD's end) separately.
3. **Spike alignment.** Chat and audio spikes per window size and threshold, with the features'
   z-scores. How often they fall within the match tolerance of a moment, against random windows
   (moments cover much of a busy VOD); how often the moments holding the most-viewed clips have
   one; how often chat and audio spikes coincide. Chat spikes are shifted back by the pooled
   chat delay.
4. **Padding inputs.** Where the loudest second and the (delay-shifted) chat peak fall relative
   to each most-viewed clip, next to where they would fall by chance in the searched span.
5. **Clip timing.** When live clips were created relative to their reported VOD position
   (`clip_timing`): a check of the positions that needs no audio or chat.

Profiles are averaged per VOD, then over VODs (each VOD counts once), smoothed before peaks are
read, with 95 % intervals from resampling VODs. Random anchors, as many per VOD as the real ones,
give each chat profile's chance level. Clips starting at or after the VOD's end are dropped and
clips running past it are cut at the end (pilot report § 10). The output holds counts and
statistics only: no chat text, no pseudonyms, no clip titles or IDs.
"""

import json
import logging
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, Final

import numpy as np
import polars as pl
import yaml

from autoclipper.eval.bootstrap import bootstrap_mean
from autoclipper.eval.metrics import TOP_CLIPS, top_clip_moments
from autoclipper.features.audio import AUDIO_FEATURE_VERSION
from autoclipper.features.chat import CHAT_FEATURE_VERSION, Lexicon
from autoclipper.labels import alignment as al
from autoclipper.labels.moments import merge_clips_into_moments

log = logging.getLogger(__name__)

ALL: Final = "all"
"""Group (and clip class) name of the pooled result."""
ANCHORS: Final = ("loud_event", "moment_start", "top_clip_start", "top_clip_end")
CLIP_ANCHORS: Final = ANCHORS[1:]
"""Anchors that come from clips, split into live and post-stream clips."""
CLIP_CLASSES: Final = ("live", "post_stream")
SIGNALS: Final = ("chat", "loudness")
SERIES_VERSION: Final = "alignment-series-v1"


class StudyError(ValueError):
    """The study's inputs or configuration are unusable."""


@dataclass(frozen=True, slots=True)
class StudyConfig:
    """Parameters of one study run (``experiments/<id>/config.yaml``).

    Attributes:
        experiment: Experiment ID, e.g. ``exp-001-chat-lag-alignment``.
        vod_list: The active VOD list, relative to the repository root.
        exclude_video_ids: VODs that must not be in the list (the run fails if one is).
        lexicon: The chat lexicon file, relative to the repository root.
        seed: Seed for bootstrap resampling and random anchors.
        bootstrap_resamples: Resamples per interval.
        null_draws: Random-anchor draws for a chat profile's chance level.
        detrend_s: Width of the moving average removed from both signals.
        smooth_s: Width (odd) of the moving average applied to a profile before reading it.
        loud_event_quantile: A loud event is a second at or above this quantile of the VOD's
            detrended loudness (per VOD, so quiet and loud channels both get events).
        loud_event_suppress_s: Loud events are at least this far apart.
        lag_range_s: Where a profile's peak is searched, and the whole-VOD lags.
        profile_range_s: Offsets around each anchor.
        baseline_range_s: Offsets that set a profile's baseline for its half-height span.
        window_s: Main window size for spikes.
        z: Main spike threshold (VOD z-score).
        suppress_s: Spike suppression distance between window centres.
        match_tolerance_s: A spike matches a moment within this gap.
        sensitivity_window_s: Window sizes of the sensitivity table.
        sensitivity_z: Thresholds of the sensitivity table.
        top_clips: Most-viewed clips per VOD.
        padding_search_s: Seconds searched before a clip's start and after its end for peaks.
    """

    experiment: str
    vod_list: str
    exclude_video_ids: tuple[str, ...]
    lexicon: str
    seed: int
    bootstrap_resamples: int
    null_draws: int
    detrend_s: int
    smooth_s: int
    loud_event_quantile: float
    loud_event_suppress_s: float
    lag_range_s: tuple[int, int]
    profile_range_s: tuple[int, int]
    baseline_range_s: tuple[int, int]
    window_s: float
    z: float
    suppress_s: float
    match_tolerance_s: float
    sensitivity_window_s: tuple[float, ...]
    sensitivity_z: tuple[float, ...]
    top_clips: int = TOP_CLIPS
    padding_search_s: tuple[float, float] = (30.0, 60.0)

    @classmethod
    def from_yaml(cls, path: Path) -> "StudyConfig":
        """Read and check a config file.

        Raises:
            StudyError: The file isn't a mapping, a key is missing or unknown, or a value is out
                of range.
        """
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise StudyError(f"{path} must be a YAML mapping")
        spike = raw.pop("spike", {})
        loud = raw.pop("loud_event", {})
        sensitivity = raw.pop("sensitivity", {})
        if not all(isinstance(m, dict) for m in (spike, loud, sensitivity)):
            raise StudyError(f"{path}: spike, loud_event and sensitivity must be mappings")
        try:
            config = cls(
                experiment=str(raw.pop("experiment")),
                vod_list=str(raw.pop("vod_list")),
                exclude_video_ids=tuple(str(v) for v in raw.pop("exclude_video_ids", [])),
                lexicon=str(raw.pop("lexicon")),
                seed=int(raw.pop("seed")),
                bootstrap_resamples=int(raw.pop("bootstrap_resamples")),
                null_draws=int(raw.pop("null_draws")),
                detrend_s=int(raw.pop("detrend_s")),
                smooth_s=int(raw.pop("smooth_s")),
                loud_event_quantile=float(loud.pop("quantile")),
                loud_event_suppress_s=float(loud.pop("suppress_s")),
                lag_range_s=_pair(raw.pop("lag_range_s"), int),
                profile_range_s=_pair(raw.pop("profile_range_s"), int),
                baseline_range_s=_pair(raw.pop("baseline_range_s"), int),
                window_s=float(spike.pop("window_s")),
                z=float(spike.pop("z")),
                suppress_s=float(spike.pop("suppress_s")),
                match_tolerance_s=float(spike.pop("match_tolerance_s")),
                sensitivity_window_s=tuple(float(v) for v in sensitivity.pop("window_s")),
                sensitivity_z=tuple(float(v) for v in sensitivity.pop("z")),
                top_clips=int(raw.pop("top_clips", TOP_CLIPS)),
                padding_search_s=_pair(raw.pop("padding_search_s", [30.0, 60.0]), float),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise StudyError(f"{path}: {exc!r}") from None
        leftover = sorted([*raw, *spike, *loud, *sensitivity])
        if leftover:
            raise StudyError(f"{path}: unknown keys {', '.join(leftover)}")
        config.check()
        return config

    def check(self) -> None:
        """Raise `StudyError` when a value is out of range."""
        lo, hi = self.lag_range_s
        plo, phi = self.profile_range_s
        blo, bhi = self.baseline_range_s
        if not (lo < hi and plo <= lo and hi <= phi and plo <= blo <= bhi <= phi):
            raise StudyError("lag, profile and baseline ranges must nest: profile ⊇ lag, baseline")
        if min(self.bootstrap_resamples, self.null_draws, self.detrend_s, self.top_clips) < 1:
            raise StudyError("bootstrap_resamples, null_draws, detrend_s, top_clips must be >= 1")
        if self.smooth_s < 1 or self.smooth_s % 2 == 0:
            raise StudyError("smooth_s must be an odd number of seconds")
        if not 0 < self.loud_event_quantile < 1:
            raise StudyError("loud_event quantile must be between 0 and 1")
        windows = (self.window_s, *self.sensitivity_window_s)
        if min(windows) < al.SERIES_STEP_S or any(w % al.SERIES_STEP_S for w in windows):
            raise StudyError("window sizes must be whole multiples of the 1 s series step")
        if (
            min(self.suppress_s, self.match_tolerance_s, self.loud_event_suppress_s) < 0
            or min(self.padding_search_s) < 0
        ):
            raise StudyError("suppression, tolerance and padding search must be >= 0")

    def as_dict(self) -> dict[str, Any]:
        """The parameters as plain JSON values."""
        return {
            "experiment": self.experiment,
            "vod_list": self.vod_list,
            "exclude_video_ids": list(self.exclude_video_ids),
            "lexicon": self.lexicon,
            "seed": self.seed,
            "bootstrap_resamples": self.bootstrap_resamples,
            "null_draws": self.null_draws,
            "detrend_s": self.detrend_s,
            "smooth_s": self.smooth_s,
            "loud_event": {
                "quantile": self.loud_event_quantile,
                "suppress_s": self.loud_event_suppress_s,
            },
            "lag_range_s": list(self.lag_range_s),
            "profile_range_s": list(self.profile_range_s),
            "baseline_range_s": list(self.baseline_range_s),
            "spike": {
                "window_s": self.window_s,
                "z": self.z,
                "suppress_s": self.suppress_s,
                "match_tolerance_s": self.match_tolerance_s,
            },
            "sensitivity": {
                "window_s": list(self.sensitivity_window_s),
                "z": list(self.sensitivity_z),
            },
            "top_clips": self.top_clips,
            "padding_search_s": list(self.padding_search_s),
        }

    @property
    def offsets(self) -> list[int]:
        """Profile offsets in seconds, one per series bin."""
        return list(range(self.profile_range_s[0], self.profile_range_s[1] + 1))


def _pair[T: (int, float)](value: object, kind: type[T]) -> tuple[T, T]:
    if not isinstance(value, list | tuple) or len(value) != 2:
        raise ValueError(f"expected a [low, high] pair, got {value!r}")
    return kind(value[0]), kind(value[1])


# --- Loading ------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class VodData:
    """One VOD's inputs to the study.

    Attributes:
        video_id: The VOD.
        login: Its channel's login.
        duration_s: Its length.
        clips: Its placed clips (offset known, starting before the end, cut at the end), with
            ``ended_at``, the VOD's end in wall-clock time.
        moments: Moments merged from `clips`.
        series: Per-second series (`autoclipper.labels.alignment.series_frame`).
        n_clips: Clips in its snapshot.
        n_clips_without_offset: Clips without a VOD offset (not placed).
        n_clips_dropped: Clips starting at or after the VOD's end (dropped).
        n_clips_cut: Clips running past the end (cut at it).
        n_chat_after_end: Chat messages at or after the VOD's end (outside every bin).
    """

    video_id: str
    login: str
    duration_s: float
    clips: pl.DataFrame
    moments: pl.DataFrame
    series: pl.DataFrame
    n_clips: int
    n_clips_without_offset: int
    n_clips_dropped: int
    n_clips_cut: int
    n_chat_after_end: int


def place_clips(clips: pl.DataFrame, duration_s: float) -> tuple[pl.DataFrame, int, int, int]:
    """Clips with a VOD offset that start before `duration_s`, cut so they end by it.

    Returns:
        ``(clips, n_without_offset, n_dropped, n_cut)``.
    """
    with_offset = clips.filter(pl.col("vod_offset_s").is_not_null())
    n_without = clips.height - with_offset.height
    inside = with_offset.filter(pl.col("vod_offset_s") < duration_s)
    n_dropped = with_offset.height - inside.height
    past = pl.col("vod_offset_s") + pl.col("duration_s") > duration_s
    n_cut = inside.filter(past).height
    placed = inside.with_columns(
        pl.when(past)
        .then(duration_s - pl.col("vod_offset_s"))
        .otherwise(pl.col("duration_s"))
        .alias("duration_s")
    )
    return placed, n_without, n_dropped, n_cut


def snapshot_tables(data_dir: Path) -> tuple[dict[str, list[Path]], Path | None]:
    """Where each VOD's clip snapshot is, by the same rules as `autoclipper.labels.dataset`.

    Ingest writes one snapshot per VOD: ``processed/clips/<snapshot_id>.json`` (naming its VOD)
    next to ``<snapshot_id>.parquet``, so a VOD without clips still has one. The synthetic
    dataset has a root ``manifest.json`` and one combined table for all its VODs instead.

    Returns:
        ``({video_id: [tables]}, combined table or None)``.
    """
    clips_dir = data_dir / "processed" / "clips"
    per_vod: dict[str, list[Path]] = {}
    for path in sorted(clips_dir.glob("*.json")):
        manifest = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(manifest, dict) and manifest.get("video_id"):
            per_vod.setdefault(str(manifest["video_id"]), []).append(path.with_suffix(".parquet"))
    combined = None
    root_manifest = data_dir / "manifest.json"
    if root_manifest.is_file():
        manifest = json.loads(root_manifest.read_text(encoding="utf-8"))
        if manifest.get("generator") == "autoclipper.synthetic":
            combined = clips_dir / f"{manifest['clips_snapshot']['snapshot_id']}.parquet"
    return per_vod, combined


def _snapshot_for(
    per_vod: Mapping[str, Sequence[Path]], combined: Path | None, video_id: str
) -> pl.DataFrame:
    tables = per_vod.get(video_id, [])
    if len(tables) > 1:
        raise StudyError(f"VOD {video_id} has {len(tables)} clip snapshots")
    table = tables[0] if tables else combined
    if table is None or not table.is_file():
        raise StudyError(f"VOD {video_id} has no clip snapshot")
    return pl.read_parquet(table).filter(pl.col("video_id") == video_id)


def _cached_series(
    cache_dir: Path,
    video_id: str,
    duration_s: float,
    chat_path: Path,
    wav_path: Path,
    lexicon: Lexicon,
) -> pl.DataFrame:
    key = {
        "series_version": SERIES_VERSION,
        "chat_feature_version": f"{CHAT_FEATURE_VERSION}+lexicon-v{lexicon.version}",
        "audio_feature_version": AUDIO_FEATURE_VERSION,
        "duration_s": duration_s,
        "chat": [chat_path.stat().st_size, chat_path.stat().st_mtime_ns],
        "wav": [wav_path.stat().st_size, wav_path.stat().st_mtime_ns],
    }
    table = cache_dir / f"{video_id}.parquet"
    meta = cache_dir / f"{video_id}.json"
    if table.is_file() and meta.is_file():
        try:
            if json.loads(meta.read_text(encoding="utf-8")) == key:
                return pl.read_parquet(table)
        except (OSError, ValueError):
            pass
    messages = pl.read_parquet(chat_path)
    series = al.series_frame(video_id, duration_s, messages, wav_path, lexicon=lexicon)
    cache_dir.mkdir(parents=True, exist_ok=True)
    series.write_parquet(table)
    meta.write_text(json.dumps(key, indent=2) + "\n", encoding="utf-8")
    return series


def load_collection(
    data_dir: Path,
    video_ids: Sequence[str],
    *,
    lexicon: Lexicon,
    cache_dir: Path,
) -> list[VodData]:
    """Read the listed VODs' metadata, clips, chat and audio, in list order.

    Raises:
        StudyError: A VOD repeats or lacks metadata, a snapshot, chat or audio, or its channel
            is unknown.
    """
    if len(set(video_ids)) != len(video_ids):
        raise StudyError("the VOD list repeats a VOD")
    vods = pl.read_parquet(data_dir / "processed" / "vods.parquet")
    logins = dict(
        pl.read_parquet(data_dir / "processed" / "broadcasters.parquet")
        .select("broadcaster_id", "login")
        .iter_rows()
    )
    per_vod, combined = snapshot_tables(data_dir)
    out: list[VodData] = []
    for video_id in video_ids:
        meta = vods.filter(pl.col("video_id") == video_id)
        if meta.height != 1:
            raise StudyError(f"VOD {video_id} has {meta.height} rows in vods.parquet")
        broadcaster_id = str(meta["broadcaster_id"][0])
        if broadcaster_id not in logins:
            raise StudyError(f"VOD {video_id}: broadcaster {broadcaster_id} isn't in the table")
        duration_s = float(meta["duration_s"][0])
        chat_path = data_dir / "interim" / "chat" / f"{video_id}.parquet"
        wav_path = data_dir / "interim" / "audio" / f"{video_id}.wav"
        for path in (chat_path, wav_path):
            if not path.is_file():
                raise StudyError(f"VOD {video_id}: {path.name} is missing")
        snapshot = _snapshot_for(per_vod, combined, video_id)
        clips, n_without, n_dropped, n_cut = place_clips(snapshot, duration_s)
        moments = merge_clips_into_moments(clips).moments
        series = _cached_series(cache_dir, video_id, duration_s, chat_path, wav_path, lexicon)
        n_after = (
            pl.scan_parquet(chat_path)
            .filter(pl.col("offset_s") >= duration_s)
            .select(pl.len())
            .collect()
            .item()
        )
        out.append(
            VodData(
                video_id=video_id,
                login=str(logins[broadcaster_id]),
                duration_s=duration_s,
                clips=clips.with_columns(
                    ended_at=pl.lit(meta["created_at"][0]) + timedelta(seconds=duration_s)
                ),
                moments=moments,
                series=series,
                n_clips=snapshot.height,
                n_clips_without_offset=n_without,
                n_clips_dropped=n_dropped,
                n_clips_cut=n_cut,
                n_chat_after_end=int(n_after),
            )
        )
        log.info(
            "loaded VOD video_id=%s login=%s moments=%d bins=%d",
            video_id,
            out[-1].login,
            moments.height,
            series.height,
        )
    return out


# --- Per-VOD measurements -----------------------------------------------------------------------


def top_clips(vod: VodData, n: int) -> pl.DataFrame:
    """The VOD's `n` most-viewed placed clips (ties to the smaller clip ID), as evaluation picks."""
    return vod.clips.sort(["view_count", "clip_id"], descending=[True, False]).head(n)


def _anchors(
    vod: VodData, loud: al.FloatArray, config: StudyConfig
) -> dict[str, tuple[list[float], list[str]]]:
    """Anchor times per anchor type, each with its clip class (``all`` for loud events)."""
    # A constant signal (silence, or audio that's all alike) has no loud events.
    varies = loud.size > 0 and float(loud.max()) > float(loud.min())
    threshold = float(np.quantile(loud, config.loud_event_quantile)) if varies else np.inf
    events = al.event_times(
        loud, threshold=threshold, suppress_s=config.loud_event_suppress_s
    ).tolist()
    out: dict[str, tuple[list[float], list[str]]] = {"loud_event": (events, [ALL] * len(events))}
    post = (pl.col("created_at") > pl.col("ended_at")).alias("post_stream")
    clips = vod.clips.select("clip_id", "created_at", post)
    by_id = {r["clip_id"]: r for r in clips.iter_rows(named=True)}
    starts: list[float] = []
    classes: list[str] = []
    for row in vod.moments.iter_rows(named=True):
        first = min(
            (by_id[c] for c in row["clip_ids"]), key=lambda r: (r["created_at"], r["clip_id"])
        )
        starts.append(float(row["start_s"]))
        classes.append("post_stream" if first["post_stream"] else "live")
    out["moment_start"] = (starts, classes)
    top = top_clips(vod, config.top_clips).with_columns(post)
    top_classes = ["post_stream" if p else "live" for p in top["post_stream"].to_list()]
    top_starts = top["vod_offset_s"].to_list()
    top_ends = (top["vod_offset_s"] + top["duration_s"]).to_list()
    out["top_clip_start"] = ([float(t) for t in top_starts], top_classes)
    out["top_clip_end"] = ([float(t) for t in top_ends], top_classes)
    return out


@dataclass(frozen=True, slots=True)
class _VodMeasure:
    video_id: str
    login: str
    lags: list[int]
    xcorr: al.FloatArray
    # (signal, anchor, clip_class) -> per-VOD mean profile; all NaN when there are no anchors.
    profiles: dict[tuple[str, str, str], al.FloatArray]
    counts: dict[tuple[str, str], int]
    # anchor -> draws x offsets: chat around random anchors, as many as the real ones.
    nulls: dict[str, al.FloatArray]
    chat: al.FloatArray


def _measure_vod(vod: VodData, config: StudyConfig, rng: np.random.Generator) -> _VodMeasure:
    chat = al.chat_signal(vod.series["chat_count"].to_numpy(), detrend_bins=config.detrend_s)
    loud = al.loudness_signal(vod.series["rms_max"].to_numpy(), detrend_bins=config.detrend_s)
    lags = list(range(config.lag_range_s[0], config.lag_range_s[1] + 1))
    offsets = config.offsets
    width = len(offsets)
    anchors = _anchors(vod, loud, config)
    profiles: dict[tuple[str, str, str], al.FloatArray] = {}
    counts: dict[tuple[str, str], int] = {}
    nulls: dict[str, al.FloatArray] = {}
    for anchor in ANCHORS:
        times, classes = anchors[anchor]
        times_a = np.asarray(times, dtype=np.float64)
        classes_a = np.asarray(classes, dtype=object)
        for clip_class in (ALL, *CLIP_CLASSES) if anchor in CLIP_ANCHORS else (ALL,):
            pick = times_a if clip_class == ALL else times_a[classes_a == clip_class]
            counts[(anchor, clip_class)] = int(pick.size)
            for name, signal in (("chat", chat), ("loudness", loud)):
                profiles[(name, anchor, clip_class)] = al.mean_profile(
                    al.event_profiles(signal, pick.tolist(), offsets)
                )
        draws = np.full((config.null_draws, width), np.nan)
        if chat.size and times_a.size:
            for d in range(config.null_draws):
                random_times = rng.uniform(0, chat.size, size=times_a.size).tolist()
                draws[d] = al.mean_profile(al.event_profiles(chat, random_times, offsets))
        nulls[anchor] = draws
    return _VodMeasure(
        video_id=vod.video_id,
        login=vod.login,
        lags=lags,
        xcorr=al.lagged_correlation(loud, chat, lags),
        profiles=profiles,
        counts=counts,
        nulls=nulls,
        chat=chat,
    )


def _top_moment_ids(vod: VodData, n_clips: int) -> set[str]:
    moment_clip_ids = {
        str(r["moment_id"]): list(r["clip_ids"]) for r in vod.moments.iter_rows(named=True)
    }
    views = {str(c): int(v) for c, v in vod.clips.select("clip_id", "view_count").iter_rows()}
    return top_clip_moments(moment_clip_ids, views, n_clips=n_clips)


def _share(mask: np.ndarray) -> float | None:
    return float(mask.mean()) if mask.size else None


def _lift(value: float | None, base: float | None) -> float | None:
    return value / base if value is not None and base else None


def _spike_metrics(
    vod: VodData, *, window_s: float, z: float, lag_s: int, config: StudyConfig
) -> dict[str, float | None]:
    """Per-VOD spike alignment at one window size and threshold (None where undefined)."""
    win = al.window_signals(vod.series, vod.duration_s, window_s)
    starts = win["start_s"].to_numpy()
    ends = win["end_s"].to_numpy()
    m_starts = vod.moments["start_s"].to_numpy()
    m_ends = vod.moments["end_s"].to_numpy()
    tol = config.match_tolerance_s
    supp = config.suppress_s
    chat_idx = al.spikes(win["chat_z"].to_numpy(), starts, ends, threshold=z, suppress_s=supp)
    audio_idx = al.spikes(win["audio_z"].to_numpy(), starts, ends, threshold=z, suppress_s=supp)
    c_starts, c_ends = starts[chat_idx] - lag_s, ends[chat_idx] - lag_s
    a_starts, a_ends = starts[audio_idx], ends[audio_idx]

    chance = _share(al.near(starts, ends, m_starts, m_ends, tolerance_s=tol))
    chance_shifted = _share(
        al.near(starts - lag_s, ends - lag_s, m_starts, m_ends, tolerance_s=tol)
    )
    chat_raw = _share(al.near(starts[chat_idx], ends[chat_idx], m_starts, m_ends, tolerance_s=tol))
    chat = _share(al.near(c_starts, c_ends, m_starts, m_ends, tolerance_s=tol))
    audio = _share(al.near(a_starts, a_ends, m_starts, m_ends, tolerance_s=tol))

    top = vod.moments.filter(
        pl.col("moment_id").is_in(sorted(_top_moment_ids(vod, config.top_clips)))
    )
    t_starts, t_ends = top["start_s"].to_numpy(), top["end_s"].to_numpy()
    top_chat = al.near(t_starts, t_ends, c_starts, c_ends, tolerance_s=tol)
    top_audio = al.near(t_starts, t_ends, a_starts, a_ends, tolerance_s=tol)
    co = _share(al.near(c_starts, c_ends, a_starts, a_ends, tolerance_s=tol))
    co_chance = _share(al.near(starts - lag_s, ends - lag_s, a_starts, a_ends, tolerance_s=tol))
    hours = vod.duration_s / 3600
    return {
        "chat_spikes_per_hour": chat_idx.size / hours,
        "audio_spikes_per_hour": audio_idx.size / hours,
        "chance_near_moment": chance,
        "chat_near_moment_unshifted": chat_raw,
        "chat_near_moment": chat,
        "chat_lift_unshifted": _lift(chat_raw, chance),
        "chat_lift": _lift(chat, chance_shifted),
        "audio_near_moment": audio,
        "audio_lift": _lift(audio, chance),
        "top_moments": float(top.height),
        "top_with_chat": _share(top_chat),
        "top_with_audio": _share(top_audio),
        "top_with_both": _share(top_chat & top_audio),
        "top_with_neither": _share(~top_chat & ~top_audio),
        "moments_with_chat": _share(al.near(m_starts, m_ends, c_starts, c_ends, tolerance_s=tol)),
        "moments_with_audio": _share(al.near(m_starts, m_ends, a_starts, a_ends, tolerance_s=tol)),
        "chat_with_audio": co,
        "chance_near_audio": co_chance,
        "chat_audio_lift": _lift(co, co_chance),
    }


def _padding_rows(
    vod: VodData, measure: _VodMeasure, *, lag_s: int, config: StudyConfig
) -> list[dict[str, Any]]:
    """Where the loudest second and the delay-shifted chat peak fall around each top clip."""
    before, after = config.padding_search_s
    loud = al.loudness_db(vod.series["rms_max"].to_numpy())
    chat = al.smooth(measure.chat, config.smooth_s)
    n = loud.size
    rows: list[dict[str, Any]] = []
    top = top_clips(vod, config.top_clips)
    for rank, clip in enumerate(top.iter_rows(named=True), start=1):
        start = float(clip["vod_offset_s"])
        end = start + float(clip["duration_s"])
        lo = max(0, math.floor(start - before))
        loud_hi = min(n, math.ceil(end + before))
        chat_hi = min(n, math.ceil(end + after))
        if loud_hi <= lo or chat_hi <= lo:
            continue
        loud_t = float(lo + int(np.argmax(loud[lo:loud_hi])))
        chat_t = float(lo + int(np.nanargmax(chat[lo:chat_hi]))) - lag_s
        row: dict[str, Any] = {
            "video_id": vod.video_id,
            "login": vod.login,
            "rank": rank,
            "clip_duration_s": end - start,
            "loud_from_start_s": loud_t - start,
            "loud_from_end_s": loud_t - end,
            "chat_from_start_s": chat_t - start,
            "chat_from_end_s": chat_t - end,
        }
        # Chance: where a peak would fall if it were anywhere in the searched span.
        for name, hi in (("loud", loud_hi), ("chat", chat_hi)):
            spans = (max(start - lo, 0.0), end - start, max(hi - end, 0.0))
            total = sum(spans)
            for part, span in zip(("before", "inside", "after"), spans, strict=True):
                row[f"{name}_chance_{part}"] = span / total
        rows.append(row)
    return rows


# --- Aggregation --------------------------------------------------------------------------------


def _interval(values: Sequence[float | None], config: StudyConfig) -> dict[str, Any] | None:
    kept = [v for v in values if v is not None and math.isfinite(v)]
    result = bootstrap_mean(kept, seed=config.seed, n_resamples=config.bootstrap_resamples)
    if result is None:
        return None
    return {"mean": result.mean, "low": result.low, "high": result.high, "n_vods": len(kept)}


def _median_interval(values: Sequence[int], config: StudyConfig) -> dict[str, Any] | None:
    if not values:
        return None
    data = np.asarray(values, dtype=np.float64)
    ci = al.bootstrap_statistic(
        data.size,
        lambda idx: float(np.median(data[idx])),
        seed=config.seed,
        n_resamples=config.bootstrap_resamples,
    )
    return {
        "median": float(np.median(data)),
        "low": ci[0] if ci else None,
        "high": ci[1] if ci else None,
        "min": float(data.min()),
        "max": float(data.max()),
        # A peak within 3 s of the searched range's ends is most likely noise, not a lag.
        "n_at_search_edge": int(
            ((data <= config.lag_range_s[0] + 3) | (data >= config.lag_range_s[1] - 3)).sum()
        ),
        "n_vods": int(data.size),
    }


@dataclass(frozen=True, slots=True)
class _Pooled:
    summary: dict[str, Any] | None
    mean: al.FloatArray
    low: al.FloatArray
    high: al.FloatArray


def _pooled(stack: al.FloatArray, nulls: al.FloatArray | None, config: StudyConfig) -> _Pooled:
    """Smoothed mean over VODs of per-VOD profiles: peak, half-height span, intervals, chance.

    Args:
        stack: VODs x offsets; a row of NaN is a VOD without anchors and is left out.
        nulls: VODs x draws x offsets of random-anchor profiles, or None.
        config: The run's parameters.
    """
    offsets = config.offsets
    width = len(offsets)
    empty = np.full(width, np.nan)
    rows = stack[~np.isnan(stack).all(axis=1)] if stack.size else stack
    if rows.shape[0] == 0:
        return _Pooled(None, empty, empty, empty)
    k = config.smooth_s
    pooled = al.smooth(al.mean_profile(rows), k)
    peak = al.profile_peak(offsets, pooled, config.lag_range_s)
    rng = np.random.default_rng(config.seed)
    picks = rng.integers(0, rows.shape[0], size=(config.bootstrap_resamples, rows.shape[0]))
    resampled = np.stack([al.smooth(al.mean_profile(rows[p]), k) for p in picks])
    finite = np.isfinite(resampled).all(axis=0)
    low, high = empty.copy(), empty.copy()
    low[finite] = np.percentile(resampled[:, finite], 2.5, axis=0)
    high[finite] = np.percentile(resampled[:, finite], 97.5, axis=0)
    if peak is None:
        return _Pooled(None, pooled, low, high)
    peak_offsets = [
        p.offset
        for p in (al.profile_peak(offsets, r, config.lag_range_s) for r in resampled)
        if p is not None
    ]
    span = al.half_max_span(offsets, pooled, peak, config.baseline_range_s)
    off = np.asarray(offsets)
    base = (off >= config.baseline_range_s[0]) & (off <= config.baseline_range_s[1])
    base &= np.isfinite(pooled)
    summary: dict[str, Any] = {
        "peak_offset_s": peak.offset,
        "peak_low_s": float(np.percentile(peak_offsets, 2.5)),
        "peak_high_s": float(np.percentile(peak_offsets, 97.5)),
        "peak_height_z": peak.height,
        "baseline_z": float(pooled[base].mean()) if base.any() else None,
        "half_height_from_s": span[0] if span else None,
        "half_height_to_s": span[1] if span else None,
        "n_vods": int(rows.shape[0]),
    }
    if nulls is not None:
        keep = ~np.isnan(stack).all(axis=1)
        draws = nulls[keep]  # VODs x draws x offsets
        heights = []
        for d in range(draws.shape[1]):
            profile = al.smooth(al.mean_profile(draws[:, d, :]), k)
            p = al.profile_peak(offsets, profile, config.lag_range_s)
            if p is not None:
                heights.append(p.height)
        if heights:
            summary["chance_peak_height_p95_z"] = float(np.percentile(heights, 95))
    return _Pooled(summary, pooled, low, high)


def _padding_summary(rows: pl.DataFrame) -> dict[str, Any] | None:
    if rows.height == 0:
        return None
    out: dict[str, Any] = {"n_clips": rows.height}
    for signal in ("loud", "chat"):
        from_start = rows[f"{signal}_from_start_s"].to_numpy()
        from_end = rows[f"{signal}_from_end_s"].to_numpy()
        before = from_start < 0
        after = from_end >= 0
        inside = ~before & ~after
        fraction = from_start[inside] / rows["clip_duration_s"].to_numpy()[inside]
        out[signal] = {
            "share_before_start": float(before.mean()),
            "share_inside": float(inside.mean()),
            "share_after_end": float(after.mean()),
            "before_start_s_p50": float(np.median(-from_start[before])) if before.any() else None,
            "before_start_s_p90": (
                float(np.percentile(-from_start[before], 90)) if before.any() else None
            ),
            "after_end_s_p50": float(np.median(from_end[after])) if after.any() else None,
            "after_end_s_p90": float(np.percentile(from_end[after], 90)) if after.any() else None,
            "inside_position_p50": float(np.median(fraction)) if inside.any() else None,
            "chance_share_before_start": float(rows[f"{signal}_chance_before"].to_numpy().mean()),
            "chance_share_inside": float(rows[f"{signal}_chance_inside"].to_numpy().mean()),
            "chance_share_after_end": float(rows[f"{signal}_chance_after"].to_numpy().mean()),
        }
    return out


def _quantiles(values: np.ndarray) -> dict[str, float] | None:
    if values.size == 0:
        return None
    return {f"p{q}": float(np.percentile(values, q)) for q in (10, 25, 50, 75, 90)}


def clip_timing(vods: Sequence[VodData]) -> dict[str, Any]:
    """When live clips were created, relative to the VOD position Twitch reports for them.

    A clip made during the stream can only hold what had already happened, so its content ends
    no later than its creation (``created_at`` minus the VOD's start, on the VOD's timeline),
    give or take the viewer's stream delay. A reported end after the creation time means the
    reported position (``vod_offset``) is later than the content, or the VOD's ``created_at`` is
    later than its first second. Post-stream clips (made from the VOD) are left out.
    """
    frames = [
        v.clips.select(
            "duration_s",
            "vod_offset_s",
            (
                (pl.col("created_at") - (pl.col("ended_at") - timedelta(seconds=v.duration_s)))
                .dt.total_milliseconds()
                .cast(pl.Float64)
                / 1000
            ).alias("created_s"),
            pl.lit(v.duration_s).alias("vod_duration_s"),
        )
        for v in vods
    ]
    clips = pl.concat(frames) if frames else pl.DataFrame()
    if clips.height == 0:
        return {"n_live_clips": 0}
    live = clips.filter(pl.col("created_s") <= pl.col("vod_duration_s"))
    after_start = (live["created_s"] - live["vod_offset_s"]).to_numpy()
    after_end = after_start - live["duration_s"].to_numpy()
    by_length = {}
    for length in (30.0, 60.0):
        pick = live["duration_s"].to_numpy() == length
        by_length[f"{int(length)}s"] = {
            "n": int(pick.sum()),
            "created_after_start_s_p50": float(np.median(after_start[pick]))
            if pick.any()
            else None,
            "created_after_end_s_p50": float(np.median(after_end[pick])) if pick.any() else None,
        }
    return {
        "n_live_clips": live.height,
        "n_post_stream_clips": clips.height - live.height,
        "created_after_reported_start_s": _quantiles(after_start),
        "created_after_reported_end_s": _quantiles(after_end),
        "share_created_before_reported_end": float((after_end < 0).mean()) if live.height else None,
        "share_created_before_reported_start": (
            float((after_start < 0).mean()) if live.height else None
        ),
        # Created within 10 s after the reported start: the position is about the creation time.
        "share_created_0_to_10s_after_reported_start": (
            float(((after_start >= 0) & (after_start <= 10)).mean()) if live.height else None
        ),
        "by_clip_length": by_length,
    }


@dataclass(frozen=True, slots=True)
class StudyResult:
    """What `run_study` measured.

    Attributes:
        summary: Aggregates as plain JSON values (``summary.json``).
        per_vod: One row per VOD: counts, whole-VOD lag and the main spike metrics.
        xcorr: ``video_id``, ``login``, ``lag_s``, ``corr`` (chat after loudness).
        profiles: ``signal``, ``anchor``, ``group``, ``clip_class``, ``offset_s``, ``mean``,
            ``low``, ``high``: smoothed mean over the group's VODs with a 95 % band.
        padding: One row per top clip: where the loudest second and the chat peak fall.
        sensitivity: One row per (group, window size, threshold): the spike metrics' means.
    """

    summary: dict[str, Any]
    per_vod: pl.DataFrame
    xcorr: pl.DataFrame
    profiles: pl.DataFrame
    padding: pl.DataFrame
    sensitivity: pl.DataFrame


def run_study(vods: Sequence[VodData], config: StudyConfig) -> StudyResult:
    """Measure chat delay, where clips sit, spike alignment and padding inputs over `vods`.

    Raises:
        StudyError: `vods` is empty.
    """
    if not vods:
        raise StudyError("no VODs to study")
    rng = np.random.default_rng(config.seed)
    measures = [_measure_vod(v, config, rng) for v in vods]
    logins = list(dict.fromkeys(v.login for v in vods))
    groups: dict[str, list[int]] = {ALL: list(range(len(vods)))}
    groups.update({login: [i for i, v in enumerate(vods) if v.login == login] for login in logins})
    offsets = config.offsets

    # 1-2. Profiles around each anchor.
    profile_frames: list[pl.DataFrame] = []
    profiles: dict[str, Any] = {}
    for signal in SIGNALS:
        for anchor in ANCHORS:
            for clip_class in (ALL, *CLIP_CLASSES) if anchor in CLIP_ANCHORS else (ALL,):
                for group, idx in groups.items():
                    stack = np.stack(
                        [measures[i].profiles[(signal, anchor, clip_class)] for i in idx]
                    )
                    nulls = (
                        np.stack([measures[i].nulls[anchor] for i in idx])
                        if signal == "chat" and clip_class == ALL
                        else None
                    )
                    pooled = _pooled(stack, nulls, config)
                    if pooled.summary is not None:
                        pooled.summary["n_anchors"] = sum(
                            measures[i].counts[(anchor, clip_class)] for i in idx
                        )
                    profiles.setdefault(signal, {}).setdefault(anchor, {}).setdefault(
                        clip_class, {}
                    )[group] = pooled.summary
                    profile_frames.append(
                        pl.DataFrame(
                            {
                                "signal": signal,
                                "anchor": anchor,
                                "group": group,
                                "clip_class": clip_class,
                                "offset_s": offsets,
                                "mean": pooled.mean,
                                "low": pooled.low,
                                "high": pooled.high,
                            }
                        )
                    )
    delay = profiles["chat"]["loud_event"][ALL][ALL]
    lag_s = int(delay["peak_offset_s"]) if delay else 0

    xcorr_lags = [al.peak_lag(m.lags, m.xcorr) for m in measures]
    xcorr_summary = {
        group: _median_interval([xcorr_lags[i] for i in idx], config)
        for group, idx in groups.items()
    }

    # 3. Spike alignment, main setting and sensitivity grid.
    grid = sorted(
        {(config.window_s, config.z)}
        | {(w, z) for w in config.sensitivity_window_s for z in config.sensitivity_z}
    )
    per_setting = {
        (w, z): [_spike_metrics(v, window_s=w, z=z, lag_s=lag_s, config=config) for v in vods]
        for w, z in grid
    }
    main = per_setting[(config.window_s, config.z)]
    alignment: dict[str, Any] = {}
    sensitivity_rows: list[dict[str, Any]] = []
    for (w, z), rows in per_setting.items():
        for group, idx in groups.items():
            means = {name: _interval([rows[i][name] for i in idx], config) for name in rows[0]}
            if (w, z) == (config.window_s, config.z):
                alignment[group] = means
            sensitivity_rows.append(
                {
                    "group": group,
                    "window_s": w,
                    "z": z,
                    **{name: (m["mean"] if m else None) for name, m in means.items()},
                }
            )

    # 4. Padding inputs.
    padding = pl.DataFrame(
        [
            row
            for v, m in zip(vods, measures, strict=True)
            for row in _padding_rows(v, m, lag_s=lag_s, config=config)
        ],
        schema={
            "video_id": pl.String,
            "login": pl.String,
            "rank": pl.Int64,
            "clip_duration_s": pl.Float64,
            "loud_from_start_s": pl.Float64,
            "loud_from_end_s": pl.Float64,
            "chat_from_start_s": pl.Float64,
            "chat_from_end_s": pl.Float64,
            **{
                f"{name}_chance_{part}": pl.Float64
                for name in ("loud", "chat")
                for part in ("before", "inside", "after")
            },
        },
    )
    padding_summary = {
        group: _padding_summary(
            padding if group == ALL else padding.filter(pl.col("login") == group)
        )
        for group in groups
    }

    per_vod = pl.DataFrame(
        [
            {
                "video_id": v.video_id,
                "login": v.login,
                "duration_s": v.duration_s,
                "n_clips": v.n_clips,
                "n_clips_placed": v.clips.height,
                "n_clips_dropped": v.n_clips_dropped,
                "n_clips_cut": v.n_clips_cut,
                "n_moments": v.moments.height,
                "n_live_moments": m.counts[("moment_start", "live")],
                "n_post_stream_moments": m.counts[("moment_start", "post_stream")],
                "n_loud_events": m.counts[("loud_event", ALL)],
                "n_chat_after_end": v.n_chat_after_end,
                "xcorr_lag_s": lag,
                "xcorr_peak": float(m.xcorr.max()),
                **{f"spike_{k}": val for k, val in s.items()},
            }
            for v, m, s, lag in zip(vods, measures, main, xcorr_lags, strict=True)
        ],
        infer_schema_length=None,
    )
    xcorr = pl.concat(
        [
            pl.DataFrame(
                {"video_id": m.video_id, "login": m.login, "lag_s": m.lags, "corr": m.xcorr}
            )
            for m in measures
        ]
    )
    top_counts = {
        cls: sum(m.counts[("top_clip_start", cls)] for m in measures) for cls in CLIP_CLASSES
    }
    summary = {
        "experiment": config.experiment,
        "config": config.as_dict(),
        "inputs": {
            "n_vods": len(vods),
            "channels": {login: len(groups[login]) for login in logins},
            "vod_hours": sum(v.duration_s for v in vods) / 3600,
            "n_clips": sum(v.n_clips for v in vods),
            "n_clips_without_offset": sum(v.n_clips_without_offset for v in vods),
            "n_clips_dropped_past_end": sum(v.n_clips_dropped for v in vods),
            "n_clips_cut_at_end": sum(v.n_clips_cut for v in vods),
            "n_moments": sum(v.moments.height for v in vods),
            "n_live_moments": int(per_vod["n_live_moments"].sum()),
            "n_post_stream_moments": int(per_vod["n_post_stream_moments"].sum()),
            "n_top_clips_live": top_counts["live"],
            "n_top_clips_post_stream": top_counts["post_stream"],
            "n_loud_events": int(per_vod["n_loud_events"].sum()),
            "n_chat_messages_after_end": sum(v.n_chat_after_end for v in vods),
        },
        "chat_delay": {
            "shift_lag_s": lag_s,
            "loud_event": profiles["chat"]["loud_event"][ALL],
            "whole_vod_xcorr": xcorr_summary,
        },
        "profiles": profiles,
        "alignment": alignment,
        "padding": padding_summary,
        "clip_timing": clip_timing(vods),
    }
    return StudyResult(
        summary=summary,
        per_vod=per_vod,
        xcorr=xcorr,
        profiles=pl.concat(profile_frames),
        padding=padding,
        sensitivity=pl.DataFrame(sensitivity_rows, infer_schema_length=None),
    )


def rounded(value: Any, digits: int = 4) -> Any:  # noqa: ANN401 - any JSON value.
    """`value` with every float rounded to `digits` (non-finite floats become None)."""
    if isinstance(value, float):
        return round(value, digits) if math.isfinite(value) else None
    if isinstance(value, dict):
        return {k: rounded(v, digits) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [rounded(v, digits) for v in value]
    return value
