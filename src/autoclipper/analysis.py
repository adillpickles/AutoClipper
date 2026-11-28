"""The ``AnalysisResult`` document: one analyzed VOD's ranked candidates.

Pydantic models for docs/architecture/api-contract.md § 4, shared by ``autoclipper analyze``
(which writes the JSON) and the API (which returns it), so a saved CLI result is also a valid
frontend fixture. Construction checks the invariants of § 4.5; a model that exists is valid.

Times are float seconds from VOD start, wall-clock times aware UTC datetimes serialized as
ISO-8601 with ``Z``, and Twitch IDs strings.
"""

import math
from datetime import UTC, datetime
from typing import Annotated, Final, Literal, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    PlainSerializer,
    field_validator,
    model_validator,
)

SCHEMA_VERSION: Final = "1.1"
MAX_CANDIDATES: Final = 200

type SchemaVersion = Literal["1.0", "1.1"]
type Source = Literal["viewer_clips", "discovered", "both"]
type ScorerKind = Literal["heuristic", "trained_model", "mock"]
# Where a candidate's `title` came from when it isn't the deterministic one (open enum).
type TitleSource = Literal["llm"]


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("must be timezone-aware (UTC)")
    return value.astimezone(UTC)


UtcDatetime = Annotated[
    datetime,
    AfterValidator(_aware_utc),
    PlainSerializer(lambda v: v.isoformat().replace("+00:00", "Z"), return_type=str),
]
"""An aware datetime, normalized to UTC and written as ``2026-10-19T15:02:41Z``."""


def _finite(value: float) -> float:
    if not math.isfinite(value):
        raise ValueError("must be finite")
    return value


FiniteFloat = Annotated[float, AfterValidator(_finite)]


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class VodSummary(_Model):
    """What the results header and timeline need about the VOD."""

    video_id: str = Field(min_length=1)
    title: str
    broadcaster_id: str = Field(min_length=1)
    broadcaster_display_name: str
    duration_s: FiniteFloat = Field(gt=0)
    created_at: UtcDatetime


class ScorerInfo(_Model):
    """What produced the scores."""

    kind: ScorerKind
    name: str = Field(min_length=1)
    run_id: str | None = None


class AnalysisWarning(_Model):
    """A degraded-input notice (api-contract.md § 3.5), e.g. ``chat_missing``."""

    code: str = Field(min_length=1)
    message: str


class SignalDetail(_Model):
    """One human-readable contributing signal behind a candidate's explanation."""

    kind: str = Field(min_length=1)
    label: str = Field(min_length=1)
    detail: str = Field(min_length=1)

    @field_validator("kind", "label", "detail")
    @classmethod
    def _non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value


class SpeechFeatures(_Model):
    """Per-candidate Whisper speech features (docs/ml/features.md § Phase 2, #64)."""

    speech_word_rate: FiniteFloat = Field(ge=0)
    speech_exclaim_density: FiniteFloat = Field(ge=0, le=1)
    speech_question_density: FiniteFloat = Field(ge=0, le=1)
    speech_keyword_rate: FiniteFloat = Field(ge=0)
    speech_frac: FiniteFloat = Field(ge=0, le=1)
    speech_peak_loudness_align: FiniteFloat = Field(ge=0, le=1)


