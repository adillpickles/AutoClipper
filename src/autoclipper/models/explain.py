"""Deterministic titles and explanations for ranked candidate moments (#65).

The scorer's ranking is final: this module never reorders, drops or rescores candidates. It
reads only aggregate numeric features per window (never raw chat messages or usernames;
docs/data/data-governance.md § 2), viewer-clip counts and optional speech-pause alignment to
attach three fields to each `Candidate`:

- ``title``: short 2-5 word human title for cards and the clip editor header.
- ``reason``: plain-English one-line summary of why the moment stood out.
- ``signal_details``: structured list of contributing signals (`SignalDetail`) for the UI's
  "why" breakdown.
"""

import logging
import math
import wave
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import numpy as np
import numpy.typing as npt
import polars as pl

from autoclipper.analysis import Candidate, SignalDetail
from autoclipper.models.pauses import PauseSnapParams, find_pauses_in_wav

log = logging.getLogger(__name__)

# Explicit allowlist of aggregate numeric feature columns this module may read. Raw text,
# usernames or hashes are never inspected even if a caller passes extra columns.
_CHAT_NUMERIC_COLUMNS: Final[tuple[str, ...]] = (
    "chat_msg_rate",
    "chat_msg_rate_z",
    "chat_msg_rate_rz",
    "chat_unique_rate_z",
    "chat_laugh_rate",
    "chat_hype_rate",
    "chat_shock_rate",
    "chat_keyword_rate",
    "chat_emote_frac",
    "chat_caps_frac",
    "chat_repeat_frac",
    "chat_event_count",
    "chat_msg_rate_lead1",
    "chat_msg_rate_lead2",
)
_AUDIO_NUMERIC_COLUMNS: Final[tuple[str, ...]] = (
    "audio_rms_max",
    "audio_rms_max_z",
    "audio_rms_rz",
    "audio_onset_rate",
    "audio_is_muted",
)
_AUDIO_EVENT_COLUMNS: Final[tuple[str, ...]] = (
    "audio_evt_laughter",
    "audio_evt_scream",
    "audio_evt_cheer",
    "audio_evt_music",
)
ALLOWED_FEATURE_COLUMNS: Final[tuple[str, ...]] = (
    *_CHAT_NUMERIC_COLUMNS,
    *_AUDIO_NUMERIC_COLUMNS,
    *_AUDIO_EVENT_COLUMNS,
)

# Minimum VOD-average message rate (msgs/s) before reporting a multiplicative rate jump ("6x").
_MIN_CHAT_MEAN_FOR_RATIO: Final = 0.05
_MIN_CHAT_RATIO: Final = 1.8
_MIN_REACTION_RATE: Final = 0.10
_MIN_REACTION_SHARE: Final = 0.15
_MIN_REACTION_LIFT: Final = 2.0
_MIN_LOOKBACK_MINUTES: Final = 5
_MIN_STREAM_SPAN_FOR_LOUDEST_S: Final = 60.0
_PAUSE_NARROW_HALF_WINDOW_S: Final = 1.0
_PAUSE_EDGE_TOLERANCE_S: Final = 0.05

# Priority order when breaking ties between reaction categories. Wordings describe the lexicon
# category rather than any single emote or phrase.
_REACTION_SPECS: Final[tuple[tuple[str, str, str, str, str], ...]] = (
    # (category, column, with_suffix, standalone, detail_text)
    (
        "laugh",
        "chat_laugh_rate",
        "with lots of laugh reactions",
        "lots of laugh reactions in chat",
        "High laugh-reaction activity",
    ),
    (
        "hype",
        "chat_hype_rate",
        "with lots of hype reactions",
        "chat got hyped",
        "High hype-reaction activity",
    ),
    (
        "shock",
        "chat_shock_rate",
        "with shocked reactions",
        "chat reacted with shock",
        "High shock-reaction activity",
    ),
    (
        "keyword",
        "chat_keyword_rate",
        "with reaction keywords",
        "chat surged with reaction keywords",
        "High reaction-keyword activity",
    ),
)

_AUDIO_EVENT_SPECS: Final[tuple[tuple[str, float, str, str, str], ...]] = (
    # (column, threshold, event_kind, reason_phrase, detail_prefix)
    ("audio_evt_scream", 0.5, "scream", "scream or shout detected", "Scream/shout detected"),
    ("audio_evt_laughter", 0.5, "laughter", "laughter detected in audio", "Laughter detected"),
    ("audio_evt_cheer", 0.5, "cheer", "cheering detected", "Cheering/applause detected"),
    ("audio_evt_music", 0.7, "music", "music cue detected", "Music cue detected"),
)

