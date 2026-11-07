"""Tests for `autoclipper ingest`: the four steps, the catalog, re-runs and failures (mocked)."""

import json
import os
import shutil
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import polars as pl
import pytest
from pydantic import SecretStr
from typer.testing import CliRunner

from autoclipper.cli import ExitCode, app
from autoclipper.config import TwitchCredentials
from autoclipper.ingest.catalog import catalog_path, read_catalog
from autoclipper.ingest.chat import PINNED_TOOL_VERSION, CommandResult
from autoclipper.ingest.media import (
    DownloadedMedia,
    DownloadRequest,
    VodAccessRestrictedError,
    normalized_audio_path,
)
from autoclipper.ingest.twitch_api import HelixClient
from autoclipper.ingest.vod import (
    IngestReport,
    IngestTools,
    chat_path,
    clips_dir,
    ingest_vod,
    raw_chat_path,
    vods_path,
)

FIXTURES = Path(__file__).parent / "fixtures"
VIDEO_ID = "9000000100"  # the chat fixture's VOD
BROADCASTER_ID = "9000000000"
SALT = SecretStr("fake-salt-5b1e")
VOD_CREATED = datetime(2026, 10, 1, 18, 0, 0, tzinfo=UTC)
NOW = datetime(2026, 10, 10, 12, 0, 0, tzinfo=UTC)
BANNER = f"TwitchDownloaderCLI {PINNED_TOOL_VERSION} Copyright (c) lay295 and contributors\n"
# Names in the chat fixture; none of them may reach the data directory.
CHATTER_NAMES = ("user_001", "User_001", "user_002", "User_002")


def video_record(created_at: datetime = VOD_CREATED, duration: str = "1h0m0s") -> dict[str, object]:
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
        "type": "archive",
        "duration": duration,
        "muted_segments": [{"duration": 30, "offset": 120}],
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


class FakeTwitch:
    """Answers the token, Get Videos and Get Clips requests; can be told to fail."""

    def __init__(self, *, video: dict[str, object] | None = None, clips_status: int = 200) -> None:
        self.video = video if video is not None else video_record()
        self.clips_status = clips_status
        self.paths: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "id.twitch.tv":
            return httpx.Response(
                200, json={"access_token": "fake-token", "expires_in": 3600, "token_type": "bearer"}
            )
        self.paths.append(request.url.path)
        if request.url.path == "/helix/videos":
            if not self.video:
                return httpx.Response(404, json={"error": "Not Found", "status": 404})
            return httpx.Response(200, json={"data": [self.video], "pagination": {}})
        assert request.url.path == "/helix/clips"
        if self.clips_status != 200:
            return httpx.Response(self.clips_status, json={"error": "Bad Request", "status": 400})
        clips = [clip_record(i, VOD_CREATED + timedelta(minutes=i)) for i in (1, 2)]
        return httpx.Response(200, json={"data": clips, "pagination": {}})


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

    def __call__(self, args: Sequence[str]) -> CommandResult:
        self.calls += 1
        Path(args[list(args).index("--output") + 1]).write_bytes(self.payload)
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
        shutil.copyfile(self.source, path)
        return DownloadedMedia(path, 3.0)


@pytest.fixture
def chat_binary(tmp_path: Path) -> Path:
    # shutil.which finds only executables with a PATHEXT extension on Windows.
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
    clock: Callable[[], datetime] = lambda: NOW,
) -> IngestTools:
    return IngestTools(
        helix=helix_for(twitch or FakeTwitch()),
        salt=salt,
        chat_runner=chat or FakeChatTool(),
        chat_binary=chat_binary,
        downloader=downloader,
        clock=clock,
    )


def outcomes(report: IngestReport) -> dict[str, str]:
    return {step.step: step.outcome for step in report.steps}


def files_under(directory: Path) -> list[Path]:
    return sorted(p for p in directory.rglob("*") if p.is_file())


# --- A full run --------------------------------------------------------------------------------


