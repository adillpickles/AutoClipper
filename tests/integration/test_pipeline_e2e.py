"""End-to-end integration test on a synthetic VOD (#48).

Runs the entire pipeline without network calls or real Twitch data:
synthetic VOD -> mocked ingest (Helix + TwitchDownloaderCLI + local video) -> windows ->
chat & audio features -> scoring -> candidate generation -> 9:16 vertical clip export of the
top candidate -> 16:9 highlights video of the top 3 candidates.
"""

import json
import sys
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import polars as pl
import pytest
from pydantic import SecretStr

from autoclipper.analysis import AnalysisResult
from autoclipper.analyze import analyze_vod, result_path
from autoclipper.config import TwitchCredentials
from autoclipper.export.clip import ClipSpec, render_clip
from autoclipper.export.cut import Interval
from autoclipper.export.highlights import (
    HighlightsRequest,
    render_highlights,
    resolve_segments,
)
from autoclipper.features.chat import load_lexicon
from autoclipper.ffmpeg import probe, run_command
from autoclipper.ingest.chat import PINNED_TOOL_VERSION, CommandResult
from autoclipper.ingest.twitch_api import HelixClient
from autoclipper.ingest.vod import IngestTools
from autoclipper.synthetic import VideoOptions, generate_dataset
from autoclipper.synthetic.media import video_command

LEXICON_PATH = Path(__file__).parents[2] / "configs" / "lexicon.yaml"
BANNER = f"TwitchDownloaderCLI {PINNED_TOOL_VERSION} Copyright (c) lay295 and contributors\n"


def _helix_duration(seconds: float) -> str:
    """Format whole seconds as Twitch's Helix duration string (e.g. '4m18s')."""
    total = round(seconds)
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    parts: list[str] = []
    if hours:
        parts.append(f"{hours}h")
    if minutes or hours:
        parts.append(f"{minutes}m")
    parts.append(f"{secs}s")
    return "".join(parts)