# Fallback signal mapping when `features` is absent and only `top_signals` is available.
_TOP_SIGNAL_FALLBACKS: Final[tuple[tuple[str, str, str, str], ...]] = (
    # (prefix, kind, label, phrase)
    ("chat_unique_rate", "chat_unique", "Unique chatters", "lots of viewers reacted at once"),
    ("chat_msg_rate_lead", "chat_rate", "Chat rate", "chat spiked right after"),
    ("chat_msg_rate_rz", "chat_rate", "Chat rate", "chat suddenly surged"),
    ("chat_msg_rate", "chat_rate", "Chat rate", "chat spiked"),
    ("chat_laugh_rate", "chat_laugh", "Chat reaction", "lots of laugh reactions in chat"),
    ("chat_hype_rate", "chat_hype", "Chat reaction", "chat got hyped"),
    ("chat_shock_rate", "chat_shock", "Chat reaction", "chat reacted with shock"),
    ("chat_keyword_rate", "chat_keyword", "Chat reaction", "chat surged with reaction keywords"),
    ("audio_evt_scream", "audio_event", "Audio event", "scream or shout detected"),
    ("audio_evt_laughter", "audio_event", "Audio event", "laughter detected in audio"),
    ("audio_evt_cheer", "audio_event", "Audio event", "cheering detected"),
    ("audio_evt_music", "audio_event", "Audio event", "music cue detected"),
    ("audio_rms_rz", "audio_loudness", "Audio loudness", "sudden audio loudness spike"),
    ("audio_rms", "audio_loudness", "Audio loudness", "loud audio spike"),
    ("audio_onset_rate", "audio_onset", "Audio activity", "burst of rapid audio activity"),
)


type _FloatArray = npt.NDArray[np.float64]


@dataclass(frozen=True, slots=True)
class _FeatureIndex:
    """Precomputed window arrays and VOD baselines for fast per-candidate explanations."""

    starts: _FloatArray
    ends: _FloatArray
    columns: dict[str, _FloatArray]
    vod_span_s: float
    vod_chat_mean: float
    vod_reaction_means: dict[str, float]
    vod_onset_mean: float
    unmuted: npt.NDArray[np.bool_]


@dataclass(slots=True)
class _SignalSummary:
    """Extracted facts for one candidate, turned into `title`, `reason` and `signal_details`."""

    chat_clause: str | None = None
    chat_strong: bool = False
    chat_mood: str | None = None
    audio_clause: str | None = None
    audio_loudest_stream: bool = False
    audio_event_clause: str | None = None
    audio_event_kind: str | None = None
    clips_clause: str | None = None
    pause_clause: str | None = None
    details: list[SignalDetail] | None = None

    def __post_init__(self) -> None:
        if self.details is None:
            self.details = []


def explain_candidates(
    candidates: Sequence[Candidate],
    *,
    windows: pl.DataFrame | None = None,
    features: pl.DataFrame | None = None,
    duration_s: float | None = None,
    audio_path: Path | None = None,
    pause_snapped_edges: Mapping[str, tuple[bool, bool]] | None = None,
    pause_snapped_ids: Collection[str] | None = None,
    pause_snap: PauseSnapParams | None = None,
) -> list[Candidate]:
    """Attach deterministic ``title``, ``reason`` and ``signal_details`` to ranked candidates.

    Candidate order, count, ``rank``, ``score``, bounds, ``source``, ``n_viewer_clips`` and
    ``top_signals`` are strictly preserved.

    Args:
        candidates: Already-ranked candidates from candidate generation.
        windows: Optional window time bounds (``window_id``, ``start_s``, ``end_s``).
        features: Optional per-window aggregate features (chat, audio and/or audio events),
            keyed by ``window_id`` (or already holding ``start_s`` and ``end_s``). Only numeric
            columns in `ALLOWED_FEATURE_COLUMNS` are read.
        duration_s: Optional VOD duration in seconds (for clamping WAV pause checks).
        audio_path: Optional normalized 16 kHz WAV used to detect whether clip boundaries sit on
            speech pauses when explicit per-edge snap flags are not passed.
        pause_snapped_edges: Optional mapping ``candidate_id -> (start_snapped, end_snapped)``
            from candidate generation. Takes precedence over WAV re-detection.
        pause_snapped_ids: Optional explicit set of ``candidate_id``s known to have both edges
            snapped to speech pauses.
        pause_snap: Optional `PauseSnapParams` for WAV pause checks.

    Returns:
        New list of `Candidate` objects in the exact input order with ``title``, ``reason`` and
        ``signal_details`` populated.
    """
    if not candidates:
        return []

    index = _build_feature_index(windows, features)
    wav = audio_path if audio_path is not None and audio_path.is_file() else None
    snap_params = pause_snap or PauseSnapParams()
    snapped_set = set(pause_snapped_ids) if pause_snapped_ids is not None else None
    wav_failed = False

    explained: list[Candidate] = []
    for candidate in candidates:
        pause_state: str | None = None
        if pause_snapped_edges is not None and candidate.candidate_id in pause_snapped_edges:
            start_snapped, end_snapped = pause_snapped_edges[candidate.candidate_id]
            pause_state = _pause_state_from_flags(start_snapped, end_snapped)
        elif snapped_set is not None and candidate.candidate_id in snapped_set:
            pause_state = "both"
        elif wav is not None and not wav_failed:
            eff_duration = duration_s
            if eff_duration is None:
                eff_duration = (
                    float(index.ends[-1])
                    if index is not None and index.ends.size > 0
                    else candidate.end_s + _PAUSE_NARROW_HALF_WINDOW_S
                )
            try:
                pause_state = _detect_pause_alignment(
                    wav,
                    candidate.start_s,
                    candidate.end_s,
                    duration_s=eff_duration,
                    params=snap_params,
                    index=index,
                )
            except (OSError, ValueError, wave.Error) as exc:
                log.warning("skipping pause alignment check path=%s error=%s", wav, exc)
                wav_failed = True

        title, reason, details = _explain_one(candidate, index, pause_state)
        explained.append(
            candidate.model_copy(
                update={
                    "title": title,
                    "reason": reason,
                    "signal_details": tuple(details),
                }
            )
        )
    return explained


