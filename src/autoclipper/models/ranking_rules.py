"""Down-weight windows that only look busy, for the default ranking (#192).

Chat is loud at moments nobody would clip: the start of a stream ("hi", "LATE"), its end ("bye"),
stretches where the VOD's audio is silent (music Twitch muted out of the VOD shows up only as
silence; Helix reports no muted segments for it), greeting/farewell waves, and chat-guessing games
where everyone types a different answer. The plain ``chat_loudness`` sum ranks those as hype.

`apply_ranking_rules` lowers such windows' scores before candidate generation; it never drops a
window. Each rule that matches a window multiplies the window's **height above the VOD's median
score** by the rule's factor (``factor`` 0.5 keeps half of it), so the result stays on the scorer's
scale, a window at or below the median is unchanged, and two matching rules give 0.25. The
thresholds and factors come from ``configs/ranking.yaml`` (`load_ranking_rules`); the built-in
`RankingRules()` defaults mirror that file.

| Flag | A window matches when |
|---|---|
| ``stream_start`` | it starts in the first ``intro_s`` seconds |
| ``stream_end`` | it ends in the last ``outro_s`` seconds |
| ``silent_audio`` | ``audio_silence_frac >= silence_frac_min`` and the window isn't Twitch-muted |
| ``greetings`` | it is in the first ``intro_s`` / last ``outro_s`` seconds and whole-message |
|  | greetings + farewells are at least ``participation_frac_min`` of its messages |
| ``answer_spam`` | ``chat_answer_frac >= answer_frac_min`` in a window that is busy for its VOD |
|  | (at least ``answer_min_msgs`` messages, ``answer_min_msg_rate`` msg/s and the VOD's |
|  | ``answer_rate_quantile`` chat rate). **Off by default** (``answer_factor`` 1.0) |

The edge rules are skipped for a VOD no longer than ``intro_s + outro_s`` (every window would be
an edge). ``greetings`` only looks at the stream's start and end, so a mid-stream "BYE BYE" kill
wave or an outraged "HEY" stays a reaction. ``answer_spam`` stays off until it is tuned on
dataset v1 (#40): a small channel's reactive chat ("bro what", "no shot") is answer-shaped too.
A rule whose factor is 1 or whose feature columns are missing (no audio, no chat) changes
nothing and is never reported.
Windows Twitch reports as muted keep the baselines' rule (their audio counts as missing,
`autoclipper.models.baselines`) and aren't flagged as silent.

The rules apply to the app's default ranking only (``chat_loudness`` and what it degrades to,
`autoclipper.analyze`). The evaluation's baselines stay the plain sums of evaluation.md § 2, and a
trained model learns from the chat-v2 participation features itself.
"""

import logging
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Final

import numpy as np
import polars as pl
import yaml

from autoclipper.analysis import Candidate

log = logging.getLogger(__name__)

FLAGS: Final = ("stream_start", "stream_end", "silent_audio", "greetings", "answer_spam")
"""Every flag `apply_ranking_rules` can set, in the order explanations list them."""

DEFAULT_RULES_PATH: Final = Path("configs/ranking.yaml")

_SCORE_TOLERANCE: Final = 1e-9


class RankingRulesError(ValueError):
    """The ranking rules file is missing or malformed."""


@dataclass(frozen=True, slots=True)
class RankingRules:
    """Thresholds and strengths of the ranking rules (``configs/ranking.yaml``).

    Attributes:
        enabled: False turns every rule off (the plain scorer ranking).
        intro_s: Windows starting this many seconds into the VOD or earlier are its start.
        outro_s: Windows ending within this many seconds of the VOD's end are its end.
        edge_factor: Share of the height above the median an edge window keeps.
        silence_frac_min: ``audio_silence_frac`` (share of frames below -60 dBFS) at which a
            window's audio counts as silent.
        silence_factor: Share a silent window keeps.
        participation_frac_min: Share of a window's messages that are greetings or farewells at
            which it is a greeting/farewell wave (stream start and end windows only).
        participation_factor: Share such a window keeps.
        answer_frac_min: ``chat_answer_frac`` at which a window is answer spam.
        answer_min_msg_rate: Messages per second a window needs before it can be answer spam (a
            few messages in a quiet window aren't spam).
        answer_min_msgs: Messages a window needs before it can be answer spam.
        answer_rate_quantile: The window's chat rate must also reach this quantile of the VOD's
            windows with chat, so the floor scales with the channel's own chat.
        answer_factor: Share an answer-spam window keeps; 1.0 (the default) turns the rule off.
        silent_warning_min_s: Seconds of silent audio at which the result carries the
            ``audio_partly_silent`` warning.
    """

    enabled: bool = True
    intro_s: float = 180.0
    outro_s: float = 180.0
    edge_factor: float = 0.5
    silence_frac_min: float = 0.95
    silence_factor: float = 0.5
    participation_frac_min: float = 0.25
    participation_factor: float = 0.5
    answer_frac_min: float = 0.7
    answer_min_msg_rate: float = 2.0
    answer_min_msgs: float = 40.0
    answer_rate_quantile: float = 0.9
    answer_factor: float = 1.0
    silent_warning_min_s: float = 60.0

    def __post_init__(self) -> None:
        for item in fields(self):
            value = getattr(self, item.name)
            if item.name == "enabled":
                if not isinstance(value, bool):
                    raise ValueError("enabled must be true or false")
                continue
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise ValueError(f"{item.name} must be a number")
            if not (math.isfinite(value) and value >= 0):
                raise ValueError(f"{item.name} must be finite and >= 0")
            if item.name.endswith(("_factor", "_frac_min", "_quantile")) and value > 1:
                raise ValueError(f"{item.name} must be between 0 and 1")