class Candidate(_Model):
    """One ranked candidate moment (api-contract.md § 4.2)."""

    candidate_id: str = Field(min_length=1)
    rank: int = Field(ge=1)
    score: FiniteFloat
    start_s: FiniteFloat = Field(ge=0)
    end_s: FiniteFloat
    source: Source
    n_viewer_clips: int = Field(ge=0)
    top_signals: tuple[str, ...] = ()
    title: str | None = None
    reason: str | None = None
    signal_details: tuple[SignalDetail, ...] = ()
    speech_features: SpeechFeatures | None = Field(default=None, exclude_if=lambda v: v is None)
    # "llm" when the optional title polisher rewrote `title`; omitted for the deterministic one.
    title_source: TitleSource | None = Field(default=None, exclude_if=lambda v: v is None)

    @field_validator("title", "reason")
    @classmethod
    def _non_blank_optional(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("must not be blank when set")
        return value

    @field_validator("top_signals")
    @classmethod
    def _distinct_signals(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value) or not all(value):
            raise ValueError("top_signals must be distinct, non-empty names")
        return value

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.end_s <= self.start_s:
            raise ValueError("end_s must be after start_s")
        if (self.n_viewer_clips == 0) != (self.source == "discovered"):
            raise ValueError("n_viewer_clips is 0 exactly when source is 'discovered'")
        if self.title_source is not None and self.title is None:
            raise ValueError("title_source is set only together with a title")
        return self


MAX_ACTIVITY_POINTS: Final = 400
"""Most points an `Activity` series may have (the pipeline writes at most 300)."""
LOUDNESS_FLOOR_DBFS: Final = -60.0
"""Loudness values are clamped to [-60, 0] dBFS; -60 is the features' silence level."""


class Activity(_Model):
    """Per-window chat and loudness, downsampled for the results timeline (§ 4.1, #213).

    Point ``i`` covers ``[i * step_s, (i + 1) * step_s)`` of the VOD, the last one cut at its
    end. The series may stop up to one step short of the end (a tail too short for a window).
    ``chat`` is chat messages per second, ``loudness`` the mean frame RMS in dBFS. A family that
    didn't arrive is ``None``; a point where nothing was measured (no window, no audio frames, a
    Twitch-muted window) is a ``None`` entry, never 0.
    """

    step_s: FiniteFloat = Field(gt=0)
    chat: tuple[FiniteFloat | None, ...] | None = None
    loudness: tuple[FiniteFloat | None, ...] | None = None

    @model_validator(mode="after")
    def _check(self) -> Self:
        series = [s for s in (self.chat, self.loudness) if s is not None]
        if not series:
            raise ValueError("activity needs chat, loudness or both")
        lengths = {len(s) for s in series}
        if len(lengths) != 1:
            raise ValueError("chat and loudness must have the same length")
        (length,) = lengths
        if not 1 <= length <= MAX_ACTIVITY_POINTS:
            raise ValueError(f"activity must have 1 to {MAX_ACTIVITY_POINTS} points")
        if self.chat is not None and any(v is not None and v < 0 for v in self.chat):
            raise ValueError("chat rates must be >= 0")
        if self.loudness is not None and any(
            v is not None and not LOUDNESS_FLOOR_DBFS <= v <= 0 for v in self.loudness
        ):
            raise ValueError(f"loudness must be within [{LOUDNESS_FLOOR_DBFS:g}, 0] dBFS")
        return self

    @property
    def n_points(self) -> int:
        """How many points each series has."""
        series = self.chat if self.chat is not None else self.loudness
        assert series is not None
        return len(series)


class AnalysisResult(_Model):
    """One analyzed VOD (api-contract.md § 4.1), checked against § 4.5."""

    schema_version: SchemaVersion = SCHEMA_VERSION
    generated_at: UtcDatetime
    vod: VodSummary
    scorer: ScorerInfo
    n_viewer_clips_total: int | None = Field(ge=0)
    warnings: tuple[AnalysisWarning, ...] = ()
    candidates: tuple[Candidate, ...] = Field(max_length=MAX_CANDIDATES)
    # Pipeline counts the progress screen shows (#188). Omitted when unknown (no chat, nothing
    # scored, or a file saved before they existed), so older files load unchanged.
    n_chat_messages: int | None = Field(default=None, ge=0, exclude_if=lambda v: v is None)
    n_windows: int | None = Field(default=None, ge=0, exclude_if=lambda v: v is None)
    # Chat and loudness over the stream for the timeline (#213). Omitted when neither family
    # arrived, or in a file saved before it existed.
    activity: Activity | None = Field(default=None, exclude_if=lambda v: v is None)

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.activity is not None:
            # One point per step from 0: the last one starts before the end, and at most one
            # step of the end (the tail without a window) has no point.
            n, step = self.activity.n_points, self.activity.step_s
            end = self.vod.duration_s
            if (n - 1) * step >= end or (n + 1) * step < end - 1e-6:
                raise ValueError("activity must have one point per step_s of the VOD")
        ids = [c.candidate_id for c in self.candidates]
        if len(set(ids)) != len(ids):
            raise ValueError("candidate_id values must be unique")
        for position, candidate in enumerate(self.candidates, start=1):
            if candidate.rank != position:
                raise ValueError("rank must be 1..N in array order")
            if candidate.end_s > self.vod.duration_s:
                raise ValueError(f"candidate {candidate.candidate_id} ends after the VOD")
        keys = [rank_key(c) for c in self.candidates]
        if keys != sorted(keys):
            raise ValueError("candidates must be ordered by score, then start_s, then ID")
        clips_missing = any(w.code == "viewer_clips_missing" for w in self.warnings)
        if (self.n_viewer_clips_total is None) != clips_missing:
            raise ValueError(
                "n_viewer_clips_total is null exactly when warnings has viewer_clips_missing"
            )
        total = self.n_viewer_clips_total
        if total is None:
            if any(c.source != "discovered" for c in self.candidates):
                raise ValueError("without viewer clips every candidate must be 'discovered'")
        elif any(c.n_viewer_clips > total for c in self.candidates):
            raise ValueError("a candidate has more viewer clips than n_viewer_clips_total")
        return self


def rank_key(candidate: Candidate) -> tuple[float, float, str]:
    """Sort key of § 4.3: score descending, then earlier ``start_s``, then smaller ID."""
    return (-candidate.score, candidate.start_s, candidate.candidate_id)