def candidate_stats(
    candidates: Sequence[Candidate],
    *,
    windows: pl.DataFrame | None = None,
    features: pl.DataFrame | None = None,
) -> list[dict[str, float]]:
    """Peak aggregate feature values inside each candidate, for the optional title polisher.

    Reads the same windows and features as `explain_candidates` and only the numeric columns in
    `ALLOWED_FEATURE_COLUMNS` (never text, usernames or hashes, whatever else `features` holds).
    Each value is the largest finite value among the windows overlapping the candidate, rounded
    to 2 decimals; audio columns skip muted windows, as the explanations do. ``audio_is_muted``
    itself is left out. A column without a finite value is omitted.

    Returns:
        One dict per candidate, in input order (empty dicts when there are no features).
    """
    index = _build_feature_index(windows, features)
    stats: list[dict[str, float]] = []
    for candidate in candidates:
        values: dict[str, float] = {}
        if index is not None:
            inside = (index.starts < candidate.end_s) & (index.ends > candidate.start_s)
            for column, array in index.columns.items():
                if column == "audio_is_muted":
                    continue
                mask = inside & index.unmuted if column.startswith("audio_") else inside
                peak = _max_finite(array[mask])
                if peak is not None:
                    values[column] = round(peak, 2)
        stats.append(values)
    return stats


def _build_feature_index(
    windows: pl.DataFrame | None, features: pl.DataFrame | None
) -> _FeatureIndex | None:
    if features is None or features.height == 0:
        return None

    present = [
        col
        for col in ALLOWED_FEATURE_COLUMNS
        if col in features.columns and features[col].dtype.is_numeric()
    ]
    if not present:
        return None

    if windows is not None and {"window_id", "start_s", "end_s"} <= set(windows.columns):
        if "window_id" not in features.columns:
            return None
        frame = (
            windows.select("window_id", "start_s", "end_s")
            .join(features.select("window_id", *present), on="window_id", how="left")
            .sort("start_s")
        )
    elif {"start_s", "end_s"} <= set(features.columns):
        frame = features.select("start_s", "end_s", *present).sort("start_s")
    else:
        return None

    if frame.height == 0:
        return None

    starts = frame["start_s"].cast(pl.Float64).to_numpy()
    ends = frame["end_s"].cast(pl.Float64).to_numpy()
    cols: dict[str, _FloatArray] = {}
    for col in present:
        cols[col] = frame[col].cast(pl.Float64).fill_null(float("nan")).to_numpy()

    if "audio_is_muted" in cols:
        muted = cols["audio_is_muted"]
        unmuted = ~(np.isfinite(muted) & (muted >= 0.5))
    else:
        unmuted = np.ones(len(starts), dtype=np.bool_)

    vod_chat_mean = _finite_mean(cols["chat_msg_rate"]) if "chat_msg_rate" in cols else 0.0
    vod_reaction_means = {
        cat: _finite_mean(cols[col]) for cat, col, _, _, _ in _REACTION_SPECS if col in cols
    }
    vod_onset_mean = (
        _finite_mean(cols["audio_onset_rate"][unmuted])
        if "audio_onset_rate" in cols and unmuted.any()
        else 0.0
    )
    vod_span_s = float(ends[-1] - starts[0]) if len(starts) > 0 else 0.0

    return _FeatureIndex(
        starts=starts,
        ends=ends,
        columns=cols,
        vod_span_s=vod_span_s,
        vod_chat_mean=vod_chat_mean,
        vod_reaction_means=vod_reaction_means,
        vod_onset_mean=vod_onset_mean,
        unmuted=unmuted,
    )