def _iso_z(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _build_helix_client(
    vod_row: dict[str, object],
    broadcaster_row: dict[str, object],
    clip_rows: list[dict[str, object]],
) -> HelixClient:
    """Mock Helix API serving the synthetic VOD, broadcaster, and viewer clips."""
    created_at = vod_row["created_at"]
    assert isinstance(created_at, datetime)
    duration_s = float(vod_row["duration_s"])  # type: ignore[arg-type]
    video_id = str(vod_row["video_id"])
    broadcaster_id = str(broadcaster_row["broadcaster_id"])
    login = str(broadcaster_row["login"])
    display_name = str(broadcaster_row["display_name"])

    video_payload = {
        "id": video_id,
        "stream_id": vod_row["stream_id"],
        "user_id": broadcaster_id,
        "user_login": login,
        "user_name": display_name,
        "title": str(vod_row["title"]),
        "description": "",
        "created_at": _iso_z(created_at),
        "published_at": _iso_z(created_at),
        "url": f"https://www.twitch.tv/videos/{video_id}",
        "thumbnail_url": "",
        "viewable": "public",
        "view_count": int(vod_row["view_count"]),  # type: ignore[arg-type]
        "language": str(vod_row["language"]),
        "type": "archive",
        "duration": _helix_duration(duration_s),
        "muted_segments": None,
    }
    user_payload = {
        "id": broadcaster_id,
        "login": login,
        "display_name": display_name,
        "broadcaster_type": str(broadcaster_row["broadcaster_type"]),
        "created_at": "2020-01-01T00:00:00Z",
    }
    clips_payload = [
        {
            "id": str(row["clip_id"]),
            "broadcaster_id": str(row["broadcaster_id"]),
            "creator_id": f"990000{index:04d}",
            "video_id": "" if row["video_id"] is None else str(row["video_id"]),
            "game_id": "" if row["game_id"] is None else str(row["game_id"]),
            "language": str(row["language"]),
            "title": str(row["title"]),
            "view_count": int(row["view_count"]),  # type: ignore[arg-type]
            "created_at": _iso_z(row["created_at"]),  # type: ignore[arg-type]
            "duration": float(row["duration_s"]),  # type: ignore[arg-type]
            "vod_offset": (
                None if row["vod_offset_s"] is None else round(float(row["vod_offset_s"]))  # type: ignore[arg-type]
            ),
            "is_featured": bool(row["is_featured"]),
        }
        for index, row in enumerate(clip_rows)
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth2/token"):
            return httpx.Response(
                200,
                json={"access_token": "synth-token", "expires_in": 3600, "token_type": "bearer"},
            )
        if request.url.path.endswith("/videos"):
            ids = request.url.params.get_list("id")
            data = [video_payload] if video_id in ids else []
            return httpx.Response(200, json={"data": data, "pagination": {}})
        if request.url.path.endswith("/users"):
            ids = request.url.params.get_list("id")
            data = [user_payload] if broadcaster_id in ids else []
            return httpx.Response(200, json={"data": data})
        if request.url.path.endswith("/clips"):
            started = request.url.params.get("started_at")
            ended = request.url.params.get("ended_at")
            matching = [
                clip
                for clip in clips_payload
                if (started is None or clip["created_at"] >= started)
                and (ended is None or clip["created_at"] <= ended)
            ]
            return httpx.Response(200, json={"data": matching, "pagination": {}})
        return httpx.Response(404, json={"message": f"unexpected path {request.url.path}"})

    return HelixClient(
        TwitchCredentials(client_id=SecretStr("synth-id"), client_secret=SecretStr("synth-secret")),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def _chat_replay_json(
    chat_df: pl.DataFrame,
    *,
    video_id: str,
    broadcaster_id: str,
    login: str,
    display_name: str,
    title: str,
    created_at: datetime,
    duration_s: float,
) -> dict[str, object]:
    """Convert synthetic chat rows into TwitchDownloaderCLI 1.4.0 JSON format."""
    comments: list[dict[str, object]] = []
    # Map each unique synthetic user_hash to a stable fake Twitch numeric ID & login.
    user_map: dict[str, int] = {}
    for index, row in enumerate(chat_df.iter_rows(named=True)):
        user_hash = str(row["user_hash"])
        if user_hash not in user_map:
            user_map[user_hash] = len(user_map) + 1
        user_num = user_map[user_hash]
        offset_s = float(row["offset_s"])
        text = str(row["text"])
        emote_ids: list[str] = list(row["emote_ids"] or [])
        fragments: list[dict[str, object]] = [{"text": text, "emoticon": None}]
        for emote_id in emote_ids:
            fragments.append({"text": "Kappa", "emoticon": {"emoticon_id": str(emote_id)}})
        badges: list[dict[str, str]] = []
        if row["is_subscriber"]:
            badges.append({"_id": "subscriber", "version": "1"})
        if row["is_moderator"]:
            badges.append({"_id": "moderator", "version": "1"})
        comments.append(
            {
                "_id": f"00000000-0000-4000-8000-{index:012d}",
                "created_at": _iso_z(created_at + timedelta(seconds=offset_s)),
                "channel_id": broadcaster_id,
                "content_type": "video",
                "content_id": video_id,
                "content_offset_seconds": offset_s,
                "commenter": {
                    "display_name": f"Viewer_{user_num:03d}",
                    "_id": f"9300{user_num:06d}",
                    "name": f"viewer_{user_num:03d}",
                },
                "message": {
                    "body": text,
                    "bits_spent": int(row["bits"]),
                    "fragments": fragments,
                    "user_badges": badges,
                },
            }
        )
    return {
        "FileInfo": {"Version": {"Major": 1, "Minor": 4, "Patch": 0}},
        "streamer": {"name": display_name, "login": login, "id": int(broadcaster_id)},
        "video": {
            "title": title,
            "id": video_id,
            "created_at": _iso_z(created_at),
            "start": 0,
            "end": round(duration_s),
            "length": round(duration_s),
        },
        "comments": comments,
    }


@pytest.mark.ffmpeg
def test_synthetic_vod_end_to_end_pipeline(tmp_path: Path) -> None:
    """Synthetic VOD -> mocked ingest -> features -> candidates -> 9:16 export + 16:9 highlights."""
    synth_dir = tmp_path / "synthetic"
    generate_dataset(synth_dir, seed=7, video=None)
    manifest = json.loads((synth_dir / "manifest.json").read_text(encoding="utf-8"))

    # Use VOD 0 (3 planted highlights with overlapping/near-touch/disjoint clips + chat/audio).
    vod_manifest = manifest["vods"][0]
    video_id = str(vod_manifest["video_id"])
    duration_s = float(vod_manifest["duration_s"])
    planted_events = vod_manifest["planted_events"]

    vods_df = pl.read_parquet(synth_dir / "processed" / "vods.parquet")
    vod_row = vods_df.filter(pl.col("video_id") == video_id).to_dicts()[0]
    broadcasters_df = pl.read_parquet(synth_dir / "processed" / "broadcasters.parquet")
    broadcaster_row = broadcasters_df.filter(
        pl.col("broadcaster_id") == vod_row["broadcaster_id"]
    ).to_dicts()[0]
    clips_parquet = next((synth_dir / "processed" / "clips").glob("*.parquet"))
    clip_rows = pl.read_parquet(clips_parquet).filter(pl.col("video_id") == video_id).to_dicts()
    chat_df = pl.read_parquet(synth_dir / "interim" / "chat" / f"{video_id}.parquet")

    # Render one synthetic 16:9 test-pattern MP4 carrying the VOD's synthesized audio.
    vod_wav = synth_dir / "interim" / "audio" / f"{video_id}.wav"
    vod_video = tmp_path / f"{video_id}.mp4"
    run_command(
        video_command(vod_wav, vod_video, duration_s, VideoOptions(size=(320, 180), frame_rate=10)),
        timeout_s=120,
    )

    # Prepare mocked ingest tools (fake Helix + fake TwitchDownloaderCLI writing chat JSON).
    chat_payload = _chat_replay_json(
        chat_df,
        video_id=video_id,
        broadcaster_id=str(broadcaster_row["broadcaster_id"]),
        login=str(broadcaster_row["login"]),
        display_name=str(broadcaster_row["display_name"]),
        title=str(vod_row["title"]),
        created_at=vod_row["created_at"],
        duration_s=duration_s,
    )
    bin_name = "TwitchDownloaderCLI.exe" if sys.platform == "win32" else "TwitchDownloaderCLI"
    chat_binary = tmp_path / "bin" / bin_name
    chat_binary.parent.mkdir(parents=True, exist_ok=True)
    chat_binary.write_bytes(b"")
    chat_binary.chmod(0o755)

    def chat_runner(args: Sequence[str], /, *, timeout_s: float | None) -> CommandResult:
        out_path = Path(args[list(args).index("--output") + 1])
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(chat_payload), encoding="utf-8")
        return CommandResult(returncode=0, stdout=BANNER, stderr="")

    now = vod_row["created_at"] + timedelta(days=8)
    data_dir = tmp_path / "data"
    artifacts_dir = tmp_path / "artifacts"
    helix = _build_helix_client(vod_row, broadcaster_row, clip_rows)
    try:
        tools = IngestTools(
            helix=helix,
            salt=SecretStr("integration-test-salt"),
            chat_binary=chat_binary,
            chat_runner=chat_runner,
            clock=lambda: now,
        )
        result = analyze_vod(
            video_id,
            data_dir=data_dir,
            artifacts_dir=artifacts_dir,
            tools=tools,
            lexicon=load_lexicon(LEXICON_PATH),
            video_file=vod_video,
            clock=lambda: now,
        )
    finally:
        helix.close()

    # 1. Analysis result is saved, clean, and ranks the strongest planted highlight near the top.
    saved = AnalysisResult.model_validate_json(
        result_path(artifacts_dir, video_id).read_text(encoding="utf-8")
    )
    assert saved == result
    warning_codes = {w.code for w in result.warnings}
    assert warning_codes.isdisjoint({"chat_missing", "audio_missing", "viewer_clips_missing"})
    assert len(result.candidates) >= 3

    strongest = max(planted_events, key=lambda event: float(event["strength"]))
    strongest_t = float(strongest["time_s"])
    top_three = result.candidates[:3]
    assert any(c.start_s <= strongest_t <= c.end_s for c in top_three), (
        f"strongest planted highlight at {strongest_t:.1f}s missed top 3: "
        f"{[(c.rank, c.start_s, c.end_s, c.score) for c in top_three]}"
    )

    # 2. Vertical 9:16 export of the top candidate is a valid 1080x1920 H.264/AAC MP4.
    top = result.candidates[0]
    export_out = artifacts_dir / "exports" / f"{video_id}_top1.mp4"
    rendered_clip = render_clip(
        ClipSpec(
            source=vod_video,
            interval=Interval(top.start_s, top.end_s),
            output=export_out,
            layout="center_crop",
            captions=False,
        )
    )
    assert rendered_clip.output == export_out
    clip_info = probe(export_out)
    assert clip_info.video_streams[0].codec_name == "h264"
    assert (clip_info.video_streams[0].width, clip_info.video_streams[0].height) == (1080, 1920)
    assert clip_info.video_streams[0].pix_fmt == "yuv420p"
    assert clip_info.audio_streams[0].codec_name == "aac"
    assert clip_info.duration_s == pytest.approx(top.end_s - top.start_s, abs=0.2)

    # 3. 16:9 highlights video of the top 3 candidates is a valid 1920x1080 H.264/AAC MP4.
    segments = resolve_segments(result, count=3, segments=None, order="chronological")
    highlights_out = artifacts_dir / "highlights" / f"autoclipper_{video_id}_highlights.mp4"
    rendered_highlights = render_highlights(
        HighlightsRequest(
            source=vod_video,
            segments=segments,
            output=highlights_out,
            order="chronological",
            captions=False,
        )
    )
    assert rendered_highlights.output == highlights_out
    hl_info = probe(highlights_out)
    assert hl_info.video_streams[0].codec_name == "h264"
    assert (hl_info.video_streams[0].width, hl_info.video_streams[0].height) == (1920, 1080)
    assert hl_info.video_streams[0].pix_fmt == "yuv420p"
    assert hl_info.audio_streams[0].codec_name == "aac"
    expected_hl_duration = sum(seg.end_s - seg.start_s for seg in segments)
    assert hl_info.duration_s == pytest.approx(expected_hl_duration, abs=0.4)
