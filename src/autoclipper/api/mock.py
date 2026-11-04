"""The mock job runner's fixed scenarios (docs/architecture/api-contract.md § 11).

Each VOD ID maps to an outcome: a ``done`` job with one of the § 4.7 results, or a failure. The
values follow the contract's examples exactly, so frontend fixtures written from the contract
match the mock. Scores are made up and measure nothing (``scorer.kind`` is ``mock``).
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Final

from autoclipper.analysis import (
    AnalysisResult,
    AnalysisWarning,
    Candidate,
    ScorerInfo,
    VodSummary,
)
from autoclipper.schemas import ApiError, Progress

MOCK_SCORER: Final = ScorerInfo(kind="mock", name="mock", run_id=None)

# The VOD whose video can't be downloaded: preview and export unavailable (§ 5.2).
NO_MEDIA_VIDEO_ID: Final = "9000000007"

JOB_STAGES: Final = (
    ("resolving_vod", "Looking up the VOD"),
    ("fetching_clips", "Fetching viewer clips"),
    ("downloading_chat", "Downloading chat replay"),
    ("downloading_audio", "Downloading audio"),
    ("computing_features", "Computing chat and audio features"),
    ("ranking", "Ranking moments"),
)

CHAT_MISSING: Final = AnalysisWarning(
    code="chat_missing",
    message=(
        "Chat replay isn't available for this VOD, so moments are ranked from audio and viewer "
        "clips only."
    ),
)
AUDIO_MISSING: Final = AnalysisWarning(
    code="audio_missing",
    message=(
        "Audio isn't available for this VOD, so moments are ranked from chat and viewer clips only."
    ),
)


@dataclass(frozen=True, slots=True)
class Scenario:
    """What the mock does for one VOD.

    Attributes:
        result: The finished result (with ``generated_at`` replaced at finish time), or None
            for a failing scenario.
        error: The failure, for a failing scenario.
        fail_at_stage: 1-based index of the stage the failure happens in.
        warnings: The job's warnings (equal to the result's for a done job).
    """

    result: AnalysisResult | None = None
    error: ApiError | None = None
    fail_at_stage: int = 1
    warnings: tuple[AnalysisWarning, ...] = field(default=())


def _vod(video_id: str, number: int, duration_s: float, created_at: datetime) -> VodSummary:
    return VodSummary(
        video_id=video_id,
        title=f"Synthetic stream {number}",
        broadcaster_id="9100000001",
        broadcaster_display_name="user_001",
        duration_s=duration_s,
        created_at=created_at,
    )


_GENERATED_AT: Final = datetime(2026, 10, 19, 15, 2, 41, tzinfo=UTC)

_EXAMPLE_CANDIDATES: Final = (
    Candidate(
        candidate_id="c001",
        rank=1,
        score=3.42,
        start_s=118.0,
        end_s=148.0,
        source="both",
        n_viewer_clips=3,
        top_signals=("chat_msg_rate_z", "audio_rms_max_z"),
    ),
    Candidate(
        candidate_id="c002",
        rank=2,
        score=2.87,
        start_s=251.5,
        end_s=276.5,
        source="discovered",
        n_viewer_clips=0,
        top_signals=("audio_rms_max_z",),
    ),
    Candidate(
        candidate_id="c003",
        rank=3,
        score=1.05,
        start_s=40.0,
        end_s=62.0,
        source="viewer_clips",
        n_viewer_clips=1,
        top_signals=(),
    ),
)


def _example_result(video_id: str) -> AnalysisResult:
    """The first § 4.7 result (one candidate of each source) for `video_id`."""
    return AnalysisResult(
        generated_at=_GENERATED_AT,
        vod=_vod(video_id, 1, 360.0, datetime(2026, 10, 12, 18, 0, tzinfo=UTC)),
        scorer=MOCK_SCORER,
        n_viewer_clips_total=4,
        candidates=_EXAMPLE_CANDIDATES,
    )


def scenario_for(video_id: str) -> Scenario:
    """The § 11 scenario of a VOD; any ID not listed gets the first § 4.7 result."""
    if video_id == "9000000002":
        result = AnalysisResult(
            generated_at=_GENERATED_AT,
            vod=_vod(video_id, 2, 360.0, datetime(2026, 10, 12, 18, 0, tzinfo=UTC)),
            scorer=MOCK_SCORER,
            n_viewer_clips_total=0,
            candidates=(
                Candidate(
                    candidate_id="c001",
                    rank=1,
                    score=1.8,
                    start_s=12.0,
                    end_s=40.0,
                    source="discovered",
                    n_viewer_clips=0,
                    top_signals=("chat_msg_rate_z",),
                ),
            ),
        )
        return Scenario(result=result)
    if video_id == "9000000003":
        result = AnalysisResult(
            generated_at=datetime(2026, 10, 19, 15, 10, 12, tzinfo=UTC),
            vod=_vod(video_id, 3, 300.0, datetime(2026, 10, 13, 18, 0, tzinfo=UTC)),
            scorer=MOCK_SCORER,
            n_viewer_clips_total=1,
            warnings=(CHAT_MISSING,),
            candidates=(
                Candidate(
                    candidate_id="c001",
                    rank=1,
                    score=2.1,
                    start_s=75.0,
                    end_s=100.0,
                    source="discovered",
                    n_viewer_clips=0,
                    top_signals=("audio_rms_max_z",),
                ),
                Candidate(
                    candidate_id="c002",
                    rank=2,
                    score=1.3,
                    start_s=190.0,
                    end_s=215.0,
                    source="viewer_clips",
                    n_viewer_clips=1,
                    top_signals=("chat_msg_rate_z",),
                ),
            ),
        )
        return Scenario(result=result, warnings=(CHAT_MISSING,))
    if video_id == "9000000004":
        return Scenario(
            error=ApiError(
                code="vod_unavailable",
                message=(
                    "This VOD isn't available on Twitch. It may have been deleted, expired or "
                    "made private."
                ),
            ),
            fail_at_stage=1,
        )
    if video_id == "9000000005":
        return Scenario(
            error=ApiError(
                code="no_usable_signals",
                message=(
                    "This VOD has no chat replay, no usable audio and no viewer clips, so "
                    "there's nothing to rank."
                ),
            ),
            fail_at_stage=4,
            warnings=(CHAT_MISSING, AUDIO_MISSING),
        )
    if video_id == "9000000006":
        result = AnalysisResult(
            generated_at=_GENERATED_AT,
            vod=_vod(video_id, 6, 360.0, datetime(2026, 10, 12, 18, 0, tzinfo=UTC)),
            scorer=MOCK_SCORER,
            n_viewer_clips_total=0,
            candidates=(),
        )
        return Scenario(result=result)
    return Scenario(result=_example_result(video_id))


def stage_progress(index: int, fraction: float | None = None) -> Progress:
    """`Progress` for the 1-based analysis stage `index`."""
    stage, message = JOB_STAGES[index - 1]
    return Progress(
        stage=stage,
        stage_index=index,
        stage_count=len(JOB_STAGES),
        stage_fraction=fraction,
        message=message,
    )