def _explain_one(
    candidate: Candidate,
    index: _FeatureIndex | None,
    pause_state: str | None,
) -> tuple[str, str, list[SignalDetail]]:
    summary = _SignalSummary()
    assert summary.details is not None

    inside = (
        (index.starts < candidate.end_s) & (index.ends > candidate.start_s)
        if index is not None
        else None
    )
    has_feature_rows = index is not None and inside is not None and bool(inside.any())

    if has_feature_rows:
        assert index is not None
        assert inside is not None
        _extract_chat_signals(candidate, index, inside, summary)
        _extract_audio_signals(candidate, index, inside, summary)
        _extract_audio_event_signals(index, inside, summary)
    else:
        _extract_top_signal_fallbacks(candidate, summary)

    # Viewer clips (counts distinct clip IDs merged into this candidate, never creators).
    if candidate.n_viewer_clips == 1:
        summary.clips_clause = "1 viewer clip"
        summary.details.append(
            SignalDetail(
                kind="viewer_clips",
                label="Viewer clips",
                detail="1 viewer clip overlaps this moment",
            )
        )
    elif candidate.n_viewer_clips > 1:
        summary.clips_clause = f"{candidate.n_viewer_clips} viewer clips"
        summary.details.append(
            SignalDetail(
                kind="viewer_clips",
                label="Viewer clips",
                detail=f"{candidate.n_viewer_clips} viewer clips overlap this moment",
            )
        )

    # Pause-snapped speech boundaries.
    if pause_state == "both":
        summary.pause_clause = "trimmed cleanly on speech pauses"
        summary.details.append(
            SignalDetail(
                kind="pause_snap",
                label="Speech boundaries",
                detail="Start and end aligned to speech pauses",
            )
        )
    elif pause_state == "start":
        summary.pause_clause = "aligned to a speech pause at the start"
        summary.details.append(
            SignalDetail(
                kind="pause_snap",
                label="Speech boundaries",
                detail="Start aligned to a speech pause",
            )
        )
    elif pause_state == "end":
        summary.pause_clause = "aligned to a speech pause at the end"
        summary.details.append(
            SignalDetail(
                kind="pause_snap",
                label="Speech boundaries",
                detail="End aligned to a speech pause",
            )
        )

    reason = _compose_reason(summary)
    title = _compose_title(candidate, summary)
    return title, reason, summary.details


