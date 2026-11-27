"""Tests for the analyze pipeline (#44): ingest → windows → features → scoring → candidates.

The ingest steps run against the same fakes as ``tests/ingest/test_vod.py`` (a fake Helix
transport, a fake TwitchDownloaderCLI and a fake downloader), and the synthetic dataset
covers the post-ingest stages end to end on real (generated) data.
"""

import json
import os
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import polars as pl
import pytest
from pydantic import SecretStr
from typer.testing import CliRunner

from autoclipper.analysis import AnalysisResult, ScorerInfo, VodSummary, rank_key
from autoclipper.analyze import AnalyzeError, analyze_vod, result_path
from autoclipper.cli import ExitCode, app
from autoclipper.config import TwitchCredentials
from autoclipper.features.chat import Lexicon, load_lexicon
from autoclipper.ffmpeg import ffmpeg_command, probe, run_command
from autoclipper.ingest.catalog import CatalogEntry, Status, read_catalog
from autoclipper.ingest.chat import PINNED_TOOL_VERSION, CommandResult
from autoclipper.ingest.media import (
    DownloadedMedia,
    DownloadRequest,
    normalized_audio_path,
)
from autoclipper.ingest.twitch_api import HelixClient
from autoclipper.ingest.vod import IngestTools
from autoclipper.synthetic import generate_dataset

FIXTURES = Path(__file__).parents[1] / "ingest" / "fixtures"
LEXICON_PATH = Path(__file__).parents[2] / "configs" / "lexicon.yaml"
VIDEO_ID = "9000000100"  # the chat fixture's VOD
BROADCASTER_ID = "9000000000"
SALT = SecretStr("fake-salt-5b1e")
VOD_CREATED = datetime(2026, 10, 1, 18, 0, 0, tzinfo=UTC)
NOW = datetime(2026, 10, 10, 12, 0, 0, tzinfo=UTC)
BANNER = f"TwitchDownloaderCLI {PINNED_TOOL_VERSION} Copyright (c) lay295 and contributors\n"


@pytest.fixture(scope="module")
def lexicon() -> Lexicon:
    return load_lexicon(LEXICON_PATH)


# --- The fakes (mirroring tests/ingest/test_vod.py) ---------------------------------------------


def video_record(
    created_at: datetime = VOD_CREATED, duration: str = "1h0m0s", type_: str = "archive"
) -> dict[str, object]:
    return {
        "id": VIDEO_ID,
        "stream_id": "9200000100",
        "user_id": BROADCASTER_ID,
        "user_login": "streamer_000",
        "user_name": "Streamer_000",
        "title": "Synthetic test stream",
        "description": "",
        "created_at": created_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "published_at": created_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "url": f"https://www.twitch.tv/videos/{VIDEO_ID}",
        "thumbnail_url": "",
        "viewable": "public",
        "view_count": 10,
        "language": "en",
        "type": type_,
        "duration": duration,
        "muted_segments": [{"duration": 30, "offset": 120}],
    }


def user_record() -> dict[str, object]:
    return {
        "id": BROADCASTER_ID,
        "login": "streamer_000",
        "display_name": "Streamer_000",
        "broadcaster_type": "partner",
        "created_at": "2000-01-01T00:00:00Z",
    }


def clip_record(index: int, created_at: datetime) -> dict[str, object]:
    return {
        "id": f"SyntheticClip{index:03d}",
        "broadcaster_id": BROADCASTER_ID,
        "creator_id": f"{9300000000 + index}",
        "video_id": VIDEO_ID,
        "game_id": "509658",
        "language": "en",
        "title": f"synthetic clip {index}",
        "view_count": 10 * index,
        "created_at": created_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "duration": 30.0,
        "vod_offset": 60 * index,
        "is_featured": False,
    }


class _Missing:
    """Sentinel for FakeTwitch's knobs: use the default fixture (None means "Twitch 404s")."""


_MISSING = _Missing()


class FakeTwitch:
    """Answers the token, Get Users (by id), Get Videos and Get Clips requests.

    A knob left as `_MISSING` serves the standard fixture; ``video=None`` makes Get Videos
    answer 404, ``clips=[]`` serves no clips and ``users=None`` makes Get Users 404.
    """

    def __init__(
        self,
        *,
        video: dict[str, object] | _Missing | None = _MISSING,
        clips: list[dict[str, object]] | _Missing = _MISSING,
        users: dict[str, object] | _Missing | None = _MISSING,
        videos_status: int = 200,
    ) -> None:
        self.video = video_record() if isinstance(video, _Missing) else video
        self.clips = (
            [clip_record(i, VOD_CREATED + timedelta(minutes=i)) for i in (1, 2)]
            if isinstance(clips, _Missing)
            else clips
        )
        self.users = user_record() if isinstance(users, _Missing) else users
        self.videos_status = videos_status
        self.paths: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.paths.append(request.url.path)
        if request.url.host == "id.twitch.tv":
            return httpx.Response(
                200,
                json={"access_token": "fake-token", "expires_in": 3600, "token_type": "bearer"},
            )
        if request.url.path == "/helix/users":
            if self.users is None:
                return httpx.Response(404, json={"error": "Not Found", "status": 404})
            return httpx.Response(200, json={"data": [self.users], "pagination": {}})
        if request.url.path == "/helix/videos":
            if self.videos_status != 200:
                return httpx.Response(
                    self.videos_status, json={"error": "Server Error", "status": self.videos_status}
                )
            if not self.video:
                return httpx.Response(404, json={"error": "Not Found", "status": 404})
            return httpx.Response(200, json={"data": [self.video], "pagination": {}})
        assert request.url.path == "/helix/clips"
        return httpx.Response(200, json={"data": self.clips, "pagination": {}})


def helix_for(fake: FakeTwitch) -> HelixClient:
    credentials = TwitchCredentials(SecretStr("fake-client-id"), SecretStr("fake-client-secret"))
    http = httpx.Client(transport=httpx.MockTransport(fake))
    return HelixClient(credentials, http_client=http, clock=lambda: NOW, sleep=lambda _: None)


class FakeChatTool:
    """Plays TwitchDownloaderCLI: writes the chat fixture (or garbage) to --output."""

    def __init__(self, payload: bytes | None = None, returncode: int = 0) -> None:
        self.payload = (
            payload if payload is not None else (FIXTURES / "chat_replay.json").read_bytes()
        )
        self.returncode = returncode
        self.calls = 0

    def __call__(self, args: Sequence[str], *, timeout_s: float | None = None) -> CommandResult:
        self.calls += 1
        Path(args[args.index("--output") + 1]).write_bytes(self.payload)
        return CommandResult(self.returncode, BANNER, "")


class FakeDownloader:
    """Copies a generated audio file into place like yt-dlp, or raises."""

    def __init__(self, source: Path | None, error: Exception | None = None) -> None:
        self.source = source
        self.error = error
        self.calls = 0

    def __call__(self, request: DownloadRequest, /) -> DownloadedMedia:
        self.calls += 1
        if self.error is not None:
            raise self.error
        assert self.source is not None
        path = request.output_dir / f"{request.stem}.m4a"
        path.parent.mkdir(parents=True, exist_ok=True)
        import shutil

        shutil.copyfile(self.source, path)
        return DownloadedMedia(path, 3.0)