def load_ranking_rules(path: Path) -> RankingRules:
    """Read a ranking rules file such as ``configs/ranking.yaml``.

    Keys the file leaves out keep their `RankingRules` defaults.

    Raises:
        RankingRulesError: The file can't be read, isn't a mapping, has an unknown key or a bad
            value.
    """
    try:
        data: object = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise RankingRulesError(f"can't read ranking rules {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise RankingRulesError(f"ranking rules {path} is not a mapping")
    version = data.pop("version", None)
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise RankingRulesError(f"ranking rules {path}: 'version' must be a positive integer")
    known = {item.name for item in fields(RankingRules)}
    unknown = sorted(set(data) - known)
    if unknown:
        raise RankingRulesError(f"ranking rules {path}: unknown keys: {', '.join(unknown)}")
    try:
        return RankingRules(**data)
    except (TypeError, ValueError) as exc:
        raise RankingRulesError(f"ranking rules {path}: {exc}") from exc


def resolve_ranking_rules(path: Path | None) -> RankingRules:
    """The rules a command runs with: `path` if given, else `DEFAULT_RULES_PATH` if it exists.

    Without either the built-in defaults apply (they equal the shipped file), so a command run
    outside the repository root still works; a path that was asked for and is missing fails.

    Raises:
        RankingRulesError: `path` (or the default file) can't be read or is malformed.
    """
    if path is not None:
        return load_ranking_rules(path)
    if DEFAULT_RULES_PATH.is_file():
        return load_ranking_rules(DEFAULT_RULES_PATH)
    log.info("ranking rules file not found, using built-in defaults path=%s", DEFAULT_RULES_PATH)
    return RankingRules()


@dataclass(frozen=True, slots=True)
class RuledScores:
    """`apply_ranking_rules`' output.

    Attributes:
        scores: The input rows with ``score`` replaced by the adjusted score.
        flags: ``window_id`` → the flags (from `FLAGS`) that lowered that window's score. A
            window at or below the median, or with no matching rule, is absent.
    """

    scores: pl.DataFrame
    flags: Mapping[str, tuple[str, ...]]


def apply_ranking_rules(
    scores: pl.DataFrame,
    features: pl.DataFrame | None,
    *,
    duration_s: float,
    rules: RankingRules,
) -> RuledScores:
    """Lower the scores of windows that only look busy (the module docstring's table).

    Args:
        scores: One VOD's window scores: ``window_id``, ``start_s``, ``end_s``, ``score``.
        features: The VOD's window features keyed by ``window_id`` (any subset), or None.
        duration_s: The VOD's length in seconds.
        rules: Thresholds and factors.

    Returns:
        The adjusted scores in the input's row order, and the flags behind each change.
    """
    if not rules.enabled or scores.height == 0:
        return RuledScores(scores, {})
    frame = scores.select("window_id", "start_s", "end_s")
    if features is not None and "window_id" in features.columns:
        wanted = [
            c
            for c in (
                "audio_silence_frac",
                "audio_is_muted",
                "chat_msg_rate",
                "chat_greeting_rate",
                "chat_farewell_rate",
                "chat_answer_frac",
            )
            if c in features.columns
        ]
        frame = frame.join(
            features.select("window_id", *wanted), on="window_id", how="left", maintain_order="left"
        )
    columns = set(frame.columns)

    def value(column: str) -> np.ndarray:
        return frame[column].cast(pl.Float64).fill_null(0.0).fill_nan(0.0).to_numpy()

    n = frame.height
    starts = frame["start_s"].to_numpy()
    ends = frame["end_s"].to_numpy()
    none = np.zeros(n, dtype=bool)
    edges = duration_s > rules.intro_s + rules.outro_s
    near_start = starts < rules.intro_s
    near_end = ends > duration_s - rules.outro_s
    matches: dict[str, np.ndarray] = {
        "stream_start": near_start if edges else none,
        "stream_end": near_end if edges else none,
        "silent_audio": none,
        "greetings": none,
        "answer_spam": none,
    }
    if "audio_silence_frac" in columns:
        muted = value("audio_is_muted") >= 0.5 if "audio_is_muted" in columns else none
        matches["silent_audio"] = (value("audio_silence_frac") >= rules.silence_frac_min) & ~muted
    if "chat_msg_rate" in columns:
        rate = value("chat_msg_rate")
        if {"chat_greeting_rate", "chat_farewell_rate"} <= columns:
            waves = value("chat_greeting_rate") + value("chat_farewell_rate")
            share = np.divide(waves, rate, out=np.zeros(n), where=rate > 0)
            matches["greetings"] = (
                (near_start | near_end) & (rate > 0) & (share >= rules.participation_frac_min)
            )
        if "chat_answer_frac" in columns:
            n_msgs = rate * (ends - starts)
            with_chat = rate[rate > 0]
            busy_for_vod = (
                float(np.quantile(with_chat, rules.answer_rate_quantile)) if len(with_chat) else 0.0
            )
            matches["answer_spam"] = (
                (n_msgs >= rules.answer_min_msgs - 1e-9)
                & (rate >= rules.answer_min_msg_rate)
                & (rate >= busy_for_vod)
                & (value("chat_answer_frac") >= rules.answer_frac_min)
            )
    factors = {
        "stream_start": rules.edge_factor,
        "stream_end": rules.edge_factor,
        "silent_audio": rules.silence_factor,
        "greetings": rules.participation_factor,
        "answer_spam": rules.answer_factor,
    }
    factor = np.ones(n)
    for flag in FLAGS:
        factor = np.where(matches[flag], factor * factors[flag], factor)

    raw = scores["score"].cast(pl.Float64).to_numpy()
    baseline = float(np.median(raw))
    lowered = (raw > baseline) & (factor < 1.0)
    adjusted = np.where(lowered, baseline + factor * (raw - baseline), raw)
    ids = frame["window_id"].to_list()
    # Only rules that changed something are reported (a factor of 1 is a rule turned off).
    flags = {
        ids[i]: tuple(flag for flag in FLAGS if matches[flag][i] and factors[flag] < 1.0)
        for i in np.flatnonzero(lowered)
    }
    counts = {flag: int(matches[flag].sum()) for flag in FLAGS}
    log.info(
        "ranking rules windows=%d lowered=%d %s",
        n,
        int(lowered.sum()),
        " ".join(f"{flag}={count}" for flag, count in counts.items()),
    )
    return RuledScores(scores.with_columns(score=pl.Series(adjusted, dtype=pl.Float64)), flags)


def candidate_flags(
    candidates: Sequence[Candidate], ruled: RuledScores
) -> dict[str, tuple[str, ...]]:
    """The flags that lowered each candidate's score, by ``candidate_id``.

    A candidate's score is one window's adjusted score (its peak, or the best window of a
    viewer-clip moment), so its flags are those of the overlapping window holding that score.
    Candidates whose score no rule lowered are absent.
    """
    if not ruled.flags:
        return {}
    frame = ruled.scores
    ids = frame["window_id"].to_list()
    starts = frame["start_s"].to_numpy()
    ends = frame["end_s"].to_numpy()
    values = frame["score"].cast(pl.Float64).to_numpy()
    found: dict[str, tuple[str, ...]] = {}
    for candidate in candidates:
        inside = np.flatnonzero((starts < candidate.end_s) & (ends > candidate.start_s))
        if not len(inside):
            continue
        exact = inside[np.abs(values[inside] - candidate.score) <= _SCORE_TOLERANCE]
        peak = int(exact[0]) if len(exact) else int(inside[np.argmax(values[inside])])
        flags = ruled.flags.get(ids[peak])
        if flags:
            found[candidate.candidate_id] = flags
    return found


def silent_seconds(features: pl.DataFrame | None, rules: RankingRules) -> float:
    """Seconds of windows whose (not Twitch-muted) audio is silent by `rules`.

    `features` needs ``audio_silence_frac`` and window bounds (``start_s``, ``end_s``); without
    them the answer is 0.
    """
    if features is None or not {"audio_silence_frac", "start_s", "end_s"} <= set(features.columns):
        return 0.0
    silent = pl.col("audio_silence_frac").fill_null(0.0) >= rules.silence_frac_min
    if "audio_is_muted" in features.columns:
        silent = silent & (pl.col("audio_is_muted").fill_null(0.0) < 0.5)
    return float(
        features.filter(silent).select((pl.col("end_s") - pl.col("start_s")).sum()).item() or 0.0
    )


__all__ = [
    "DEFAULT_RULES_PATH",
    "FLAGS",
    "RankingRules",
    "RankingRulesError",
    "RuledScores",
    "apply_ranking_rules",
    "candidate_flags",
    "load_ranking_rules",
    "resolve_ranking_rules",
    "silent_seconds",
]
