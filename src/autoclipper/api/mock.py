"""The mock job runner's fixed scenarios (docs/architecture/api-contract.md § 11).

Each VOD ID maps to an outcome: a ``done`` job with one of the § 4.7 results, or a failure. The
values follow the contract's examples exactly, so frontend fixtures written from the contract
match the mock. Scores are made up and measure nothing (``scorer.kind`` is ``mock``).
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Final

from autoclipper.analysis import (
    AnalysisResult,
    AnalysisWarning,
    Candidate,
    ScorerInfo,
    SignalDetail,
    VodSummary,
)
from autoclipper.schemas import (
    ApiError,
    ChannelInfo,
    ChannelVod,
    ChannelVodsResponse,
    Progress,
)

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
        title="Loud audio & laughing chat",
        reason=(
            "Chat jumped to 6\u00d7 its usual rate with lots of laugh reactions; "
            "loudest moment of the stream; 3 viewer clips"
        ),
        signal_details=(
            SignalDetail(
                kind="chat_rate", label="Chat rate", detail="6\u00d7 usual stream rate (z=3.4)"
            ),
            SignalDetail(
                kind="chat_laugh", label="Chat reaction", detail="High laugh-reaction activity"
            ),
            SignalDetail(
                kind="audio_loudness",
                label="Audio loudness",
                detail="Loudest moment of the stream (z=2.6)",
            ),
            SignalDetail(
                kind="viewer_clips",
                label="Viewer clips",
                detail="3 viewer clips overlap this moment",
            ),
        ),
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
        title="Loud audio spike",
        reason="Sudden audio loudness spike; trimmed cleanly on speech pauses",
        signal_details=(
            SignalDetail(
                kind="audio_loudness",
                label="Audio loudness",
                detail="Sudden spike vs. last 5 min (rz=2.9)",
            ),
            SignalDetail(
                kind="pause_snap",
                label="Speech boundaries",
                detail="Start and end aligned to speech pauses",
            ),
        ),
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
        title="Viewer-clipped moment",
        reason="1 viewer clip",
        signal_details=(
            SignalDetail(
                kind="viewer_clips",
                label="Viewer clips",
                detail="1 viewer clip overlaps this moment",
            ),
        ),
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
                    title="Hype chat surge",
                    reason="Chat jumped to 4\u00d7 its usual rate with lots of hype reactions",
                    signal_details=(
                        SignalDetail(
                            kind="chat_rate",
                            label="Chat rate",
                            detail="4\u00d7 usual stream rate (z=2.4)",
                        ),
                        SignalDetail(
                            kind="chat_hype",
                            label="Chat reaction",
                            detail="High hype-reaction activity",
                        ),
                    ),
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
                    title="Loudest moment of stream",
                    reason="Loudest moment of the stream",
                    signal_details=(
                        SignalDetail(
                            kind="audio_loudness",
                            label="Audio loudness",
                            detail="Loudest moment of the stream (z=2.1)",
                        ),
                    ),
                ),
                Candidate(
                    candidate_id="c002",
                    rank=2,
                    score=1.3,
                    start_s=190.0,
                    end_s=215.0,
                    source="viewer_clips",
                    n_viewer_clips=1,
                    top_signals=(),
                    title="Viewer-clipped moment",
                    reason="1 viewer clip",
                    signal_details=(
                        SignalDetail(
                            kind="viewer_clips",
                            label="Viewer clips",
                            detail="1 viewer clip overlaps this moment",
                        ),
                    ),
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


# --- Mock channel VOD listings (§ 9.7, § 11) --------------------------------------------------

MOCK_PREANALYZED_VIDEO_IDS: Final = frozenset({"9000000002"})
"""VOD IDs pre-marked as already analyzed in mock channel listings."""


@dataclass(frozen=True, slots=True)
class MockChannelOutcome:
    """What the mock returns for ``GET /api/v1/channels/vods``."""

    response: ChannelVodsResponse | None = None
    error_status: int | None = None
    error: ApiError | None = None


@dataclass(frozen=True, slots=True)
class _MockVodEntry:
    video_id: str
    view_count: int


# Ordered newest-first; title, duration_s and created_at come from `scenario_for(video_id)` so
# browsing a channel and opening a VOD's analysis result show identical VOD metadata (§ 11).
_MOCK_CHANNEL_VODS: Final = (
    _MockVodEntry(video_id="9000000003", view_count=2890),
    _MockVodEntry(video_id="9000000001", view_count=4820),
    _MockVodEntry(video_id="9000000002", view_count=3410),
    _MockVodEntry(video_id="9000000006", view_count=640),
    _MockVodEntry(video_id="9000000007", view_count=1210),
)


def _mock_channel_vod(entry: _MockVodEntry, finished: Mapping[str, str]) -> ChannelVod:
    scenario = scenario_for(entry.video_id)
    assert scenario.result is not None
    vod = scenario.result.vod
    return ChannelVod(
        video_id=entry.video_id,
        title=vod.title,
        created_at=vod.created_at,
        duration_s=vod.duration_s,
        thumbnail_url=None,
        url=f"https://www.twitch.tv/videos/{entry.video_id}",
        view_count=entry.view_count,
        analyzed=(entry.video_id in finished or entry.video_id in MOCK_PREANALYZED_VIDEO_IDS),
        last_job_id=finished.get(entry.video_id),
    )


def channel_scenario_for(
    login: str,
    *,
    limit: int = 20,
    done_jobs: Mapping[str, str] | None = None,
) -> MockChannelOutcome:
    """The § 11 channel VOD listing scenario for a normalized lowercase channel login."""
    if login == "unknown_channel":
        return MockChannelOutcome(
            error_status=404,
            error=ApiError(
                code="channel_not_found",
                message=(
                    "No Twitch channel with that name was found. Check the spelling and try again."
                ),
            ),
        )
    if login == "no_credentials":
        return MockChannelOutcome(
            error_status=503,
            error=ApiError(
                code="twitch_credentials_missing",
                message=(
                    "Twitch API credentials aren't configured on this server. Set "
                    "TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET in .env to browse channel VODs."
                ),
                details={"missing": ["TWITCH_CLIENT_ID", "TWITCH_CLIENT_SECRET"]},
            ),
        )
    if login == "twitch_error":
        return MockChannelOutcome(
            error_status=502,
            error=ApiError(
                code="upstream_unavailable",
                message="Twitch isn't responding right now. Try again in a moment.",
            ),
        )
    if login == "empty_channel":
        return MockChannelOutcome(
            response=ChannelVodsResponse(
                channel=ChannelInfo(
                    broadcaster_id="9100000099",
                    login="empty_channel",
                    display_name="Empty_Channel",
                    profile_image_url=None,
                ),
                vods=(),
                truncated=False,
            )
        )
    finished = done_jobs or {}
    selected = _MOCK_CHANNEL_VODS[:limit]
    vods = tuple(_mock_channel_vod(entry, finished) for entry in selected)
    display_name = "User_001" if login == "user_001" else login
    return MockChannelOutcome(
        response=ChannelVodsResponse(
            channel=ChannelInfo(
                broadcaster_id="9100000001",
                login=login,
                display_name=display_name,
                profile_image_url=None,
            ),
            vods=vods,
            truncated=len(_MOCK_CHANNEL_VODS) > limit,
        )
    )