@pytest.fixture
def chat_binary(tmp_path: Path) -> Path:
    # shutil.which finds only executables with a PATHEXT extension on Windows.
    import sys

    name = "TwitchDownloaderCLI.exe" if sys.platform == "win32" else "TwitchDownloaderCLI"
    binary = tmp_path / "bin" / name
    binary.parent.mkdir()
    binary.write_bytes(b"")
    binary.chmod(0o755)
    return binary


@pytest.fixture
def audio_source(make_test_media: Callable[..., Path]) -> Path:
    return make_test_media("source.m4a", duration_s=3.0, video_size=None)


def tools(
    chat_binary: Path,
    *,
    twitch: FakeTwitch | None = None,
    chat: FakeChatTool | None = None,
    downloader: FakeDownloader | None = None,
    salt: SecretStr | None = SALT,
) -> IngestTools:
    return IngestTools(
        helix=helix_for(twitch or FakeTwitch()),
        salt=salt,
        chat_runner=chat or FakeChatTool(),
        chat_binary=chat_binary,
        downloader=downloader,
        clock=lambda: NOW,
    )


def warning_codes(result: AnalysisResult) -> list[str]:
    return [warning.code for warning in result.warnings]


# --- A full run ----------------------------------------------------------------------------------


@pytest.mark.ffmpeg
def test_a_full_run_ranks_candidates_and_writes_the_result(
    tmp_path: Path,
    chat_binary: Path,
    audio_source: Path,
    lexicon: Lexicon,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    data = tmp_path / "data"
    artifacts = tmp_path / "artifacts"
    caplog.set_level(logging.INFO)

    result = analyze_vod(
        VIDEO_ID,
        data_dir=data,
        artifacts_dir=artifacts,
        tools=tools(chat_binary, downloader=FakeDownloader(audio_source)),
        lexicon=lexicon,
        clock=lambda: NOW,
    )

    assert result.vod == VodSummary(
        video_id=VIDEO_ID,
        title="Synthetic test stream",
        broadcaster_id=BROADCASTER_ID,
        broadcaster_display_name="Streamer_000",  # Get Users by ID
        duration_s=3600.0,
        created_at=VOD_CREATED,
    )
    assert result.scorer == ScorerInfo(kind="heuristic", name="chat_loudness", run_id=None)
    assert result.n_viewer_clips_total == 2
    # The fake VOD mutes a segment; everything else arrived.
    assert warning_codes(result) == ["audio_partly_muted"]
    assert result.candidates, "the planted signals should produce candidates"
    assert [c.rank for c in result.candidates] == list(range(1, len(result.candidates) + 1))
    assert [rank_key(c) for c in result.candidates] == sorted(
        rank_key(c) for c in result.candidates
    )
    assert all(0 <= c.start_s < c.end_s <= 3600.0 for c in result.candidates)
    assert any(c.source == "viewer_clips" or c.source == "both" for c in result.candidates)
    # How long each stage took is logged.
    assert "analyze stage=ingest elapsed_s=" in caplog.text
    assert "analyze stage=scoring elapsed_s=" in caplog.text
    # The written JSON is the same result, and nothing partial is left behind.
    path = result_path(artifacts, VIDEO_ID)
    assert path.is_file()
    assert AnalysisResult.model_validate_json(path.read_text(encoding="utf-8")) == result
    assert not list(artifacts.rglob("*.part"))


def test_a_vod_with_zero_viewer_clips_is_not_a_warning(
    tmp_path: Path, chat_binary: Path, audio_source: Path, lexicon: Lexicon
) -> None:
    data = tmp_path / "data"
    twitch = FakeTwitch(clips=[])

    result = analyze_vod(
        VIDEO_ID,
        data_dir=data,
        artifacts_dir=tmp_path / "artifacts",
        tools=tools(chat_binary, twitch=twitch, downloader=FakeDownloader(audio_source)),
        lexicon=lexicon,
        clock=lambda: NOW,
    )

    assert result.n_viewer_clips_total == 0
    assert "viewer_clips_missing" not in warning_codes(result)
    assert result.candidates
    assert all(candidate.source == "discovered" for candidate in result.candidates)
    assert all(candidate.n_viewer_clips == 0 for candidate in result.candidates)


# --- Missing signals degrade the result (api-contract.md § 3.5) ---------------------------------


@pytest.mark.ffmpeg
def test_missing_chat_warns_and_degrades_the_scorer(
    tmp_path: Path, chat_binary: Path, audio_source: Path, lexicon: Lexicon
) -> None:
    data = tmp_path / "data"

    result = analyze_vod(
        VIDEO_ID,
        data_dir=data,
        artifacts_dir=tmp_path / "artifacts",
        tools=tools(
            chat_binary,
            chat=FakeChatTool(returncode=1),
            downloader=FakeDownloader(audio_source),
        ),
        lexicon=lexicon,
        clock=lambda: NOW,
    )

    assert "chat_missing" in warning_codes(result)
    assert result.scorer.name == "loudness"  # chat_loudness without chat
    assert result.scorer.kind == "heuristic"
    assert result.candidates


def test_missing_audio_warns_and_degrades_the_scorer(
    tmp_path: Path, chat_binary: Path, lexicon: Lexicon
) -> None:
    result = analyze_vod(
        VIDEO_ID,
        data_dir=tmp_path / "data",
        artifacts_dir=tmp_path / "artifacts",
        tools=tools(chat_binary, downloader=FakeDownloader(None, RuntimeError("offline"))),
        lexicon=lexicon,
        clock=lambda: NOW,
    )

    assert "audio_missing" in warning_codes(result)
    assert result.scorer.name == "chat_spike"  # chat_loudness without audio
    assert result.candidates


def test_without_any_signal_viewer_clips_rank_alone(
    tmp_path: Path, chat_binary: Path, lexicon: Lexicon
) -> None:
    result = analyze_vod(
        VIDEO_ID,
        data_dir=tmp_path / "data",
        artifacts_dir=tmp_path / "artifacts",
        tools=tools(
            chat_binary,
            chat=FakeChatTool(returncode=1),
            downloader=FakeDownloader(None, RuntimeError("offline")),
        ),
        lexicon=lexicon,
        clock=lambda: NOW,
    )

    assert warning_codes(result) == ["chat_missing", "audio_missing", "audio_partly_muted"]
    assert result.scorer == ScorerInfo(kind="heuristic", name="no_signals", run_id=None)
    assert result.n_viewer_clips_total == 2
    assert [candidate.source for candidate in result.candidates] == ["viewer_clips"] * 2
    assert all(candidate.score == 0.0 for candidate in result.candidates)


def test_nothing_to_rank_after_transient_failures_is_upstream_unavailable(
    tmp_path: Path, chat_binary: Path, lexicon: Lexicon
) -> None:
    with pytest.raises(AnalyzeError) as excinfo:
        analyze_vod(
            VIDEO_ID,
            data_dir=tmp_path / "data",
            artifacts_dir=tmp_path / "artifacts",
            tools=tools(
                chat_binary,
                twitch=FakeTwitch(clips=[]),
                chat=FakeChatTool(returncode=1),
                downloader=FakeDownloader(None, RuntimeError("offline")),
            ),
            lexicon=lexicon,
            clock=lambda: NOW,
        )

    assert excinfo.value.code == "upstream_unavailable"
    assert "trying again later may work" in excinfo.value.message


def test_the_no_usable_signals_precedence() -> None:
    """§ 3.5 step 3: only a transient failure makes a rankless result upstream_unavailable."""
    from autoclipper.analyze import _nothing_to_rank

    def entry(status: Status) -> CatalogEntry:
        return CatalogEntry(
            "chat", VIDEO_ID, status, "tool", "1", NOW, None if status == "ok" else "x"
        )

    gone = {
        ("chat", VIDEO_ID): entry("unavailable"),
        ("audio", VIDEO_ID): entry("unavailable"),
        ("clips", VIDEO_ID): entry("ok"),
    }
    assert _nothing_to_rank(gone, VIDEO_ID).code == "no_usable_signals"

    failed = {**gone, ("audio", VIDEO_ID): entry("failed")}
    assert _nothing_to_rank(failed, VIDEO_ID).code == "upstream_unavailable"

    # A chosen scorer: only an input it needs decides whether a retry can help. Audio failing
    # doesn't bring chat back, so chat_spike is out of options for good.
    scorer = _nothing_to_rank(
        failed,
        VIDEO_ID,
        scorer_reason="the chat_spike scorer can't score this VOD",
        needed=("chat",),
    )
    assert scorer.code == "no_usable_signals"
    assert "chat_spike" in scorer.message
    retriable = _nothing_to_rank(
        failed,
        VIDEO_ID,
        scorer_reason="the loudness scorer can't score this VOD",
        needed=("audio",),
    )
    assert retriable.code == "upstream_unavailable"
    assert "loudness" in retriable.message


def test_which_steps_a_scorer_needs() -> None:
    """The retry advice depends on the scorer's own inputs, not on every step."""
    from autoclipper.analyze import _needed_steps
    from autoclipper.models.baselines import ChatLoudness, ChatSpike, Loudness, RandomScorer

    assert _needed_steps(ChatSpike()) == ("chat",)
    assert _needed_steps(Loudness()) == ("audio",)
    assert _needed_steps(ChatLoudness()) == ("audio", "chat")
    assert _needed_steps(RandomScorer(seed=1)) == ()


def test_a_named_scorer_without_its_signal_fails_with_the_precedence_codes(
    tmp_path: Path, chat_binary: Path, audio_source: Path, lexicon: Lexicon
) -> None:
    """A named single-signal scorer doesn't degrade; its failure carries § 3.5's code:
    upstream when its input failed transiently, so a retry may help."""
    with pytest.raises(AnalyzeError) as excinfo:
        analyze_vod(
            VIDEO_ID,
            data_dir=tmp_path / "data",
            artifacts_dir=tmp_path / "artifacts",
            tools=tools(
                chat_binary,
                chat=FakeChatTool(returncode=1),  # chat failed: a retry may fix it
                downloader=FakeDownloader(audio_source),
            ),
            lexicon=lexicon,
            baseline="chat_spike",
            clock=lambda: NOW,
        )

    assert excinfo.value.code == "upstream_unavailable"
    assert "chat_spike" in excinfo.value.message
    assert "trying again later may work" in excinfo.value.message


def test_the_random_scorer_cannot_rank_an_inputless_vod(
    tmp_path: Path, chat_binary: Path, lexicon: Lexicon
) -> None:
    """No chat, no audio, no viewer clips: nothing to rank for any scorer, random included."""
    with pytest.raises(AnalyzeError) as excinfo:
        analyze_vod(
            VIDEO_ID,
            data_dir=tmp_path / "data",
            artifacts_dir=tmp_path / "artifacts",
            tools=tools(
                chat_binary,
                twitch=FakeTwitch(clips=[]),
                chat=FakeChatTool(returncode=1),
                downloader=FakeDownloader(None, RuntimeError("offline")),
            ),
            lexicon=lexicon,
            baseline="random",
            clock=lambda: NOW,
        )

    assert excinfo.value.code == "upstream_unavailable"


def test_moments_only_results_respect_the_candidate_limit(
    tmp_path: Path, chat_binary: Path, lexicon: Lexicon
) -> None:
    """More moments than the contract's 200 candidates: the first 200 in stream order."""
    from autoclipper.analysis import MAX_CANDIDATES

    many_clips = [
        {
            "id": f"ManyClip{i:03d}",
            "broadcaster_id": BROADCASTER_ID,
            "creator_id": f"{9400000000 + i}",
            "video_id": VIDEO_ID,
            "game_id": "509658",
            "language": "en",
            "title": f"clip {i}",
            "view_count": 1,
            "created_at": (VOD_CREATED + timedelta(minutes=i)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "duration": 1.0,
            "vod_offset": 15.0 * i,
            "is_featured": False,
        }
        for i in range(MAX_CANDIDATES + 20)
    ]
    result = analyze_vod(
        VIDEO_ID,
        data_dir=tmp_path / "data",
        artifacts_dir=tmp_path / "artifacts",
        tools=tools(
            chat_binary,
            twitch=FakeTwitch(clips=many_clips),
            chat=FakeChatTool(returncode=1),
            downloader=FakeDownloader(None, RuntimeError("offline")),
        ),
        lexicon=lexicon,
        clock=lambda: NOW,
    )

    assert result.scorer.name == "no_signals"
    assert len(result.candidates) == MAX_CANDIDATES
    assert [candidate.rank for candidate in result.candidates] == list(range(1, MAX_CANDIDATES + 1))


def test_viewer_clip_candidates_cover_the_stretch_each_clip_ends_at(
    tmp_path: Path, chat_binary: Path, lexicon: Lexicon
) -> None:
    """Helix's vod_offset is a clip's end (#126): 100 s / 30 s is [70, 100], 10 s / 30 s [0, 10]."""
    records = [clip_record(1, VOD_CREATED), clip_record(2, VOD_CREATED)]
    records[0]["vod_offset"], records[1]["vod_offset"] = 100, 10
    result = analyze_vod(
        VIDEO_ID,
        data_dir=tmp_path / "data",
        artifacts_dir=tmp_path / "artifacts",
        tools=tools(
            chat_binary,
            twitch=FakeTwitch(clips=records),
            chat=FakeChatTool(returncode=1),
            downloader=FakeDownloader(None, RuntimeError("offline")),
        ),
        lexicon=lexicon,
        clock=lambda: NOW,
    )

    assert result.scorer.name == "no_signals"
    assert [(c.start_s, c.end_s, c.source) for c in result.candidates] == [
        (0.0, 10.0, "viewer_clips"),
        (70.0, 100.0, "viewer_clips"),
    ]


# --- The VOD itself (§ 3.5 step 1) ----------------------------------------------------------------


def test_a_video_that_is_not_a_past_broadcast_is_rejected(
    tmp_path: Path, chat_binary: Path, lexicon: Lexicon
) -> None:
    with pytest.raises(AnalyzeError) as excinfo:
        analyze_vod(
            VIDEO_ID,
            data_dir=tmp_path / "data",
            artifacts_dir=tmp_path / "artifacts",
            tools=tools(
                chat_binary,
                twitch=FakeTwitch(video=video_record(type_="highlight")),
                downloader=FakeDownloader(None, RuntimeError("offline")),
            ),
            lexicon=lexicon,
            clock=lambda: NOW,
        )

    assert excinfo.value.code == "vod_unsupported"
    assert "highlight" in excinfo.value.message


def test_a_vod_twitch_does_not_have_is_unavailable(
    tmp_path: Path, chat_binary: Path, lexicon: Lexicon
) -> None:
    data = tmp_path / "data"
    chat = FakeChatTool()
    downloader = FakeDownloader(None, AssertionError("must not download"))
    stages: list[str] = []
    seen_warnings: list[tuple[str, ...]] = []

    with pytest.raises(AnalyzeError) as excinfo:
        analyze_vod(
            VIDEO_ID,
            data_dir=data,
            artifacts_dir=tmp_path / "artifacts",
            tools=tools(
                chat_binary,
                twitch=FakeTwitch(video=None),
                chat=chat,
                downloader=downloader,
            ),
            lexicon=lexicon,
            clock=lambda: NOW,
            on_stage=stages.append,
            on_warnings=lambda ws: seen_warnings.append(tuple(w.code for w in ws)),
        )

    assert excinfo.value.code == "vod_unavailable"
    assert "--video" not in excinfo.value.message
    assert stages == ["resolving_vod"]
    assert seen_warnings == []
    assert (chat.calls, downloader.calls) == (0, 0)
    assert set(read_catalog(data)) == {("vod_meta", VIDEO_ID)}

    # A second run hits the cached unavailable metadata entry and still fails at resolving_vod.
    stages.clear()
    with pytest.raises(AnalyzeError) as cached:
        analyze_vod(
            VIDEO_ID,
            data_dir=data,
            artifacts_dir=tmp_path / "artifacts",
            tools=tools(
                chat_binary,
                twitch=FakeTwitch(video=None),
                chat=chat,
                downloader=downloader,
            ),
            lexicon=lexicon,
            clock=lambda: NOW,
            on_stage=stages.append,
            on_warnings=lambda ws: seen_warnings.append(tuple(w.code for w in ws)),
        )

    assert cached.value.code == "vod_unavailable"
    assert "--video" not in cached.value.message
    assert stages == ["resolving_vod"]
    assert seen_warnings == []
    assert (chat.calls, downloader.calls) == (0, 0)


def test_expired_vod_ingested_earlier_still_fails_at_resolving_vod(
    tmp_path: Path, chat_binary: Path, lexicon: Lexicon
) -> None:
    """Even if standalone ingest already marked all steps unavailable, analyze stops at
    resolving_vod instead of advancing to downloading_audio and advising --video."""
    from autoclipper.ingest.vod import ingest_vod

    data = tmp_path / "data"
    downloader = FakeDownloader(None, AssertionError("must not download"))
    ingest_vod(
        VIDEO_ID,
        data_dir=data,
        tools=tools(chat_binary, twitch=FakeTwitch(video=None), downloader=downloader),
    )
    stages: list[str] = []
    seen_warnings: list[tuple[str, ...]] = []

    with pytest.raises(AnalyzeError) as excinfo:
        analyze_vod(
            VIDEO_ID,
            data_dir=data,
            artifacts_dir=tmp_path / "artifacts",
            tools=tools(chat_binary, twitch=FakeTwitch(video=None), downloader=downloader),
            lexicon=lexicon,
            clock=lambda: NOW,
            on_stage=stages.append,
            on_warnings=lambda ws: seen_warnings.append(tuple(w.code for w in ws)),
        )

    assert excinfo.value.code == "vod_unavailable"
    assert "--video" not in excinfo.value.message
    assert stages == ["resolving_vod"]
    assert seen_warnings == []


def test_missing_helix_credentials_stop_before_downloads(
    tmp_path: Path, chat_binary: Path, lexicon: Lexicon
) -> None:
    """Uncached VOD with no Helix credentials fails at resolving_vod without downloading chat or
    audio."""
    data = tmp_path / "data"
    raw_chat = data / "raw" / "chat" / f"{VIDEO_ID}.json"
    raw_chat.parent.mkdir(parents=True)
    raw_chat.write_text('{"commenter": "user_001"}', encoding="utf-8")
    chat = FakeChatTool()
    downloader = FakeDownloader(None, AssertionError("must not download"))
    stages: list[str] = []
    seen_warnings: list[tuple[str, ...]] = []

    with pytest.raises(AnalyzeError) as excinfo:
        analyze_vod(
            VIDEO_ID,
            data_dir=data,
            artifacts_dir=tmp_path / "artifacts",
            tools=IngestTools(
                helix=None,
                salt=SALT,
                chat_runner=chat,
                chat_binary=chat_binary,
                downloader=downloader,
                clock=lambda: NOW,
            ),
            lexicon=lexicon,
            clock=lambda: NOW,
            on_stage=stages.append,
            on_warnings=lambda ws: seen_warnings.append(tuple(w.code for w in ws)),
        )

    assert excinfo.value.code == "upstream_unavailable"
    assert "metadata" in excinfo.value.message
    assert stages == ["resolving_vod"]
    assert seen_warnings == []
    assert (chat.calls, downloader.calls) == (0, 0)
    assert set(read_catalog(data)) == {("vod_meta", VIDEO_ID)}
    assert not raw_chat.exists()


def test_media_twitch_refuses_is_vod_unavailable_without_a_user_file(
    tmp_path: Path, chat_binary: Path, lexicon: Lexicon
) -> None:
    """§ 3.5: a VOD that isn't publicly viewable (deleted, expired or subscriber-only) is
    vod_unavailable, not a chat-only result nobody could ever preview or export."""
    from autoclipper.ingest.media import VodAccessRestrictedError

    with pytest.raises(AnalyzeError) as excinfo:
        analyze_vod(
            VIDEO_ID,
            data_dir=tmp_path / "data",
            artifacts_dir=tmp_path / "artifacts",
            tools=tools(
                chat_binary,
                downloader=FakeDownloader(
                    None, VodAccessRestrictedError(VIDEO_ID, "subscriber-only")
                ),
            ),
            lexicon=lexicon,
            clock=lambda: NOW,
        )

    assert excinfo.value.code == "vod_unavailable"
    assert "subscriber-only" in excinfo.value.message
    assert "--video" in excinfo.value.message


def test_metadata_that_keeps_failing_is_upstream_unavailable(
    tmp_path: Path, chat_binary: Path, lexicon: Lexicon
) -> None:
    data = tmp_path / "data"
    chat = FakeChatTool()
    downloader = FakeDownloader(None, AssertionError("must not download"))
    stages: list[str] = []
    seen_warnings: list[tuple[str, ...]] = []

    with pytest.raises(AnalyzeError) as excinfo:
        analyze_vod(
            VIDEO_ID,
            data_dir=data,
            artifacts_dir=tmp_path / "artifacts",
            tools=tools(
                chat_binary,
                twitch=FakeTwitch(videos_status=500),
                chat=chat,
                downloader=downloader,
            ),
            lexicon=lexicon,
            clock=lambda: NOW,
            on_stage=stages.append,
            on_warnings=lambda ws: seen_warnings.append(tuple(w.code for w in ws)),
        )

    assert excinfo.value.code == "upstream_unavailable"
    assert "metadata" in excinfo.value.message
    assert stages == ["resolving_vod"]
    assert seen_warnings == []
    assert (chat.calls, downloader.calls) == (0, 0)
    assert set(read_catalog(data)) == {("vod_meta", VIDEO_ID)}


def test_a_local_video_of_another_length_is_video_mismatch(
    tmp_path: Path,
    chat_binary: Path,
    make_test_media: Callable[..., Path],
    lexicon: Lexicon,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    other_vod = make_test_media("other.mp4", duration_s=60.0)
    caplog.set_level(logging.WARNING)

    with pytest.raises(AnalyzeError) as excinfo:
        analyze_vod(
            VIDEO_ID,
            data_dir=tmp_path / "data",
            artifacts_dir=tmp_path / "artifacts",
            tools=tools(chat_binary, downloader=FakeDownloader(None, RuntimeError("x"))),
            lexicon=lexicon,
            video_file=other_vod,
            clock=lambda: NOW,
        )

    assert excinfo.value.code == "video_mismatch"
    assert "is 60.0 s long but the VOD is 3600.0 s" in excinfo.value.message
    # API-facing message stays free of local filesystem paths; CLI/detail and logs retain them.
    assert str(other_vod) not in excinfo.value.message
    assert other_vod.name not in excinfo.value.message
    assert str(other_vod) in str(excinfo.value)
    assert str(other_vod) in caplog.text


@pytest.mark.ffmpeg
def test_an_unreadable_snapshot_degrades_with_recovery_advice(
    tmp_path: Path, chat_binary: Path, audio_source: Path, lexicon: Lexicon
) -> None:
    """A damaged clip snapshot (bad manifest or bad Parquet) is a warning, not a crash, and
    the warning says what to remove, because ingest never re-takes a snapshot it considers
    present."""
    data = tmp_path / "data"
    analyze_vod(
        VIDEO_ID,
        data_dir=data,
        artifacts_dir=tmp_path / "artifacts",
        tools=tools(chat_binary, downloader=FakeDownloader(audio_source)),
        lexicon=lexicon,
        clock=lambda: NOW,
    )
    (manifest,) = (tmp_path / "data" / "processed" / "clips").glob(f"{VIDEO_ID}-*.json")
    (parquet,) = (tmp_path / "data" / "processed" / "clips").glob(f"{VIDEO_ID}-*.parquet")

    for damaged in (manifest, parquet):
        keep = damaged.read_bytes()
        damaged.write_bytes(b"corrupt file, not the format it claims" * 8)

        result = analyze_vod(
            VIDEO_ID,
            data_dir=data,
            artifacts_dir=tmp_path / "artifacts",
            tools=tools(chat_binary, downloader=FakeDownloader(audio_source)),
            lexicon=lexicon,
            clock=lambda: NOW,
        )

        assert "viewer_clips_missing" in warning_codes(result)
        advice = next(
            warning.message for warning in result.warnings if warning.code == "viewer_clips_missing"
        )
        assert "couldn't be read" in advice
        assert "processed" in advice  # names where the broken snapshot lives
        assert str(data) not in advice  # no absolute host path in user-facing warnings
        assert result.n_viewer_clips_total is None
        assert all(candidate.source == "discovered" for candidate in result.candidates)
        damaged.write_bytes(keep)


@pytest.mark.ffmpeg
def test_a_rejected_local_video_is_refused_before_any_download(
    tmp_path: Path, chat_binary: Path, make_test_media: Callable[..., Path], lexicon: Lexicon
) -> None:
    """A --video file outside the contract's 2 s: the run stops at resolving_vod, before
    chat or audio is fetched, so no catalog row or file of theirs is left behind."""
    data = tmp_path / "data"
    sixty_three = make_test_media("sixty-three.mp4", duration_s=63.0)

    with pytest.raises(AnalyzeError) as excinfo:
        analyze_vod(
            VIDEO_ID,
            data_dir=data,
            artifacts_dir=tmp_path / "artifacts",
            tools=tools(
                chat_binary,
                twitch=FakeTwitch(video=video_record(duration="0h1m0s")),
                downloader=FakeDownloader(None, AssertionError("never reached")),
            ),
            lexicon=lexicon,
            video_file=sixty_three,
            clock=lambda: NOW,
        )

    assert excinfo.value.code == "video_mismatch"
    catalog = read_catalog(data)
    assert set(catalog) == {("vod_meta", VIDEO_ID)}  # nothing else was fetched
    assert not list(data.rglob("*.wav"))


@pytest.mark.ffmpeg
def test_a_rejected_video_keeps_earlier_valid_audio(
    tmp_path: Path, chat_binary: Path, make_test_media: Callable[..., Path], lexicon: Lexicon
) -> None:
    """A wrong --video file leaves earlier runs' work untouched: the rejection happens at
    resolving_vod, so the cached audio of a correct earlier file survives."""
    data = tmp_path / "data"
    sixty = make_test_media("sixty.mp4", duration_s=60.0)
    sixty_three = make_test_media("sixty-three.mp4", duration_s=63.0)
    sixty_vod = tools(
        chat_binary,
        twitch=FakeTwitch(video=video_record(duration="0h1m0s")),
        downloader=FakeDownloader(None, AssertionError("not used")),
    )

    first = analyze_vod(
        VIDEO_ID,
        data_dir=data,
        artifacts_dir=tmp_path / "artifacts",
        tools=sixty_vod,
        lexicon=lexicon,
        video_file=sixty,
        clock=lambda: NOW,
    )
    assert first.candidates  # the correct file analyzed fine

    with pytest.raises(AnalyzeError) as excinfo:
        analyze_vod(
            VIDEO_ID,
            data_dir=data,
            artifacts_dir=tmp_path / "artifacts",
            tools=sixty_vod,
            lexicon=lexicon,
            video_file=sixty_three,
            clock=lambda: NOW,
        )

    assert excinfo.value.code == "video_mismatch"
    assert normalized_audio_path(data, VIDEO_ID).is_file()  # the earlier audio survives
    audio = read_catalog(data)[("audio", VIDEO_ID)]
    assert audio.status == "ok"


def test_a_local_video_the_probe_cannot_read_is_invalid(
    tmp_path: Path,
    chat_binary: Path,
    lexicon: Lexicon,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    not_a_video = tmp_path / "not-a-video.mp4"
    not_a_video.write_bytes(b"definitely not an mp4")
    caplog.set_level(logging.WARNING)

    with pytest.raises(AnalyzeError) as excinfo:
        analyze_vod(
            VIDEO_ID,
            data_dir=tmp_path / "data",
            artifacts_dir=tmp_path / "artifacts",
            tools=tools(chat_binary, downloader=FakeDownloader(None, RuntimeError("x"))),
            lexicon=lexicon,
            video_file=not_a_video,
            clock=lambda: NOW,
        )

    assert excinfo.value.code == "invalid_video_path"
    assert str(not_a_video) not in excinfo.value.message
    assert str(not_a_video) in str(excinfo.value)
    assert str(not_a_video) in caplog.text


def test_a_matching_local_video_is_used_for_audio(
    tmp_path: Path, chat_binary: Path, make_test_media: Callable[..., Path], lexicon: Lexicon
) -> None:
    """--video with the VOD's length: its audio is converted instead of downloaded."""
    whole_vod = make_test_media("whole.mp4", duration_s=60.0)

    result = analyze_vod(
        VIDEO_ID,
        data_dir=tmp_path / "data",
        artifacts_dir=tmp_path / "artifacts",
        tools=tools(
            chat_binary,
            twitch=FakeTwitch(video=video_record(duration="0h1m0s")),
            downloader=FakeDownloader(None, AssertionError("not used")),
        ),
        lexicon=lexicon,
        video_file=whole_vod,
        clock=lambda: NOW,
    )

    assert result.candidates
    assert warning_codes(result) == ["audio_partly_muted"]


@pytest.mark.ffmpeg
def test_switching_to_local_video_replaces_cached_audio_for_ranking(
    tmp_path: Path, chat_binary: Path, lexicon: Lexicon
) -> None:
    """Analyzing from Twitch and then re-analyzing with a same-length local --video that has
    different audio re-normalizes the WAV so loudness ranking uses the local file's audio."""
    duration_s = 60.0
    twitch_audio = tmp_path / "twitch.m4a"
    local_video = tmp_path / "local.mp4"

    # Loud burst at 10-15 s in Twitch audio; loud burst at 45-50 s in the local video.
    run_command(
        ffmpeg_command(
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:sample_rate=48000:duration={duration_s:g}",
            "-af",
            "volume='if(between(t,10,15),1.0,0.02)':eval=frame",
            "-c:a",
            "aac",
            twitch_audio,
        ),
        timeout_s=60,
    )
    run_command(
        ffmpeg_command(
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=black:size=160x90:rate=5:duration={duration_s:g}",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:sample_rate=48000:duration={duration_s:g}",
            "-af",
            "volume='if(between(t,45,50),1.0,0.02)':eval=frame",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-c:a",
            "aac",
            local_video,
        ),
        timeout_s=60,
    )

    class FullLengthDownloader(FakeDownloader):
        def __call__(self, request: DownloadRequest, /) -> DownloadedMedia:
            res = super().__call__(request)
            return DownloadedMedia(res.path, duration_s)

    data = tmp_path / "data"
    artifacts = tmp_path / "artifacts"
    one_minute = FakeTwitch(video=video_record(duration="0h1m0s"), clips=[])

    first = analyze_vod(
        VIDEO_ID,
        data_dir=data,
        artifacts_dir=artifacts,
        tools=tools(chat_binary, twitch=one_minute, downloader=FullLengthDownloader(twitch_audio)),
        lexicon=lexicon,
        baseline="loudness",
        clock=lambda: NOW,
    )
    second = analyze_vod(
        VIDEO_ID,
        data_dir=data,
        artifacts_dir=artifacts,
        tools=tools(
            chat_binary,
            twitch=one_minute,
            downloader=FakeDownloader(None, AssertionError("not used")),
        ),
        lexicon=lexicon,
        video_file=local_video,
        baseline="loudness",
        clock=lambda: NOW,
    )

    assert first.candidates
    assert second.candidates
    first_top = first.candidates[0]
    second_top = second.candidates[0]
    assert first_top.start_s <= 15.0 <= first_top.end_s
    assert second_top.start_s <= 45.0 <= second_top.end_s
    assert (first_top.start_s, first_top.end_s) != (second_top.start_s, second_top.end_s)


def _reset_video_source(tmp_path: Path, *, part_s: float = 15.0) -> Path:
    """Two MPEG-TS parts joined byte for byte whose timestamps restart halfway (#95)."""
    first = tmp_path / "part0.ts"
    second = tmp_path / "part1.ts"
    for path, color, tone in ((first, "black", 440), (second, "white", 880)):
        run_command(
            ffmpeg_command(
                "-y",
                *("-f", "lavfi", "-i", f"color={color}:size=320x180:rate=10:duration={part_s:g}"),
                *(
                    "-f",
                    "lavfi",
                    "-i",
                    f"sine=frequency={tone}:sample_rate=48000:duration={part_s:g}",
                ),
                *("-c:v", "libx264", "-preset", "ultrafast", "-g", "20", "-c:a", "aac"),
                *("-output_ts_offset", "0", "-f", "mpegts", path),
            ),
            timeout_s=60,
        )
    source = tmp_path / "reset.ts"
    source.write_bytes(first.read_bytes() + second.read_bytes())
    return source


@pytest.mark.ffmpeg
def test_a_local_video_whose_timestamps_restart_passes_video_mismatch(
    tmp_path: Path, chat_binary: Path, lexicon: Lexicon
) -> None:
    """A whole-VOD file whose timestamps restart passes video_mismatch via the analysis
    timeline, while a genuinely wrong length still fails (#95)."""
    reset_vod = _reset_video_source(tmp_path, part_s=15.0)
    assert probe(reset_vod).duration_s < 20.0  # ffprobe sees only the first span

    result = analyze_vod(
        VIDEO_ID,
        data_dir=tmp_path / "data-ok",
        artifacts_dir=tmp_path / "artifacts-ok",
        tools=tools(
            chat_binary,
            twitch=FakeTwitch(video=video_record(duration="0h0m30s")),
            downloader=FakeDownloader(None, AssertionError("not used")),
        ),
        lexicon=lexicon,
        video_file=reset_vod,
        clock=lambda: NOW,
    )

    assert result.candidates
    assert probe(normalized_audio_path(tmp_path / "data-ok", VIDEO_ID)).duration_s == pytest.approx(
        30.0, abs=0.5
    )

    # A genuinely shorter file (30 s on the analysis timeline vs a 60 s VOD) still fails.
    with pytest.raises(AnalyzeError) as excinfo:
        analyze_vod(
            VIDEO_ID,
            data_dir=tmp_path / "data-bad",
            artifacts_dir=tmp_path / "artifacts-bad",
            tools=tools(
                chat_binary,
                twitch=FakeTwitch(video=video_record(duration="0h1m0s")),
                downloader=FakeDownloader(None, AssertionError("not used")),
            ),
            lexicon=lexicon,
            video_file=reset_vod,
            clock=lambda: NOW,
        )

    assert excinfo.value.code == "video_mismatch"
    assert "but the VOD is 60.0 s" in excinfo.value.message


@pytest.mark.ffmpeg
def test_corrected_duration_is_skipped_when_ffprobe_matches(
    tmp_path: Path,
    chat_binary: Path,
    make_test_media: Callable[..., Path],
    lexicon: Lexicon,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The full-file timeline re-read only runs when the cheap ffprobe duration disagrees (#95)."""
    whole_vod = make_test_media("whole.mp4", duration_s=60.0)

    def should_not_run(*args: object, **kwargs: object) -> float:
        raise AssertionError("corrected_duration_s should not run when ffprobe agrees")

    monkeypatch.setattr("autoclipper.analyze.corrected_duration_s", should_not_run)

    result = analyze_vod(
        VIDEO_ID,
        data_dir=tmp_path / "data",
        artifacts_dir=tmp_path / "artifacts",
        tools=tools(
            chat_binary,
            twitch=FakeTwitch(video=video_record(duration="0h1m0s")),
            downloader=FakeDownloader(None, AssertionError("not used")),
        ),
        lexicon=lexicon,
        video_file=whole_vod,
        clock=lambda: NOW,
    )

    assert result.candidates


def test_a_broadcaster_that_cannot_be_fetched_falls_back_to_the_id(
    tmp_path: Path, chat_binary: Path, audio_source: Path, lexicon: Lexicon
) -> None:
    result = analyze_vod(
        VIDEO_ID,
        data_dir=tmp_path / "data",
        artifacts_dir=tmp_path / "artifacts",
        tools=tools(
            chat_binary,
            twitch=FakeTwitch(users=None),  # Get Users answers 404
            downloader=FakeDownloader(audio_source),
        ),
        lexicon=lexicon,
        clock=lambda: NOW,
    )

    assert result.vod.broadcaster_display_name == BROADCASTER_ID


@pytest.mark.ffmpeg
def test_analyze_persists_broadcaster_login_and_reuses_it_without_helix(
    tmp_path: Path, chat_binary: Path, audio_source: Path, lexicon: Lexicon
) -> None:
    """Analysis retains the canonical login from its single Get Users call in broadcasters.parquet
    even when display_name differs beyond case, and reuses the cached display name offline."""
    from autoclipper.ingest.vod import read_broadcaster

    data = tmp_path / "data"
    twitch = FakeTwitch(
        users={
            "id": BROADCASTER_ID,
            "login": "caseoh_",
            "display_name": "CaseOh",
            "broadcaster_type": "partner",
            "created_at": "2000-01-01T00:00:00Z",
        }
    )

    first = analyze_vod(
        VIDEO_ID,
        data_dir=data,
        artifacts_dir=tmp_path / "artifacts",
        tools=tools(chat_binary, twitch=twitch, downloader=FakeDownloader(audio_source)),
        lexicon=lexicon,
        clock=lambda: NOW,
    )

    assert first.vod.broadcaster_display_name == "CaseOh"
    assert twitch.paths.count("/helix/users") == 1
    saved = read_broadcaster(data, BROADCASTER_ID)
    assert saved is not None
    assert saved.login == "caseoh_"
    assert saved.display_name == "CaseOh"

    # Re-analyzing the cached VOD without Helix credentials reuses the cached display name.
    second = analyze_vod(
        VIDEO_ID,
        data_dir=data,
        artifacts_dir=tmp_path / "artifacts",
        tools=IngestTools(clock=lambda: NOW),
        lexicon=lexicon,
        clock=lambda: NOW,
    )
    assert second.vod.broadcaster_display_name == "CaseOh"


@pytest.mark.ffmpeg
def test_broadcaster_cache_write_failure_does_not_fail_analysis(
    tmp_path: Path,
    chat_binary: Path,
    audio_source: Path,
    lexicon: Lexicon,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    def fail_write(*_args: object, **_kwargs: object) -> None:
        raise OSError("read-only data dir")

    monkeypatch.setattr("autoclipper.analyze.write_broadcaster", fail_write)
    caplog.set_level(logging.WARNING)

    result = analyze_vod(
        VIDEO_ID,
        data_dir=tmp_path / "data",
        artifacts_dir=tmp_path / "artifacts",
        tools=tools(chat_binary, downloader=FakeDownloader(audio_source)),
        lexicon=lexicon,
        clock=lambda: NOW,
    )

    assert result.vod.broadcaster_display_name == "Streamer_000"
    assert "broadcaster cache write failed" in caplog.text


# --- The synthetic dataset, post-ingest stages end to end ----------------------------------------


def test_analyze_the_synthetic_dataset_end_to_end(tmp_path: Path, lexicon: Lexicon) -> None:
    """The synthetic dataset is an ingested data dir (its catalog says every step ok), so
    analyze reads it without any network: features, scoring and candidates for real."""
    root = tmp_path / "synthetic"
    generate_dataset(root, seed=1)
    vods = pl.read_parquet(root / "processed" / "vods.parquet")
    video_id = str(vods["video_id"][0])
    _adapt_clip_snapshot(root, video_id)

    result = analyze_vod(
        video_id,
        data_dir=root,
        artifacts_dir=tmp_path / "artifacts",
        tools=IngestTools(),  # every step is done; no client is ever called
        lexicon=lexicon,
        clock=lambda: NOW,
    )

    assert result.scorer == ScorerInfo(kind="heuristic", name="chat_loudness", run_id=None)
    assert result.vod.video_id == video_id
    assert result.vod.duration_s == float(vods["duration_s"][0])
    clips = pl.read_parquet(root / "processed" / "clips" / f"{video_id}-20261010T120000Z.parquet")
    with_offsets = clips.filter(pl.col("vod_offset_s").is_not_null())["clip_id"].n_unique()
    assert result.n_viewer_clips_total == with_offsets
    assert result.candidates, "the planted highlights should rank"
    assert all(c.title and c.reason for c in result.candidates)
    assert "chat_missing" not in warning_codes(result)
    assert "audio_missing" not in warning_codes(result)
    assert (
        AnalysisResult.model_validate_json(
            result_path(tmp_path / "artifacts", video_id).read_text(encoding="utf-8")
        )
        == result
    )


def test_analyze_explanations_preserve_candidate_order_and_scores(
    tmp_path: Path, lexicon: Lexicon, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Attaching deterministic explanations never reorders, drops or rescores candidates."""
    root = tmp_path / "synthetic"
    generate_dataset(root, seed=1)
    vods = pl.read_parquet(root / "processed" / "vods.parquet")
    video_id = str(vods["video_id"][0])
    _adapt_clip_snapshot(root, video_id)

    with_explanations = analyze_vod(
        video_id,
        data_dir=root,
        artifacts_dir=tmp_path / "artifacts-explained",
        tools=IngestTools(),
        lexicon=lexicon,
        clock=lambda: NOW,
    )

    monkeypatch.setattr(
        "autoclipper.analyze.explain_candidates",
        lambda candidates, **_kwargs: list(candidates),
    )
    without_explanations = analyze_vod(
        video_id,
        data_dir=root,
        artifacts_dir=tmp_path / "artifacts-raw",
        tools=IngestTools(),
        lexicon=lexicon,
        clock=lambda: NOW,
    )

    assert len(with_explanations.candidates) == len(without_explanations.candidates)
    assert with_explanations.candidates
    for explained, raw in zip(
        with_explanations.candidates, without_explanations.candidates, strict=True
    ):
        assert explained.candidate_id == raw.candidate_id
        assert explained.rank == raw.rank
        assert explained.score == raw.score
        assert explained.start_s == raw.start_s
        assert explained.end_s == raw.end_s
        assert explained.source == raw.source
        assert explained.n_viewer_clips == raw.n_viewer_clips
        assert explained.top_signals == raw.top_signals
        assert explained.title is not None
        assert explained.reason is not None
        assert raw.title is None
        assert raw.reason is None


def _adapt_clip_snapshot(root: Path, video_id: str) -> None:
    """Write this VOD's clip snapshot the way ingest would have (a Parquet plus its
    manifest), from the single synthetic clips table, so the ingest step finds it done."""
    synth = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    clips = pl.read_parquet(
        root / "processed" / "clips" / f"{synth['clips_snapshot']['snapshot_id']}.parquet"
    ).filter(pl.col("video_id") == video_id)
    snapshot_id = f"{video_id}-20261010T120000Z"
    parquet = root / "processed" / "clips" / f"{snapshot_id}.parquet"
    clips.write_parquet(parquet)
    captured = "2026-10-10T12:00:00Z"
    manifest = {
        "snapshot_id": snapshot_id,
        "video_id": video_id,
        "broadcaster_id": str(clips["broadcaster_id"][0]) if clips.height else BROADCASTER_ID,
        "file": parquet.name,
        "capture_started_at": captured,
        "capture_ended_at": captured,
        "search_started_at": captured,
        "search_ended_at": captured,
        "n_ranges_queried": 1,
        "saturated_ranges": [],
        "complete": True,
        "n_clips": clips.height,
        "n_without_vod_offset": int(clips.filter(pl.col("vod_offset_s").is_null()).height),
        "vod_created_at": captured,
        "vod_age_days": 9.0,
        "mature": True,
        "stage_version": "1",
    }
    parquet.with_suffix(".json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


# --- The CLI --------------------------------------------------------------------------------------


runner = CliRunner()


def test_the_cli_rejects_a_bad_link_and_a_missing_video() -> None:
    bad = runner.invoke(app, ["analyze", "twitch.tv/someone"])
    missing = runner.invoke(app, ["analyze", VIDEO_ID, "--video", "nope.mp4"])

    assert bad.exit_code == ExitCode.USER_ERROR
    assert "invalid_vod_link" in bad.output
    assert missing.exit_code == ExitCode.USER_ERROR
    assert "invalid_video_path" in missing.output


@pytest.mark.ffmpeg
def test_the_cli_analyzes_and_reports_the_result(
    tmp_path: Path,
    chat_binary: Path,
    audio_source: Path,
    lexicon: Lexicon,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = tmp_path / "data"
    artifacts = tmp_path / "artifacts"
    monkeypatch.setenv("AUTOCLIPPER_DATA_DIR", str(data))
    monkeypatch.setenv("AUTOCLIPPER_ARTIFACTS_DIR", str(artifacts))
    monkeypatch.setenv("AUTOCLIPPER_CHAT_HASH_SALT", "test-salt")

    # Everything is ingested first, so the CLI's run skips the fetches and only analyzes.
    analyze_vod(
        VIDEO_ID,
        data_dir=data,
        artifacts_dir=artifacts,
        tools=tools(chat_binary, downloader=FakeDownloader(audio_source)),
        lexicon=lexicon,
        clock=lambda: NOW,
    )

    def ingest_tools(**kwargs: object) -> IngestTools:
        del kwargs  # every step is already done; no client is needed
        return IngestTools(clock=lambda: NOW)

    monkeypatch.setattr("autoclipper.ingest.vod.IngestTools", ingest_tools)

    result = runner.invoke(app, ["analyze", VIDEO_ID, "--lexicon", str(LEXICON_PATH)])

    assert result.exit_code == ExitCode.SUCCESS, result.output
    assert "Analyzed" in result.output
    assert "candidates" in result.output
    assert "chat_loudness" in result.output
    assert f"Wrote {artifacts / 'analysis' / f'{VIDEO_ID}.json'}" in result.output
    assert "audio_partly_muted" in result.output  # warnings reach stderr


def test_the_cli_exits_1_for_a_vod_that_is_not_a_past_broadcast(
    tmp_path: Path, chat_binary: Path, lexicon: Lexicon, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTOCLIPPER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AUTOCLIPPER_ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    monkeypatch.setenv("AUTOCLIPPER_CHAT_HASH_SALT", "test-salt")

    def ingest_tools(**kwargs: object) -> IngestTools:
        del kwargs  # the CLI has no credentials; the fake provides its own clients
        return tools(
            chat_binary,
            twitch=FakeTwitch(video=video_record(type_="highlight")),
            downloader=FakeDownloader(None, RuntimeError("x")),
        )

    monkeypatch.setattr("autoclipper.ingest.vod.IngestTools", ingest_tools)

    result = runner.invoke(app, ["analyze", VIDEO_ID, "--lexicon", str(LEXICON_PATH)])

    assert result.exit_code == ExitCode.USER_ERROR
    assert "vod_unsupported" in result.output


def test_the_cli_exits_2_when_the_metadata_keeps_failing(
    tmp_path: Path, chat_binary: Path, lexicon: Lexicon, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTOCLIPPER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AUTOCLIPPER_ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    monkeypatch.setenv("AUTOCLIPPER_CHAT_HASH_SALT", "test-salt")

    def ingest_tools(**kwargs: object) -> IngestTools:
        del kwargs
        return tools(
            chat_binary,
            twitch=FakeTwitch(videos_status=500),
            downloader=FakeDownloader(None, RuntimeError("x")),
        )

    monkeypatch.setattr("autoclipper.ingest.vod.IngestTools", ingest_tools)

    result = runner.invoke(app, ["analyze", VIDEO_ID, "--lexicon", str(LEXICON_PATH)])

    assert result.exit_code == ExitCode.EXTERNAL_FAILURE
    assert "upstream_unavailable" in result.output


def test_the_cli_reports_unexpected_failures_as_internal_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bug in the pipeline exits 3 with a generic message (§ 4.6); details go to the log."""
    monkeypatch.setenv("AUTOCLIPPER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AUTOCLIPPER_ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    monkeypatch.setenv("AUTOCLIPPER_CHAT_HASH_SALT", "test-salt")

    def broken(*args: object, **kwargs: object) -> object:
        raise RuntimeError("a bug")

    monkeypatch.setattr("autoclipper.analyze.analyze_vod", broken)

    result = runner.invoke(app, ["analyze", VIDEO_ID, "--lexicon", str(LEXICON_PATH)])

    assert result.exit_code == ExitCode.INTERNAL_ERROR
    # The message the user reads is generic; the traceback only lives in the log lines.
    error_lines = [line for line in result.output.splitlines() if "internal_error" in line]
    assert error_lines
    assert all("a bug" not in line for line in error_lines)


# --- Live -----------------------------------------------------------------------------------------


@pytest.mark.live
def test_analyze_a_real_vod(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Analyze a real VOD into a temporary data dir.

    Needs Twitch credentials, the chat hash salt, TwitchDownloaderCLI, yt-dlp, FFmpeg and
    network. Set AUTOCLIPPER_TEST_VOD to a public VOD ID in the environment (not in .env,
    which accepts only settings): ``AUTOCLIPPER_TEST_VOD=<id> uv run pytest -m live
    tests/analyze/test_analyze.py``.
    """
    video_id = os.environ.get("AUTOCLIPPER_TEST_VOD")
    if not video_id:
        pytest.skip("set AUTOCLIPPER_TEST_VOD to a public Twitch VOD ID")
    monkeypatch.setenv("AUTOCLIPPER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AUTOCLIPPER_ARTIFACTS_DIR", str(tmp_path / "artifacts"))

    result = runner.invoke(app, ["analyze", video_id, "--lexicon", str(LEXICON_PATH)])

    assert result.exit_code == ExitCode.SUCCESS, result.output
    written = AnalysisResult.model_validate_json(
        result_path(tmp_path / "artifacts", video_id).read_text(encoding="utf-8")
    )
    assert written.vod.video_id == video_id
    assert written.candidates