@pytest.mark.ffmpeg
def test_a_run_saves_metadata_clips_chat_and_audio(
    tmp_path: Path, chat_binary: Path, audio_source: Path
) -> None:
    data = tmp_path / "data"

    report = ingest_vod(
        VIDEO_ID, data_dir=data, tools=tools(chat_binary, downloader=FakeDownloader(audio_source))
    )

    assert outcomes(report) == {"vod_meta": "ok", "clips": "ok", "chat": "ok", "audio": "ok"}
    vods = pl.read_parquet(vods_path(data))
    assert vods["video_id"].to_list() == [VIDEO_ID]
    assert vods["muted_segments"].to_list() == [[{"offset_s": 120.0, "duration_s": 30.0}]]
    (snapshot,) = clips_dir(data).glob("*.parquet")
    assert snapshot.name == f"{VIDEO_ID}-20261010T120000Z.parquet"
    clips = pl.read_parquet(snapshot)
    assert sorted(clips["clip_id"].to_list()) == ["SyntheticClip001", "SyntheticClip002"]
    assert clips["video_id"].dtype == pl.String
    manifest = json.loads(snapshot.with_suffix(".json").read_text(encoding="utf-8"))
    assert manifest["snapshot_id"] == snapshot.stem
    assert manifest["capture_started_at"] == "2026-10-10T12:00:00Z"
    assert manifest["n_clips"] == 2
    assert manifest["complete"] is True
    assert manifest["mature"] is True  # 8.75 days old
    chat = pl.read_parquet(chat_path(data, VIDEO_ID))
    assert len(chat) > 0
    assert set(chat["video_id"]) == {VIDEO_ID}
    assert not raw_chat_path(data, VIDEO_ID).exists()
    assert normalized_audio_path(data, VIDEO_ID).is_file()
    catalog = read_catalog(data)
    assert {key: entry.status for key, entry in catalog.items()} == {
        ("audio", VIDEO_ID): "ok",
        ("chat", VIDEO_ID): "ok",
        ("clips", VIDEO_ID): "ok",
        ("vod_meta", VIDEO_ID): "ok",
    }
    assert catalog[("chat", VIDEO_ID)].tool == f"TwitchDownloaderCLI@{PINNED_TOOL_VERSION}"
    assert catalog[("audio", VIDEO_ID)].tool.startswith("yt-dlp@")
    assert all(entry.error is None for entry in catalog.values())


@pytest.mark.ffmpeg
def test_no_chatter_username_is_written_to_disk(
    tmp_path: Path, chat_binary: Path, audio_source: Path
) -> None:
    data = tmp_path / "data"

    ingest_vod(
        VIDEO_ID, data_dir=data, tools=tools(chat_binary, downloader=FakeDownloader(audio_source))
    )

    for path in files_under(data):
        if path.suffix == ".wav":
            continue
        content = path.read_bytes()
        if path.suffix == ".parquet":  # compare decoded values, not compressed bytes
            content = pl.read_parquet(path).write_ndjson().encode()
        for name in CHATTER_NAMES:
            assert name.encode() not in content, f"{name} in {path}"


def test_a_failed_chat_parse_leaves_no_raw_file(tmp_path: Path, chat_binary: Path) -> None:
    data = tmp_path / "data"
    broken = FakeChatTool(payload=b'{"comments": [{"commenter": {"name": "user_001"}}]}')

    report = ingest_vod(
        VIDEO_ID,
        data_dir=data,
        tools=tools(
            chat_binary, chat=broken, downloader=FakeDownloader(None, RuntimeError("offline"))
        ),
    )

    assert outcomes(report)["chat"] == "failed"
    assert not raw_chat_path(data, VIDEO_ID).exists()
    assert not chat_path(data, VIDEO_ID).exists()
    assert "ChatDownloadError" in (read_catalog(data)[("chat", VIDEO_ID)].error or "")


# --- Re-runs -----------------------------------------------------------------------------------