def _extract_chat_signals(
    candidate: Candidate,
    index: _FeatureIndex,
    inside: npt.NDArray[np.bool_],
    summary: _SignalSummary,
) -> None:
    assert summary.details is not None
    cols = index.columns

    inside_indices = np.flatnonzero(inside)
    peak_rate: float | None = None
    peak_rate_idx: int | None = None
    if "chat_msg_rate" in cols:
        rate_vals = cols["chat_msg_rate"][inside_indices]
        finite_mask = np.isfinite(rate_vals)
        if finite_mask.any():
            local_best = int(np.argmax(np.where(finite_mask, rate_vals, -np.inf)))
            peak_rate = float(rate_vals[local_best])
            peak_rate_idx = int(inside_indices[local_best])

    peak_z = _max_finite(cols["chat_msg_rate_z"][inside]) if "chat_msg_rate_z" in cols else None
    peak_rz = _max_finite(cols["chat_msg_rate_rz"][inside]) if "chat_msg_rate_rz" in cols else None
    peak_unique_z = (
        _max_finite(cols["chat_unique_rate_z"][inside]) if "chat_unique_rate_z" in cols else None
    )

    lead_vals = [
        val
        for col in ("chat_msg_rate_lead1", "chat_msg_rate_lead2")
        if col in cols and (val := _max_finite(cols[col][inside])) is not None
    ]
    peak_lead_z = max(lead_vals) if lead_vals else None

    chat_ratio: float | None = None
    if peak_rate is not None and peak_rate > 0 and index.vod_chat_mean >= _MIN_CHAT_MEAN_FOR_RATIO:
        chat_ratio = peak_rate / index.vod_chat_mean

    # Pick the strongest notable chat reaction category (aggregate lexicon counts only).
    best_reaction: tuple[str, str, str, str, float] | None = None
    for cat, col, suffix, standalone, detail_text in _REACTION_SPECS:
        if col not in cols:
            continue
        peak_cat = _max_finite(cols[col][inside])
        if peak_cat is None or peak_cat < _MIN_REACTION_RATE:
            continue
        share = (peak_cat / peak_rate) if (peak_rate is not None and peak_rate > 0) else 0.0
        vod_cat_mean = index.vod_reaction_means.get(cat, 0.0)
        lift = (peak_cat / vod_cat_mean) if vod_cat_mean >= 0.02 else float("inf")
        if (share >= _MIN_REACTION_SHARE or lift >= _MIN_REACTION_LIFT) and (
            best_reaction is None or peak_cat > best_reaction[4]
        ):
            best_reaction = (cat, suffix, standalone, detail_text, peak_cat)

    top_set = set(candidate.top_signals)
    has_rate_columns = bool({"chat_msg_rate", "chat_msg_rate_z", "chat_msg_rate_rz"} & set(cols))
    has_msg_rate_spike = (
        (
            chat_ratio is not None
            and chat_ratio >= _MIN_CHAT_RATIO
            and (peak_z is None or peak_z >= 1.0)
        )
        or (peak_z is not None and peak_z >= 1.5)
        or (peak_rz is not None and peak_rz >= 2.0)
        or (not has_rate_columns and bool(top_set & {"chat_msg_rate_z", "chat_msg_rate_rz"}))
    )
    has_delayed_spike = not has_msg_rate_spike and (
        (peak_lead_z is not None and peak_lead_z >= 1.5)
        or (
            "chat_msg_rate_lead1" not in cols
            and "chat_msg_rate_lead2" not in cols
            and any(sig.startswith("chat_msg_rate_lead") for sig in top_set)
        )
    )
    has_unique_spike = (peak_unique_z is not None and peak_unique_z >= 1.5) or (
        "chat_unique_rate_z" in top_set and (peak_unique_z is None or peak_unique_z >= 1.0)
    )

    if best_reaction is not None:
        summary.chat_mood = best_reaction[0]

    if has_msg_rate_spike:
        summary.chat_strong = (chat_ratio is not None and chat_ratio >= 3.5) or (
            peak_z is not None and peak_z >= 3.0
        )
        if chat_ratio is not None and chat_ratio >= _MIN_CHAT_RATIO:
            mult = _format_multiplier(chat_ratio)
            base_clause = f"chat jumped to {mult} its usual rate"
            z_suffix = f" (z={peak_z:.1f})" if peak_z is not None and peak_z >= 0.5 else ""
            summary.details.append(
                SignalDetail(
                    kind="chat_rate",
                    label="Chat rate",
                    detail=f"{mult} usual stream rate{z_suffix}",
                )
            )
        elif peak_z is not None and peak_z >= 1.5:
            base_clause = "chat spiked sharply"
            summary.details.append(
                SignalDetail(
                    kind="chat_rate",
                    label="Chat rate",
                    detail=f"Sharp chat spike (z={peak_z:.1f})",
                )
            )
        elif peak_rz is not None and peak_rz >= 2.0:
            base_clause = "chat suddenly surged"
            summary.details.append(
                SignalDetail(
                    kind="chat_rate",
                    label="Chat rate",
                    detail=f"Sudden surge vs. last 5 min (rz={peak_rz:.1f})",
                )
            )
        else:
            base_clause = "chat spiked"
            summary.details.append(
                SignalDetail(
                    kind="chat_rate",
                    label="Chat rate",
                    detail="Above-average chat rate",
                )
            )

        if best_reaction is not None:
            summary.chat_clause = f"{base_clause} {best_reaction[1]}"
            summary.details.append(
                SignalDetail(
                    kind=f"chat_{best_reaction[0]}",
                    label="Chat reaction",
                    detail=best_reaction[3],
                )
            )
        else:
            summary.chat_clause = base_clause
    elif has_delayed_spike:
        base_clause = "chat spiked right after"
        z_suffix = (
            f" (lead z={peak_lead_z:.1f})" if peak_lead_z is not None and peak_lead_z >= 0.5 else ""
        )
        summary.details.append(
            SignalDetail(
                kind="chat_rate",
                label="Chat rate",
                detail=f"Chat surge followed right after{z_suffix}",
            )
        )
        if best_reaction is not None:
            summary.chat_clause = f"{base_clause} {best_reaction[1]}"
            summary.details.append(
                SignalDetail(
                    kind=f"chat_{best_reaction[0]}",
                    label="Chat reaction",
                    detail=best_reaction[3],
                )
            )
        else:
            summary.chat_clause = base_clause
    elif has_unique_spike:
        base_clause = "lots of viewers reacted at once"
        unique_detail = (
            f"Broad chatter spike (z={peak_unique_z:.1f})"
            if peak_unique_z is not None and peak_unique_z >= 0.5
            else "More unique chatters reacted"
        )
        summary.details.append(
            SignalDetail(
                kind="chat_unique",
                label="Unique chatters",
                detail=unique_detail,
            )
        )
        if best_reaction is not None:
            summary.chat_clause = f"{base_clause} {best_reaction[1]}"
            summary.details.append(
                SignalDetail(
                    kind=f"chat_{best_reaction[0]}",
                    label="Chat reaction",
                    detail=best_reaction[3],
                )
            )
        else:
            summary.chat_clause = base_clause
    elif best_reaction is not None:
        summary.chat_clause = best_reaction[2]
        summary.details.append(
            SignalDetail(
                kind=f"chat_{best_reaction[0]}",
                label="Chat reaction",
                detail=best_reaction[3],
            )
        )

    # Secondary aggregate chat signals for the "why" breakdown.
    if (
        peak_unique_z is not None
        and peak_unique_z >= 2.0
        and all(d.kind != "chat_unique" for d in summary.details)
    ):
        summary.details.append(
            SignalDetail(
                kind="chat_unique",
                label="Unique chatters",
                detail=f"Broad audience reaction (z={peak_unique_z:.1f})",
            )
        )

    if "chat_event_count" in cols:
        ev = cols["chat_event_count"][inside]
        ev_total = float(np.nansum(ev)) if np.isfinite(ev).any() else 0.0
        if ev_total >= 2.0:
            n_events = round(ev_total)
            summary.details.append(
                SignalDetail(
                    kind="chat_events",
                    label="Stream events",
                    detail=f"{n_events} stream events in chat",
                )
            )
            if summary.chat_clause is None:
                summary.chat_clause = f"{n_events} stream events rolled in"

    # Measure emote and uppercase fractions on the actual peak chat-rate window so an almost-empty
    # neighboring window can't mischaracterize the peak window.
    if peak_rate is not None and peak_rate >= 0.3 and peak_rate_idx is not None:
        if "chat_emote_frac" in cols:
            emote_frac = float(cols["chat_emote_frac"][peak_rate_idx])
            if math.isfinite(emote_frac) and emote_frac >= 0.5:
                summary.details.append(
                    SignalDetail(
                        kind="chat_emotes",
                        label="Emote density",
                        detail=f"{round(emote_frac * 100)}% emotes in peak window",
                    )
                )
        if "chat_caps_frac" in cols:
            caps_frac = float(cols["chat_caps_frac"][peak_rate_idx])
            if math.isfinite(caps_frac) and caps_frac >= 0.5:
                summary.details.append(
                    SignalDetail(
                        kind="chat_caps",
                        label="All-caps chat",
                        detail=f"{round(caps_frac * 100)}% uppercase letters in peak window",
                    )
                )


