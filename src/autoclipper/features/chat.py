"""Chat features per time window: how busy chat is and how it reacts (docs/ml/features.md).

`compute_chat_features` turns a VOD's ``chat_messages`` rows and its ``windows`` into the
``features_chat`` table: one row per window, keyed by ``window_id``. Everything is vectorized with
Polars, so a multi-hour VOD with hundreds of thousands of messages takes well under a second.

Rules (docs/ml/features.md § Chat preprocessing, § Chat features, § Normalization):

- **Clip links first.** Whole messages matching the lexicon's ``clip_filter`` patterns (clip URLs
  and clip-created announcements) are removed before any aggregate, lead/lag windows included:
  they give a moment away. Only their count is logged.
- **A message belongs to the window whose ``[start_s, end_s)`` holds its offset.** Messages
  outside every window (e.g. after the last, dropped, partial window) are not counted; their
  number is logged. Windows must not overlap.
- **Empty windows are 0, never null.** Rates, fractions and means of a window without messages
  are 0; its z-scores still follow the formula.
- **``_z``** is the z-score against all windows of the same VOD (population standard deviation);
  **``_rz``** against the previous `ROLLING_WINDOWS` windows (at least two). A standard deviation
  below `ZERO_STD` gives 0, so a VOD where every window is alike scores 0 everywhere.
- **Lead/lag** columns are ``chat_msg_rate_z`` of windows t±k of the same VOD, and 0 past the
  VOD's edges (the VOD's average). Leads are offline-only: chat reacts 5-15 s after the event.
- **Participation vs. reaction** (#192): greeting and farewell waves ("hi", "LATE", "bye") and
  answer spam (many different short messages, as in chat-guessing games) are busy chat that
  isn't reacting to anything. ``chat_greeting_rate``, ``chat_farewell_rate``,
  ``chat_distinct_frac`` and ``chat_answer_frac`` let the ranking rule and the models tell them
  from a shared reaction such as a wave of "KEKW".
"""

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

import polars as pl
import yaml

log = logging.getLogger(__name__)

# Bump when a definition below changes (docs/ml/features.md § Naming). The lexicon's own version
# is appended, so changing a word list also changes `feature_version`.
CHAT_FEATURE_VERSION: Final = "chat-v2"

# docs/ml/features.md § Normalization: R = 30 windows (5 minutes of 10 s windows).
ROLLING_WINDOWS: Final = 30
ZERO_STD: Final = 1e-9
# chat_repeat_frac: a message repeats one seen this many seconds before, or less.
REPEAT_WINDOW_S: Final = 10.0
LEAD_LAG_WINDOWS: Final = (1, 2, 3)

REACTIONS: Final = ("laugh", "hype", "shock", "keyword")
"""Lexicon reaction categories; each becomes ``chat_<category>_rate``."""

PARTICIPATION: Final = ("greeting", "farewell")
"""Optional lexicon categories for check-ins and goodbyes; each becomes ``chat_<category>_rate``
(0 everywhere when the lexicon has no such section)."""

# Answer-spam defaults (the lexicon's optional `answers` section overrides them): a message of at
# most this many whitespace tokens and characters can be an answer.
ANSWER_MAX_TOKENS: Final = 3
ANSWER_MAX_CHARS: Final = 25

MESSAGE_COLUMNS: Final = ("video_id", "offset_s", "user_hash", "text", "n_emotes", "bits")
WINDOW_COLUMNS: Final = ("window_id", "video_id", "index", "start_s", "end_s")

FEATURE_COLUMNS: Final = (
    "chat_msg_rate",
    "chat_msg_rate_z",
    "chat_msg_rate_rz",
    "chat_unique_rate",
    "chat_unique_rate_z",
    "chat_emote_rate",
    "chat_emote_frac",
    "chat_caps_frac",
    "chat_repeat_frac",
    "chat_mean_len",
    "chat_keyword_rate",
    "chat_laugh_rate",
    "chat_hype_rate",
    "chat_shock_rate",
    "chat_event_count",
    *(f"chat_msg_rate_lead{k}" for k in LEAD_LAG_WINDOWS),
    *(f"chat_msg_rate_lag{k}" for k in LEAD_LAG_WINDOWS),
    # chat-v2 (#192): appended, so the chat-v1 columns keep their names, order and meaning.
    "chat_greeting_rate",
    "chat_farewell_rate",
    "chat_distinct_frac",
    "chat_answer_frac",
)
"""The ``features_chat`` feature columns, all Float64, in output order."""