@pytest.mark.ffmpeg
def test_a_rerun_skips_finished_steps_and_retries_failed_ones(
    tmp_path: Path, chat_binary: Path, audio_source: Path
) -> None:
    data = tmp_path / "data"
    twitch = FakeTwitch()
    chat = FakeChatTool()
    offline = FakeDownloader(None, RuntimeError("network down"))

    first = ingest_vod(
        VIDEO_ID,
        data_dir=data,
        tools=tools(chat_binary, twitch=twitch, chat=chat, downloader=offline),
    )
    calls_after_first = list(twitch.paths)
    assert "network down" in (read_catalog(data)[("audio", VIDEO_ID)].error or "")
    downloader = FakeDownloader(audio_source)
    later = lambda: NOW + timedelta(hours=1)  # noqa: E731
    second = ingest_vod(
        VIDEO_ID,
        data_dir=data,
        tools=tools(chat_binary, twitch=twitch, chat=chat, downloader=downloader, clock=later),
    )

    assert outcomes(first) == {"vod_meta": "ok", "clips": "ok", "chat": "ok", "audio": "failed"}
    assert outcomes(second) == {
        "vod_meta": "skipped",
        "clips": "skipped",
        "chat": "skipped",
        "audio": "ok",
    }
    assert twitch.paths == calls_after_first  # no new Helix calls: one snapshot, never retaken
    assert chat.calls == 1
    assert downloader.calls == 1
    assert len(list(clips_dir(data).glob("*.parquet"))) == 1
    assert read_catalog(data)[("audio", VIDEO_ID)].status == "ok"


def test_a_removed_output_is_fetched_again(tmp_path: Path, chat_binary: Path) -> None:
    data = tmp_path / "data"
    chat = FakeChatTool()
    no_audio = FakeDownloader(None, RuntimeError("offline"))
    ingest_vod(VIDEO_ID, data_dir=data, tools=tools(chat_binary, chat=chat, downloader=no_audio))
    chat_path(data, VIDEO_ID).unlink()

    report = ingest_vod(
        VIDEO_ID, data_dir=data, tools=tools(chat_binary, chat=chat, downloader=no_audio)
    )

    assert outcomes(report)["chat"] == "ok"
    assert chat.calls == 2
    assert chat_path(data, VIDEO_ID).is_file()


# --- Failures are recorded, not fatal -----------------------------------------------------------


def test_a_deleted_vod_is_unavailable_everywhere_and_not_retried(
    tmp_path: Path, chat_binary: Path
) -> None:
    data = tmp_path / "data"
    twitch = FakeTwitch(video={})
    chat = FakeChatTool()
    downloader = FakeDownloader(None, AssertionError("must not download"))

    report = ingest_vod(
        VIDEO_ID,
        data_dir=data,
        tools=tools(chat_binary, twitch=twitch, chat=chat, downloader=downloader),
    )
    again = ingest_vod(
        VIDEO_ID,
        data_dir=data,
        tools=tools(chat_binary, twitch=twitch, chat=chat, downloader=downloader),
    )

    assert set(outcomes(report).values()) == {"unavailable"}
    assert not report.failed  # nothing to retry: the command exits 0
    assert set(outcomes(again).values()) == {"skipped"}
    assert twitch.paths.count("/helix/videos") == 1
    assert (chat.calls, downloader.calls) == (0, 0)


@pytest.mark.ffmpeg
def test_a_local_file_recovers_audio_twitch_refused(
    tmp_path: Path, chat_binary: Path, make_test_media: Callable[..., Path]
) -> None:
    data = tmp_path / "data"
    short = FakeTwitch(video=video_record(duration="0h0m2s"))
    restricted = FakeDownloader(None, VodAccessRestrictedError(VIDEO_ID, "subscriber-only"))
    ingest_vod(
        VIDEO_ID, data_dir=data, tools=tools(chat_binary, twitch=short, downloader=restricted)
    )
    video = make_test_media("my copy.mp4", duration_s=2.0)

    report = ingest_vod(
        VIDEO_ID,
        data_dir=data,
        tools=tools(chat_binary, twitch=short, downloader=restricted),
        media_file=video,
    )

    assert outcomes(report)["audio"] == "ok"
    assert read_catalog(data)[("audio", VIDEO_ID)].tool == "local-file"