def _extract_audio_signals(
    candidate: Candidate,
    index: _FeatureIndex,
    inside: npt.NDArray[np.bool_],
    summary: _SignalSummary,
) -> None:
    assert summary.details is not None
    cols = index.columns
    eligible = inside & index.unmuted
    if not eligible.any():
        return

    peak_z = _max_finite(cols["audio_rms_max_z"][eligible]) if "audio_rms_max_z" in cols else None
    peak_rz = _max_finite(cols["audio_rms_rz"][eligible]) if "audio_rms_rz" in cols else None
    has_loud_z_cols = "audio_rms_max_z" in cols or "audio_rms_rz" in cols
    top_set = set(candidate.top_signals)

    # When loudness z-score columns are present, judge strictly from unmuted windows so a muted
    # window that appeared in `top_signals` never triggers a loudness claim.
    loud_notable = (
        (peak_z is not None and peak_z >= 1.0)
        or (peak_rz is not None and peak_rz >= 1.5)
        or (not has_loud_z_cols and bool(top_set & {"audio_rms_max_z", "audio_rms_rz"}))
    )

    if loud_notable and "audio_rms_max" in cols:
        rms = cols["audio_rms_max"]
        eligible_indices = np.flatnonzero(eligible & np.isfinite(rms))
        if eligible_indices.size > 0:
            peak_idx = int(eligible_indices[int(np.argmax(rms[eligible_indices]))])
            peak_rms = float(rms[peak_idx])
            all_unmuted = index.unmuted & np.isfinite(rms)
            louder_anywhere = np.flatnonzero(all_unmuted & (rms > peak_rms + 1e-9))

            z_note = f" (z={peak_z:.1f})" if peak_z is not None and peak_z >= 0.5 else ""
            if (
                louder_anywhere.size == 0
                and index.vod_span_s >= _MIN_STREAM_SPAN_FOR_LOUDEST_S
                and (peak_z is None or peak_z >= 1.0)
            ):
                summary.audio_clause = "loudest moment of the stream"
                summary.audio_loudest_stream = True
                summary.details.append(
                    SignalDetail(
                        kind="audio_loudness",
                        label="Audio loudness",
                        detail=f"Loudest moment of the stream{z_note}",
                    )
                )
            else:
                earlier_louder = louder_anywhere[louder_anywhere < peak_idx]
                if earlier_louder.size > 0:
                    prev_idx = int(earlier_louder[-1])
                    lookback_s = max(0.0, float(index.starts[peak_idx] - index.ends[prev_idx]))
                else:
                    lookback_s = max(0.0, float(index.starts[peak_idx] - index.starts[0]))
                lookback_min = int(lookback_s // 60)

                if lookback_min >= _MIN_LOOKBACK_MINUTES:
                    if lookback_min >= 120:
                        hours = lookback_min // 60
                        summary.audio_clause = f"loudest moment in over {hours} hours"
                        detail_lead = f"Loudest in over {hours} hours"
                    else:
                        summary.audio_clause = f"loudest moment in the last {lookback_min} minutes"
                        detail_lead = f"Loudest in the last {lookback_min} min"
                    summary.details.append(
                        SignalDetail(
                            kind="audio_loudness",
                            label="Audio loudness",
                            detail=f"{detail_lead}{z_note}",
                        )
                    )
                elif peak_rz is not None and peak_rz >= 2.0:
                    summary.audio_clause = "sudden audio loudness spike"
                    summary.details.append(
                        SignalDetail(
                            kind="audio_loudness",
                            label="Audio loudness",
                            detail=f"Sudden spike vs. last 5 min (rz={peak_rz:.1f})",
                        )
                    )
                else:
                    summary.audio_clause = "loud audio spike"
                    summary.details.append(
                        SignalDetail(
                            kind="audio_loudness",
                            label="Audio loudness",
                            detail=f"High peak loudness{z_note}"
                            if z_note
                            else "High peak loudness",
                        )
                    )
            return
    elif loud_notable:
        if peak_rz is not None and peak_rz >= 2.0:
            summary.audio_clause = "sudden audio loudness spike"
            summary.details.append(
                SignalDetail(
                    kind="audio_loudness",
                    label="Audio loudness",
                    detail=f"Sudden spike vs. last 5 min (rz={peak_rz:.1f})",
                )
            )
        else:
            z_note = f" (z={peak_z:.1f})" if peak_z is not None and peak_z >= 0.5 else ""
            summary.audio_clause = "loud audio spike"
            summary.details.append(
                SignalDetail(
                    kind="audio_loudness",
                    label="Audio loudness",
                    detail=f"High peak loudness{z_note}" if z_note else "High peak loudness",
                )
            )
        return

    if "audio_onset_rate" in cols:
        peak_onset = _max_finite(cols["audio_onset_rate"][eligible])
        if (
            peak_onset is not None
            and peak_onset >= 1.5
            and index.vod_onset_mean > 0
            and peak_onset >= 2.0 * index.vod_onset_mean
        ):
            summary.audio_clause = "burst of rapid audio activity"
            summary.details.append(
                SignalDetail(
                    kind="audio_onset",
                    label="Audio activity",
                    detail=f"Fast audio burst ({peak_onset:.1f} onsets/s)",
                )
            )


def _extract_audio_event_signals(
    index: _FeatureIndex,
    inside: npt.NDArray[np.bool_],
    summary: _SignalSummary,
) -> None:
    assert summary.details is not None
    cols = index.columns
    eligible = inside & index.unmuted
    if not eligible.any():
        return

    hits: list[tuple[float, str, str, str]] = []
    for col, threshold, event_kind, phrase, detail_prefix in _AUDIO_EVENT_SPECS:
        if col not in cols:
            continue
        prob = _max_finite(cols[col][eligible])
        if prob is not None and prob >= threshold:
            hits.append((prob, event_kind, phrase, f"{detail_prefix} ({round(prob * 100)}%)"))

    if not hits:
        return

    hits.sort(key=lambda item: (-item[0], item[1]))
    _, best_kind, best_phrase, _ = hits[0]
    summary.audio_event_kind = best_kind
    summary.audio_event_clause = best_phrase
    for _, _, _, detail_text in hits:
        summary.details.append(
            SignalDetail(kind="audio_event", label="Audio event", detail=detail_text)
        )


def _extract_top_signal_fallbacks(candidate: Candidate, summary: _SignalSummary) -> None:
    assert summary.details is not None
    seen_kinds: set[str] = set()
    for signal in candidate.top_signals:
        for prefix, kind, label, phrase in _TOP_SIGNAL_FALLBACKS:
            if not signal.startswith(prefix):
                continue
            if kind in seen_kinds:
                break
            seen_kinds.add(kind)
            summary.details.append(SignalDetail(kind=kind, label=label, detail=_capitalize(phrase)))
            if kind.startswith("chat_") and summary.chat_clause is None:
                summary.chat_clause = phrase
                if kind in ("chat_laugh", "chat_hype", "chat_shock", "chat_keyword"):
                    summary.chat_mood = kind.removeprefix("chat_")
            elif kind == "audio_event" and summary.audio_event_clause is None:
                summary.audio_event_clause = phrase
                if "scream" in prefix:
                    summary.audio_event_kind = "scream"
                elif "laughter" in prefix:
                    summary.audio_event_kind = "laughter"
                elif "cheer" in prefix:
                    summary.audio_event_kind = "cheer"
                elif "music" in prefix:
                    summary.audio_event_kind = "music"
            elif kind.startswith("audio_") and summary.audio_clause is None:
                summary.audio_clause = phrase
            break


def _pause_state_from_flags(start_snapped: bool, end_snapped: bool) -> str | None:
    if start_snapped and end_snapped:
        return "both"
    if start_snapped:
        return "start"
    if end_snapped:
        return "end"
    return None


def _edge_is_muted(edge_s: float, index: _FeatureIndex | None) -> bool:
    if index is None:
        return False
    hits = np.flatnonzero((index.starts <= edge_s) & (edge_s < index.ends))
    return bool(hits.size > 0 and not index.unmuted[int(hits[0])])


def _detect_pause_alignment(
    wav_path: Path,
    start_s: float,
    end_s: float,
    *,
    duration_s: float,
    params: PauseSnapParams,
    index: _FeatureIndex | None,
) -> str | None:
    start_aligned = not _edge_is_muted(start_s, index) and _edge_on_pause(
        wav_path, start_s, duration_s, params
    )
    end_aligned = not _edge_is_muted(end_s, index) and _edge_on_pause(
        wav_path, end_s, duration_s, params
    )
    return _pause_state_from_flags(start_aligned, end_aligned)


def _edge_on_pause(
    wav_path: Path,
    edge_s: float,
    duration_s: float,
    params: PauseSnapParams,
) -> bool:
    # Check both the snap search radius (`max_shift_s + min_pause_s`, matching
    # `snap_interval_to_wav`) and a narrower ±1 s window so both long pauses and short speech
    # bursts adjacent to the boundary are recognized, while completely silent stretches are never
    # reported as speech pauses.
    wide_pad = params.max_shift_s + params.min_pause_s
    radii = (
        (wide_pad,)
        if wide_pad <= _PAUSE_NARROW_HALF_WINDOW_S
        else (
            wide_pad,
            _PAUSE_NARROW_HALF_WINDOW_S,
        )
    )
    for pad in radii:
        lo = max(0.0, edge_s - pad)
        hi = min(duration_s, edge_s + pad)
        if hi - lo < params.min_pause_s:
            continue
        pauses = find_pauses_in_wav(wav_path, lo, hi, params=params)
        if any(
            (p_start - _PAUSE_EDGE_TOLERANCE_S <= edge_s <= p_end + _PAUSE_EDGE_TOLERANCE_S)
            and not (p_start <= lo + 0.02 and p_end >= hi - 0.02)
            for p_start, p_end in pauses
        ):
            return True
    return False


def _compose_reason(summary: _SignalSummary) -> str:
    clauses: list[str] = []
    if summary.chat_clause is not None:
        clauses.append(summary.chat_clause)
    if summary.audio_clause is not None:
        clauses.append(summary.audio_clause)
    if summary.audio_event_clause is not None and (
        len(clauses) < 2 or (summary.clips_clause is None and len(clauses) < 3)
    ):
        clauses.append(summary.audio_event_clause)
    if summary.clips_clause is not None:
        clauses.append(summary.clips_clause)
    if summary.pause_clause is not None and len(clauses) < 3:
        clauses.append(summary.pause_clause)

    if not clauses:
        return "Steady activity throughout"
    return _capitalize("; ".join(clauses))


def _compose_title(candidate: Candidate, summary: _SignalSummary) -> str:
    has_chat = summary.chat_clause is not None
    has_loud = summary.audio_clause is not None
    mood = summary.chat_mood
    evt = summary.audio_event_kind

    # Vocal titles ("Laughter", "Shout", "Scream") require an audio-event signal; plain RMS
    # loudness is described separately from chat reactions.
    if mood == "laugh":
        if evt == "laughter":
            return "Laughter & chat burst"
        if has_loud:
            return "Loud audio & laughing chat"
        return "Chat erupts laughing"
    if mood == "hype":
        if evt == "scream":
            return "Hype shout & chat surge"
        if has_loud:
            return "Loud audio & hype chat"
        return "Hype chat surge"
    if mood == "shock":
        if evt == "scream":
            return "Shocked shout & chat spike"
        if has_loud:
            return "Loud audio & shocked chat"
        return "Shocked chat reaction"
    if mood == "keyword":
        return "Reaction-heavy chat spike"

    if evt == "scream":
        return "Loud scream & chat spike" if has_chat else "Loud scream moment"
    if evt == "laughter":
        return "Big laugh & chat spike" if has_chat else "Big laugh moment"
    if evt == "cheer":
        return "Cheering & chat surge" if has_chat else "Cheering moment"

    if has_chat and has_loud:
        return "Loud audio & chat spike"
    if has_chat:
        return "Big chat spike" if summary.chat_strong else "Chat surge"
    if summary.audio_loudest_stream:
        return "Loudest moment of stream"
    if has_loud:
        return (
            "Loud moment with viewer clips" if candidate.n_viewer_clips > 0 else "Loud audio spike"
        )
    if evt == "music":
        return "Music cue moment"
    if candidate.n_viewer_clips >= 3:
        return "Popular viewer clip"
    if candidate.n_viewer_clips >= 1:
        return "Viewer-clipped moment"
    return "Stream moment"


def _format_multiplier(ratio: float) -> str:
    if ratio >= 2.5:
        return f"{round(ratio)}\u00d7"
    return f"{ratio:.1f}\u00d7"


def _capitalize(text: str) -> str:
    return text[0].upper() + text[1:] if text else text


def _finite_mean(values: _FloatArray) -> float:
    finite = values[np.isfinite(values)]
    if finite.size == 0:
        return 0.0
    return float(np.mean(finite))


def _max_finite(values: _FloatArray) -> float | None:
    finite = values[np.isfinite(values)]
    if finite.size == 0:
        return None
    val = float(np.max(finite))
    return val if math.isfinite(val) else None


__all__ = ["ALLOWED_FEATURE_COLUMNS", "candidate_stats", "explain_candidates"]