class LexiconError(ValueError):
    """The lexicon file is missing, malformed, or holds a pattern that doesn't compile."""


@dataclass(frozen=True, slots=True)
class Lexicon:
    """Compiled reaction, event and clip-link patterns (``configs/lexicon.yaml``).

    Attributes:
        version: The file's ``version``; part of ``feature_version``.
        reactions: One regex per category in `REACTIONS`.
        events: Regex for subscription, gift and raid notices.
        clip_filter: Regex for messages that give a clip away.
        participation: One regex per `PARTICIPATION` category the file defines (greetings and
            farewells); a missing one counts no messages.
        answer_max_tokens: Longest answer-spam message, in whitespace tokens.
        answer_max_chars: Longest answer-spam message, in characters.
    """

    version: int
    reactions: Mapping[str, str]
    events: str
    clip_filter: str
    participation: Mapping[str, str] = field(default_factory=dict[str, str])
    answer_max_tokens: int = ANSWER_MAX_TOKENS
    answer_max_chars: int = ANSWER_MAX_CHARS

    @property
    def feature_version(self) -> str:
        """``feature_version`` of features computed with this lexicon."""
        return f"{CHAT_FEATURE_VERSION}+lexicon-v{self.version}"


def load_lexicon(path: Path) -> Lexicon:
    """Read and compile a lexicon file such as ``configs/lexicon.yaml``.

    Raises:
        LexiconError: The file can't be read, isn't the documented shape, or a pattern doesn't
            compile.
    """
    try:
        data: object = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise LexiconError(f"can't read lexicon {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise LexiconError(f"lexicon {path} is not a mapping")
    version = data.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise LexiconError(f"lexicon {path}: 'version' must be a positive integer")
    categories = data.get("categories")
    if (
        not isinstance(categories, dict)
        or not set(REACTIONS) <= set(categories)
        or not set(categories) <= {*REACTIONS, *PARTICIPATION}
    ):
        raise LexiconError(
            f"lexicon {path}: 'categories' must hold {', '.join(REACTIONS)} and may hold "
            f"{', '.join(PARTICIPATION)}"
        )
    reactions = {
        name: _category_regex(categories[name], f"{path}: categories.{name}") for name in REACTIONS
    }
    participation = {
        name: _category_regex(categories[name], f"{path}: categories.{name}", whole_message=True)
        for name in PARTICIPATION
        if name in categories
    }
    events = _category_regex(data.get("events"), f"{path}: events")
    clip_filter = _category_regex(data.get("clip_filter"), f"{path}: clip_filter")
    answers = data.get("answers", {})
    if not isinstance(answers, dict) or not set(answers) <= set(_ANSWER_DEFAULTS):
        raise LexiconError(f"lexicon {path}: 'answers' must be a mapping of max_tokens, max_chars")
    limits = {key: answers.get(key, default) for key, default in _ANSWER_DEFAULTS.items()}
    for key, value in limits.items():
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise LexiconError(f"lexicon {path}: answers.{key} must be a positive integer")
    return Lexicon(
        version,
        reactions,
        events,
        clip_filter,
        participation,
        answer_max_tokens=limits["max_tokens"],
        answer_max_chars=limits["max_chars"],
    )


_ANSWER_DEFAULTS: Final = {"max_tokens": ANSWER_MAX_TOKENS, "max_chars": ANSWER_MAX_CHARS}


def _category_regex(section: object, where: str, *, whole_message: bool = False) -> str:
    """One regex matching a message when any of the section's entries matches.

    With `whole_message`, the message must consist of the section's entries alone (repeated,
    separated by spaces or punctuation): "hi chat" and "bye bye" match, "hey what was that" and
    "he said bye" don't. Greetings and farewells use it, so a reaction that merely contains
    "hey" or "bye" isn't counted as one.
    """
    if not isinstance(section, dict) or not set(section) <= {"emotes", "words", "patterns"}:
        raise LexiconError(f"{where} must be a mapping of emotes, words and patterns")
    emotes = _strings(section.get("emotes", []), f"{where}.emotes")
    words = _strings(section.get("words", []), f"{where}.words")
    patterns = _strings(section.get("patterns", []), f"{where}.patterns")
    if whole_message:
        return _compiled(_whole_message_regex(emotes, words, patterns, where), where)
    parts: list[str] = []
    # Emoji match anywhere ("😂😂😂"); other emotes are whole tokens in exact case ("W" is hype,
    # "w" isn't), optionally followed by punctuation ("KEKW!").
    emoji = [e for e in emotes if not any(c.isascii() for c in e)]
    names = [e for e in emotes if e not in emoji]
    if emoji:
        parts.append("|".join(pl.escape_regex(e) for e in emoji))
    if names:
        alternatives = "|".join(pl.escape_regex(e) for e in sorted(names, key=len, reverse=True))
        parts.append(rf"(?:^|\s)(?:{alternatives})[!?.,:;]*(?:\s|$)")
    if words:
        alternatives = "|".join(pl.escape_regex(w) for w in sorted(words, key=len, reverse=True))
        parts.append(rf"(?i:\b(?:{alternatives})\b)")
    parts.extend(f"(?i:{pattern})" for pattern in patterns)
    if not parts:
        raise LexiconError(f"{where} has no entries")
    return _compiled("|".join(parts), where)


def _whole_message_regex(
    emotes: list[str], words: list[str], patterns: list[str], where: str
) -> str:
    """A regex for messages made only of these entries (see `_category_regex`)."""
    tokens: list[str] = []
    if emotes:  # exact case, emoji included
        tokens.append("|".join(pl.escape_regex(e) for e in sorted(emotes, key=len, reverse=True)))
    if words:
        alternatives = "|".join(pl.escape_regex(w) for w in sorted(words, key=len, reverse=True))
        tokens.append(rf"(?i:(?:{alternatives})\b)")
    tokens.extend(f"(?i:{pattern})" for pattern in patterns)
    if not tokens:
        raise LexiconError(f"{where} has no entries")
    token = "|".join(f"(?:{t})" for t in tokens)
    return rf"^\W*(?:(?:{token})\W*)+$"


def _compiled(regex: str, where: str) -> str:
    try:
        pl.Series([""]).str.contains(regex)
    except pl.exceptions.ComputeError as exc:
        raise LexiconError(f"{where}: a pattern doesn't compile: {exc}") from exc
    return regex


def _strings(value: object, where: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise LexiconError(f"{where} must be a list of non-blank strings")
    return [str(v) for v in value]


def compute_chat_features(
    windows: pl.DataFrame, messages: pl.DataFrame, *, lexicon: Lexicon
) -> pl.DataFrame:
    """Compute the chat features of every window.

    Args:
        windows: Rows of the ``windows`` table (`autoclipper.labels.windows`), one or more VODs.
        messages: Rows of the ``chat_messages`` table for those VODs (at least
            `MESSAGE_COLUMNS`). Messages of VODs without windows are ignored.
        lexicon: From `load_lexicon`.

    Returns:
        One row per window, in ``video_id`` and window order: ``window_id``, the
        `FEATURE_COLUMNS` (Float64, never null) and ``feature_version``.

    Raises:
        ValueError: A column is missing or null, ``window_id`` repeats, windows of a VOD
            overlap or have no length, or a message offset is not finite.
    """
    _check_columns(windows, WINDOW_COLUMNS, "windows")
    _check_columns(messages, MESSAGE_COLUMNS, "messages")
    windows = windows.select(WINDOW_COLUMNS).sort(["video_id", "start_s"])
    if windows["window_id"].n_unique() != windows.height:
        raise ValueError("windows lists a window_id more than once")
    bad = windows.filter(
        ~(pl.col("end_s") > pl.col("start_s"))
        | (pl.col("start_s") < pl.col("end_s").shift(1).over("video_id"))
    )
    if bad.height:
        raise ValueError(f"windows overlap or have no length, e.g. {bad['window_id'][0]}")
    if not messages["offset_s"].is_finite().all():
        raise ValueError("messages has an offset_s that is not finite")

    chat = messages.select(MESSAGE_COLUMNS).filter(
        pl.col("video_id").is_in(windows["video_id"].unique().implode())
    )
    is_clip_link = pl.col("text").str.contains(lexicon.clip_filter)
    n_clip_links = chat.filter(is_clip_link).height
    chat = chat.filter(~is_clip_link)
    chat = _with_message_signals(chat, lexicon)

    # Each message goes to the last window of its VOD starting at or before it, if it is still
    # inside that window ([start_s, end_s)).
    placed = (
        chat.sort(["video_id", "offset_s"])
        .join_asof(
            windows.select("video_id", "window_id", "start_s", "end_s"),
            left_on="offset_s",
            right_on="start_s",
            by="video_id",
            strategy="backward",
            check_sortedness=False,
        )
        .filter(pl.col("window_id").is_not_null() & (pl.col("offset_s") < pl.col("end_s")))
        # An answer is a short, reaction-free message nobody else sent in the same window: many
        # of them is chat answering (a guessing game), not chat reacting together.
        .with_columns(
            is_answer=pl.col("is_short_plain") & (pl.len().over(["window_id", "normalized"]) == 1)
        )
    )
    n_outside = chat.height - placed.height
    silent = sorted(set(windows["video_id"].unique()) - set(placed["video_id"].unique()))
    if silent:
        # Could be a quiet stream, or chat that failed to download: say which VODs.
        log.warning("no chat messages in any window video_ids=%s", ",".join(silent))
    log.info(
        "chat features windows=%d messages=%d clip_links_removed=%d outside_windows=%d",
        windows.height,
        placed.height,
        n_clip_links,
        n_outside,
    )

    counts = placed.group_by("window_id").agg(
        n_msgs=pl.len(),
        n_unique=pl.col("user_hash").n_unique(),
        n_emotes=pl.col("n_emotes").sum(),
        n_tokens=pl.col("n_tokens").sum(),
        n_letters=pl.col("n_letters").sum(),
        n_upper=pl.col("n_upper").sum(),
        n_chars=pl.col("n_chars").sum(),
        n_repeats=pl.col("is_repeat").sum(),
        n_events=pl.col("is_event").sum(),
        n_distinct=pl.col("normalized").n_unique(),
        n_answers=pl.col("is_answer").sum(),
        **{f"n_{name}": pl.col(f"is_{name}").sum() for name in (*REACTIONS, *PARTICIPATION)},
    )
    count_columns = [c for c in counts.columns if c != "window_id"]
    frame = (
        windows.join(counts, on="window_id", how="left", maintain_order="left")
        .with_columns(pl.col(count_columns).fill_null(0).cast(pl.Float64))
        .with_columns(seconds=pl.col("end_s") - pl.col("start_s"))
    )

    def per_second(column: str) -> pl.Expr:
        return pl.col(column) / pl.col("seconds")

    def fraction(part: str, whole: str) -> pl.Expr:
        return pl.when(pl.col(whole) > 0).then(pl.col(part) / pl.col(whole)).otherwise(0.0)

    frame = frame.with_columns(
        chat_msg_rate=per_second("n_msgs"),
        chat_unique_rate=per_second("n_unique"),
        chat_emote_rate=per_second("n_emotes"),
        chat_emote_frac=fraction("n_emotes", "n_tokens").clip(upper_bound=1.0),
        chat_caps_frac=fraction("n_upper", "n_letters"),
        chat_repeat_frac=fraction("n_repeats", "n_msgs"),
        chat_mean_len=fraction("n_chars", "n_msgs"),
        chat_event_count=pl.col("n_events"),
        chat_distinct_frac=fraction("n_distinct", "n_msgs"),
        chat_answer_frac=fraction("n_answers", "n_msgs"),
        **{f"chat_{name}_rate": per_second(f"n_{name}") for name in (*REACTIONS, *PARTICIPATION)},
    )
    frame = frame.with_columns(
        chat_msg_rate_z=_vod_z("chat_msg_rate"),
        chat_unique_rate_z=_vod_z("chat_unique_rate"),
        chat_msg_rate_rz=_rolling_z("chat_msg_rate"),
    )
    frame = frame.with_columns(
        *(
            pl.col("chat_msg_rate_z")
            .shift(-k)
            .over("video_id")
            .fill_null(0.0)
            .alias(f"chat_msg_rate_lead{k}")
            for k in LEAD_LAG_WINDOWS
        ),
        *(
            pl.col("chat_msg_rate_z")
            .shift(k)
            .over("video_id")
            .fill_null(0.0)
            .alias(f"chat_msg_rate_lag{k}")
            for k in LEAD_LAG_WINDOWS
        ),
    )
    return frame.select(
        "window_id",
        *(pl.col(c).cast(pl.Float64) for c in FEATURE_COLUMNS),
        feature_version=pl.lit(lexicon.feature_version),
    )


def _with_message_signals(chat: pl.DataFrame, lexicon: Lexicon) -> pl.DataFrame:
    """Add per-message counts and flags; repeats are found across window boundaries."""
    text = pl.col("text")
    normalized = text.str.to_lowercase().str.replace_all(r"\s+", " ").str.strip_chars()
    participation = {
        name: text.str.contains(lexicon.participation[name])
        if name in lexicon.participation
        else pl.lit(False)
        for name in PARTICIPATION
    }
    return (
        chat.with_columns(
            n_tokens=text.str.count_matches(r"\S+"),
            n_letters=text.str.count_matches(r"\p{L}"),
            n_upper=text.str.count_matches(r"\p{Lu}"),
            n_chars=text.str.len_chars(),
            is_event=(pl.col("bits") > 0) | text.str.contains(lexicon.events),
            normalized=normalized,
            **{f"is_{name}": text.str.contains(lexicon.reactions[name]) for name in REACTIONS},
            **{f"is_{name}": flag for name, flag in participation.items()},
        )
        .with_columns(
            # Answer-shaped: short, has a letter or digit, no emote, and neither a reaction, a
            # greeting/farewell nor an event notice. Whether anyone else sent it is checked per
            # window once messages are placed.
            is_short_plain=pl.col("n_tokens").is_between(1, lexicon.answer_max_tokens)
            & (pl.col("n_chars") <= lexicon.answer_max_chars)
            & text.str.contains(r"[\p{L}\p{N}]")
            & (pl.col("n_emotes") == 0)
            & ~pl.any_horizontal(
                pl.col(f"is_{name}") for name in (*REACTIONS, *PARTICIPATION, "event")
            )
        )
        .sort(["video_id", "normalized", "offset_s"])
        .with_columns(
            is_repeat=(pl.col("normalized") != "")
            & (
                pl.col("offset_s") - pl.col("offset_s").shift(1).over(["video_id", "normalized"])
                <= REPEAT_WINDOW_S
            ).fill_null(False)
        )
    )


def _vod_z(column: str) -> pl.Expr:
    x = pl.col(column)
    std = x.std(ddof=0).over("video_id")
    return pl.when(std >= ZERO_STD).then((x - x.mean().over("video_id")) / std).otherwise(0.0)


def _rolling_z(column: str) -> pl.Expr:
    x = pl.col(column)
    previous = x.shift(1)
    mean = previous.rolling_mean(ROLLING_WINDOWS, min_samples=2).over("video_id")
    std = previous.rolling_std(ROLLING_WINDOWS, min_samples=2, ddof=0).over("video_id")
    return pl.when(std >= ZERO_STD).then((x - mean) / std).otherwise(0.0)


def _check_columns(frame: pl.DataFrame, columns: Iterable[str], name: str) -> None:
    missing = sorted(set(columns) - set(frame.columns))
    if missing:
        raise ValueError(f"{name} is missing columns: {', '.join(missing)}")
    nulls = [c for c in columns if frame[c].null_count()]
    if nulls:
        raise ValueError(f"{name} has nulls in: {', '.join(nulls)}")