@pytest.mark.ffmpeg
def test_a_local_file_of_the_wrong_length_is_refused(
    tmp_path: Path, chat_binary: Path, make_test_media: Callable[..., Path]
) -> None:
    data = tmp_path / "data"
    clip = make_test_media("a clip.mp4", duration_s=2.0)  # the VOD is an hour long

    report = ingest_vod(
        VIDEO_ID,
        data_dir=data,
        tools=tools(chat_binary, downloader=FakeDownloader(None, AssertionError("no"))),
        media_file=clip,
    )

    assert outcomes(report)["audio"] == "failed"
    assert "MediaLengthError" in (read_catalog(data)[("audio", VIDEO_ID)].error or "")
    assert not normalized_audio_path(data, VIDEO_ID).exists()


def test_an_interrupted_chat_download_leaves_no_raw_file(tmp_path: Path, chat_binary: Path) -> None:
    data = tmp_path / "data"

    class Interrupted(FakeChatTool):
        def __call__(self, args: Sequence[str]) -> CommandResult:
            super().__call__(args)  # the raw file, with usernames, is on disk now
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        ingest_vod(VIDEO_ID, data_dir=data, tools=tools(chat_binary, chat=Interrupted()))

    assert not raw_chat_path(data, VIDEO_ID).exists()


def test_a_leftover_raw_chat_file_is_removed_even_when_chat_is_done(
    tmp_path: Path, chat_binary: Path
) -> None:
    data = tmp_path / "data"
    offline = FakeDownloader(None, RuntimeError("offline"))
    ingest_vod(VIDEO_ID, data_dir=data, tools=tools(chat_binary, downloader=offline))
    raw = raw_chat_path(data, VIDEO_ID)
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text('{"commenter": "user_001"}', encoding="utf-8")  # e.g. from a killed run

    report = ingest_vod(VIDEO_ID, data_dir=data, tools=tools(chat_binary, downloader=offline))

    assert outcomes(report)["chat"] == "skipped"
    assert not raw.exists()


