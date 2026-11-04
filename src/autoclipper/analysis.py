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

SCHEMA_VERSION: Final = "1.0"
MAX_CANDIDATES: Final = 200

type Source = Literal["viewer_clips", "discovered", "both"]
type ScorerKind = Literal["heuristic", "trained_model", "mock"]


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
        return self


class AnalysisResult(_Model):
    """One analyzed VOD (api-contract.md § 4.1), checked against § 4.5."""

    schema_version: Literal["1.0"] = SCHEMA_VERSION
    generated_at: UtcDatetime
    vod: VodSummary
    scorer: ScorerInfo
    n_viewer_clips_total: int | None = Field(ge=0)
    warnings: tuple[AnalysisWarning, ...] = ()
    candidates: tuple[Candidate, ...] = Field(max_length=MAX_CANDIDATES)

    @model_validator(mode="after")
    def _check(self) -> Self:
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