def test_a_failed_manifest_write_leaves_no_unpinned_snapshot(
    tmp_path: Path, chat_binary: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tmp_path / "data"
    real_write_text = Path.write_text

    def failing_write_text(self: Path, *args: object, **kwargs: object) -> int:
        if self.name.endswith(".json.part"):
            raise OSError(28, "No space left on device")
        return real_write_text(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "write_text", failing_write_text)
    offline = FakeDownloader(None, RuntimeError("offline"))

    report = ingest_vod(VIDEO_ID, data_dir=data, tools=tools(chat_binary, downloader=offline))

    assert outcomes(report)["clips"] == "failed"
    assert list(clips_dir(data).iterdir()) == []


def test_a_subscriber_only_vod_has_unavailable_audio(tmp_path: Path, chat_binary: Path) -> None:
    data = tmp_path / "data"
    restricted = FakeDownloader(None, VodAccessRestrictedError(VIDEO_ID, "subscriber-only"))

    report = ingest_vod(VIDEO_ID, data_dir=data, tools=tools(chat_binary, downloader=restricted))

    assert outcomes(report)["audio"] == "unavailable"
    assert read_catalog(data)[("audio", VIDEO_ID)].status == "unavailable"


def test_failed_clips_and_missing_settings_are_recorded(tmp_path: Path, chat_binary: Path) -> None:
    data = tmp_path / "data"
    twitch = FakeTwitch(clips_status=400)
    offline = FakeDownloader(None, RuntimeError("offline"))

    report = ingest_vod(
        VIDEO_ID, data_dir=data, tools=tools(chat_binary, twitch=twitch, downloader=offline)
    )
    unsalted = ingest_vod(
        VIDEO_ID,
        data_dir=tmp_path / "other",
        tools=tools(chat_binary, downloader=offline, salt=None),
    )

    assert outcomes(report)["clips"] == "failed"
    assert report.failed
    assert outcomes(unsalted)["vod_meta"] == "ok"
    assert outcomes(unsalted)["clips"] == "failed"
    assert outcomes(unsalted)["chat"] == "failed"
    errors = read_catalog(tmp_path / "other")
    assert "AUTOCLIPPER_CHAT_HASH_SALT" in (errors[("chat", VIDEO_ID)].error or "")


def test_a_young_vod_is_flagged_in_the_manifest(tmp_path: Path, chat_binary: Path) -> None:
    data = tmp_path / "data"
    young = FakeTwitch(video=video_record(created_at=NOW - timedelta(days=2)))

    report = ingest_vod(
        VIDEO_ID,
        data_dir=data,
        tools=tools(chat_binary, twitch=young, downloader=FakeDownloader(None, RuntimeError("x"))),
    )

    (manifest,) = clips_dir(data).glob("*.json")
    assert json.loads(manifest.read_text(encoding="utf-8"))["mature"] is False
    clips_step = next(step for step in report.steps if step.step == "clips")
    assert "flagged" in clips_step.detail


@pytest.mark.ffmpeg
def test_cached_local_audio_is_length_checked_once_the_vod_length_is_known(
    tmp_path: Path, chat_binary: Path, make_test_media: Callable[..., Path]
) -> None:
    data = tmp_path / "data"
    six_seconds = make_test_media("six seconds.mp4", duration_s=6.0)
    no_helix = IngestTools(
        salt=SALT, chat_runner=FakeChatTool(), chat_binary=chat_binary, clock=lambda: NOW
    )
    # Run 1: metadata fails, so the local file's audio can't be checked against the VOD yet.
    first = ingest_vod(VIDEO_ID, data_dir=data, tools=no_helix, media_file=six_seconds)
    assert outcomes(first)["audio"] == "ok"

    # Run 2: the metadata says the VOD lasts a minute.
    one_minute = FakeTwitch(video=video_record(duration="0h1m0s"))
    no_download = FakeDownloader(None, AssertionError("must not download"))
    second = ingest_vod(
        VIDEO_ID,
        data_dir=data,
        tools=tools(chat_binary, twitch=one_minute, downloader=no_download),
        media_file=six_seconds,
    )

    assert outcomes(second)["vod_meta"] == "ok"
    assert outcomes(second)["audio"] == "failed"
    entry = read_catalog(data)[("audio", VIDEO_ID)]
    assert entry.status == "failed"
    assert "MediaLengthError" in (entry.error or "")
    assert "60.0 s" in (entry.error or "")
    assert not normalized_audio_path(data, VIDEO_ID).exists()
    # Without --media, the next run doesn't keep it either: it downloads instead.
    third = ingest_vod(
        VIDEO_ID,
        data_dir=data,
        tools=tools(chat_binary, twitch=one_minute, downloader=no_download),
    )
    assert outcomes(third)["audio"] == "failed"
    assert no_download.calls == 1


@pytest.mark.ffmpeg
def test_unreadable_cached_local_audio_is_recorded_not_fatal(
    tmp_path: Path, chat_binary: Path, make_test_media: Callable[..., Path]
) -> None:
    data = tmp_path / "data"
    two_seconds = make_test_media("two seconds.mp4", duration_s=2.0)
    no_helix = IngestTools(
        salt=SALT, chat_runner=FakeChatTool(), chat_binary=chat_binary, clock=lambda: NOW
    )
    ingest_vod(VIDEO_ID, data_dir=data, tools=no_helix, media_file=two_seconds)
    normalized_audio_path(data, VIDEO_ID).write_bytes(b"not a wav")  # e.g. damaged on disk

    second = ingest_vod(
        VIDEO_ID,
        data_dir=data,
        tools=tools(chat_binary, twitch=FakeTwitch(video=video_record(duration="0h0m2s"))),
    )

    assert outcomes(second)["audio"] == "failed"
    assert read_catalog(data)[("audio", VIDEO_ID)].status == "failed"
    assert not normalized_audio_path(data, VIDEO_ID).exists()


@pytest.mark.ffmpeg
def test_cached_local_audio_of_the_right_length_is_kept(
    tmp_path: Path, chat_binary: Path, make_test_media: Callable[..., Path]
) -> None:
    data = tmp_path / "data"
    two_seconds = make_test_media("two seconds.mp4", duration_s=2.0)
    no_helix = IngestTools(
        salt=SALT, chat_runner=FakeChatTool(), chat_binary=chat_binary, clock=lambda: NOW
    )
    ingest_vod(VIDEO_ID, data_dir=data, tools=no_helix, media_file=two_seconds)

    second = ingest_vod(
        VIDEO_ID,
        data_dir=data,
        tools=tools(chat_binary, twitch=FakeTwitch(video=video_record(duration="0h0m2s"))),
        media_file=two_seconds,
    )

    assert outcomes(second)["audio"] == "skipped"
    assert normalized_audio_path(data, VIDEO_ID).is_file()


@pytest.mark.ffmpeg
def test_a_local_media_file_is_used_instead_of_downloading(
    tmp_path: Path, chat_binary: Path, make_test_media: Callable[..., Path]
) -> None:
    data = tmp_path / "data"
    video = make_test_media("my vod.mp4", duration_s=2.0)
    downloader = FakeDownloader(None, AssertionError("must not download"))
    two_seconds = FakeTwitch(video=video_record(duration="0h0m2s"))

    report = ingest_vod(
        VIDEO_ID,
        data_dir=data,
        tools=tools(chat_binary, twitch=two_seconds, downloader=downloader),
        media_file=video,
    )

    assert outcomes(report)["audio"] == "ok"
    assert downloader.calls == 0
    assert read_catalog(data)[("audio", VIDEO_ID)].tool == "local-file"
    assert normalized_audio_path(data, VIDEO_ID).is_file()


def test_the_catalog_keeps_one_row_per_object(tmp_path: Path, chat_binary: Path) -> None:
    data = tmp_path / "data"
    offline = FakeDownloader(None, RuntimeError("offline"))

    for _ in range(3):
        ingest_vod(VIDEO_ID, data_dir=data, tools=tools(chat_binary, downloader=offline))

    table = pl.read_parquet(catalog_path(data))
    assert len(table) == 4
    assert table.filter(pl.col("object_type") == "audio")["status"].to_list() == ["failed"]


# --- CLI ---------------------------------------------------------------------------------------


runner = CliRunner()


def test_the_cli_rejects_a_bad_link_and_a_missing_media_file(tmp_path: Path) -> None:
    bad = runner.invoke(app, ["ingest", "twitch.tv/someone"])
    missing = runner.invoke(app, ["ingest", VIDEO_ID, "--media", str(tmp_path / "nope.mp4")])

    assert bad.exit_code == ExitCode.USER_ERROR
    assert missing.exit_code == ExitCode.USER_ERROR
    assert "media file not found" in missing.output


def test_the_cli_reports_steps_and_fails_without_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTOCLIPPER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("PATH", str(tmp_path))  # no TwitchDownloaderCLI, no yt-dlp network

    def no_network(*_: object, **__: object) -> object:
        raise RuntimeError("no network in tests")

    monkeypatch.setattr("autoclipper.ingest.vod.prepare_vod_audio", no_network)

    result = runner.invoke(app, ["ingest", f"https://www.twitch.tv/videos/{VIDEO_ID}"])

    assert result.exit_code == ExitCode.EXTERNAL_FAILURE
    assert "vod_meta: failed" in result.output
    assert "TWITCH_CLIENT_ID" in result.output
    assert "run the same command again" in result.output
    assert read_catalog(tmp_path / "data")[("chat", VIDEO_ID)].status == "failed"


# --- Live --------------------------------------------------------------------------------------


@pytest.mark.live
def test_ingest_a_real_vod(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Ingest a real VOD into a temporary data dir.

    Needs Twitch credentials, the salt, TwitchDownloaderCLI, FFmpeg and network. Set
    AUTOCLIPPER_TEST_VOD to a public VOD ID in the environment (not in .env, which accepts only
    settings): ``AUTOCLIPPER_TEST_VOD=<id> uv run pytest -m live tests/ingest/test_vod.py``.
    """
    video_id = os.environ.get("AUTOCLIPPER_TEST_VOD")
    if not video_id:
        pytest.skip("set AUTOCLIPPER_TEST_VOD to a public Twitch VOD ID")
    monkeypatch.setenv("AUTOCLIPPER_DATA_DIR", str(tmp_path / "data"))

    result = runner.invoke(app, ["ingest", video_id])

    assert result.exit_code == ExitCode.SUCCESS, result.output
    catalog = read_catalog(tmp_path / "data")
    assert {entry.status for entry in catalog.values()} <= {"ok", "unavailable"}
