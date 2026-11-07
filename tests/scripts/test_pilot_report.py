"""Tests for scripts/pilot_report.py: measurements, checks, storage and the document blocks.

Every test builds a small fake pilot data directory in ``tmp_path`` (selections, list, catalog,
tables, clip snapshots, chat, a few seconds of WAV, run records). Nothing here contacts Twitch;
real network connections are blocked.
"""

import hashlib
import json
import os
import socket
import subprocess
import sys
import wave
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pilot_report as pr
import polars as pl
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import select_pilot_vods as sp
from typer.testing import CliRunner, Result

from autoclipper import tables
from autoclipper.cli import ExitCode
from autoclipper.ingest.catalog import CatalogEntry, record
from autoclipper.ingest.media import vod_url
from autoclipper.ingest.twitch_api import Broadcaster, Listing, Vod
from autoclipper.ingest.vod import STEPS

NOW = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
A_ID, B_ID, OLD_ID = "9000000001", "9000000002", "9000000003"
HASH = "ab" * 32


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail any test that opens a real network connection."""

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError(f"network access attempted: {args!r}")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


# --- Pure measurements -------------------------------------------------------------------------


def test_densest_window_is_half_open() -> None:
    assert pr.densest_window([], 10) == 0
    assert pr.densest_window([0, 5, 9.9, 10, 30], 10) == 3  # 10 is outside [0, 10)
    assert pr.densest_window([30, 0, 10, 15, 19], 10) == 3  # unsorted input: 10, 15, 19


def test_nearest_rank() -> None:
    values = [5, 1, 4, 2, 3]
    assert pr.nearest_rank(values, 0.5) == 3
    assert pr.nearest_rank(values, 0.9) == 5
    assert pr.nearest_rank(values, 0.2) == 1  # 0.2 * 5 = 1.0 exactly: the first value
    assert pr.nearest_rank(list(range(1, 101)), 0.99) == 99


def clip_frame(rows: list[dict[str, Any]]) -> pl.DataFrame:
    defaults = {
        "video_id": "100",
        "broadcaster_id": A_ID,
        "creator_hash": HASH,
        "title": "t",
        "game_id": "509658",
        "language": "en",
        "duration_s": 30.0,
        "view_count": 1,
        "is_featured": False,
        "fetched_at": NOW,
    }
    full = [
        {**defaults, "clip_id": f"c{n}", "created_at": NOW - timedelta(days=9), **row}
        for n, row in enumerate(rows)
    ]
    return pl.from_arrow(pa.Table.from_pylist(full, schema=tables.CLIPS))  # type: ignore[return-value]


MANIFEST: dict[str, Any] = {
    "snapshot_id": "100-20261106T120000Z",
    "n_ranges_queried": 3,
    "saturated_ranges": [],
    "complete": True,
    "mature": True,
    "vod_age_days": 10.0,
}


def test_clip_stats_boundaries_and_timing() -> None:
    start = NOW - timedelta(days=10)
    frame = clip_frame(
        [
            {"vod_offset_s": 10.0, "created_at": start + timedelta(seconds=40)},
            {"vod_offset_s": 80.0, "duration_s": 30.0},  # ends at 110 > 100: runs past the end
            {"vod_offset_s": 100.0, "creator_hash": "cd" * 32},  # starts exactly at the end
            {"vod_offset_s": None, "view_count": 7},
        ]
    )
    stats = pr.clip_stats(
        frame, MANIFEST, vod_created_at=start, vod_duration_s=100.0, listing=(12, 3)
    )
    assert stats.n_clips == 4
    assert stats.n_without_offset == 1
    assert stats.n_start_after_end == 1
    assert stats.n_end_after_end == 2  # the one past the end and the one starting at it
    assert stats.n_made_after_stream == 3  # only the first was made during the stream
    assert stats.views == (1, 1, 1, 7)
    assert stats.n_creators == 2
    assert stats.densest_window == 3
    assert (stats.n_other_videos, stats.n_duplicates) == (12, 3)


def write_chat(path: Path, frame: pl.DataFrame) -> None:
    """Write chat the way ingest does: Arrow in the ``chat_messages`` schema."""
    pq.write_table(frame.to_arrow().cast(tables.CHAT_MESSAGES), path)


def chat_frame(
    offsets: list[float], texts: list[str], hashes: list[str] | None = None
) -> pl.DataFrame:
    rows = [
        {
            "video_id": "100",
            "offset_s": o,
            "user_hash": (hashes or [HASH] * len(offsets))[i],
            "text": texts[i],
            "emote_ids": [],
            "n_emotes": 0,
            "is_subscriber": False,
            "is_moderator": False,
            "bits": 0,
            "source": "replay",
        }
        for i, o in enumerate(offsets)
    ]
    return pl.from_arrow(pa.Table.from_pylist(rows, schema=tables.CHAT_MESSAGES))  # type: ignore[return-value]


def test_chat_stats_counts_messages_after_the_end_and_privacy_problems() -> None:
    clean = pr.chat_stats(
        chat_frame([0.0, 50.0, 100.0, 101.0], ["hi @user", "x", "y", "z"]), vod_duration_s=100.0
    )
    assert (clean.n_messages, clean.n_authors, clean.n_after_end) == (4, 1, 1)
    assert (clean.n_bad_hashes, clean.n_unredacted_mentions, clean.offsets_sorted) == (0, 0, True)

    leaky = pr.chat_stats(
        chat_frame([5.0, 1.0], ["hello @someone", "ok"], [HASH, "not-a-hash"]),
        vod_duration_s=100.0,
    )
    assert leaky.n_unredacted_mentions == 1
    assert leaky.n_bad_hashes == 1
    assert leaky.offsets_sorted is False


_V = "logger=autoclipper.ingest.vod ingest step="
LOG = [  # the shape of a real attempt log (runner time, stream, the child's log line)
    "2026-11-06T23:01:33.869135Z err logger=autoclipper.ingest.twitch_api fetched token",
    f"2026-11-06T23:01:34.016740Z err {_V}vod_meta video_id=1 status=ok",
    "2026-11-06T23:01:52.170529Z err logger=autoclipper.ingest.clips fetched clips video_id=1"
    " n=5 without_offset=0 other_videos=29 duplicates=4 ranges=3 split=0 saturated=0",
    f"2026-11-06T23:01:52.192472Z err {_V}clips video_id=1 status=ok",
    "2026-11-06T23:04:25.025607Z err logger=autoclipper.ingest.chat parsed chat replay"
    " video_id=1 n_messages=9 n_malformed=2 format_version=1.4.0",
    f"2026-11-06T23:04:25.833281Z err {_V}chat video_id=1 status=ok",
    "2026-11-06T23:09:04.479820Z err logger=autoclipper.ingest.media downloaded audio path=x",
    f"2026-11-06T23:09:24.024702Z err {_V}audio video_id=1 status=ok",
    "2026-11-06T23:09:24.052627Z out     audio: ok - 14074.3 s",
]


def test_read_log_and_phases() -> None:
    facts = pr.read_log(LOG)
    assert facts.n_malformed == 2
    assert facts.listing == (29, 4)
    started = datetime(2026, 11, 6, 23, 1, 32, 481528, tzinfo=UTC)
    phases = pr.phase_seconds(started, facts.milestones)
    assert phases == pytest.approx(
        {
            "metadata": 1.535212,
            "clips": 18.175732,
            "chat": 153.640809,
            "audio_download": 278.646539,
            "normalize": 19.544882,
        }
    )


def test_phases_of_skipped_steps_are_unknown() -> None:
    lines = [ln for ln in LOG if "step=chat" not in ln]
    phases = pr.phase_seconds(NOW, pr.read_log(lines).milestones)
    assert phases["chat"] is None
    assert phases["audio_download"] is None  # it starts where chat ended


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("catalog.parquet", ("tables", None)),
        ("processed/vods.parquet", ("tables", None)),
        ("raw/audio/100.ts", ("raw_audio", "100")),
        ("raw/chat/100.json", ("raw_chat", "100")),
        ("interim/audio/100.wav", ("wav", "100")),
        ("interim/chat/100.parquet", ("chat", "100")),
        ("processed/clips/100-20261106T120000Z.json", ("clips", "100")),
        ("pilot/selections/superseded/selection-x.json", ("selections", None)),
        ("pilot/runs/r/100-1.log", ("runs", None)),
        ("pilot/run.lock", ("lock", None)),
        ("pilot/notes.md", ("notes", None)),
        ("interim/features/100.parquet", ("other", None)),
    ],
)
def test_classify(path: str, expected: tuple[str, str | None]) -> None:
    assert pr.classify(Path(path)) == expected


# --- A fake pilot data directory ---------------------------------------------------------------

VOD_S = 4.0  # seconds: long enough for real WAVs, small enough for a test


def make_vod(video_id: str, owner: str, age_days: float) -> Vod:
    return Vod(
        video_id=video_id,
        broadcaster_id=owner,
        stream_id=None,
        title="t",
        created_at=NOW - timedelta(days=age_days),
        duration_s=VOD_S,
        type="archive",
        language="en",
        view_count=1,
        muted_segments=(),
        game_ids=None,
        fetched_at=NOW,
    )


def write_wav(path: Path, seconds: float, rate: int = 16_000) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(seconds * rate))


def ingest_fake(data_dir: Path, v: Vod, n_clips: int, run_id: str, *, messages: int = 3) -> None:
    """The files `autoclipper ingest` and the runner leave for one VOD."""
    for step in STEPS:
        tool = {"chat": "TwitchDownloaderCLI@1.56.5", "audio": "yt-dlp@2026.08.19"}
        record(data_dir, CatalogEntry(step, v.video_id, "ok", tool.get(step, "helix"), "1", NOW))
    snapshot = f"{v.video_id}-20261106T120000Z"
    clips_dir = data_dir / "processed" / "clips"
    clips_dir.mkdir(parents=True, exist_ok=True)
    rows = [
        {
            "clip_id": f"{v.video_id}-c{n}",
            "video_id": v.video_id,
            "broadcaster_id": v.broadcaster_id,
            "creator_hash": HASH,
            "title": "t",
            "game_id": "509658",
            "language": "en",
            "created_at": v.created_at + timedelta(seconds=n),
            "vod_offset_s": float(n),
            "duration_s": 2.0,
            "view_count": n + 1,
            "is_featured": False,
            "fetched_at": NOW,
        }
        for n in range(n_clips)
    ]
    pq.write_table(
        pa.Table.from_pylist(rows, schema=tables.CLIPS), clips_dir / f"{snapshot}.parquet"
    )
    manifest = {
        "snapshot_id": snapshot,
        "file": f"{snapshot}.parquet",
        "n_clips": n_clips,
        "n_without_vod_offset": 0,
        "n_ranges_queried": 2,
        "saturated_ranges": [],
        "complete": True,
        "mature": True,
        "vod_age_days": (NOW - v.created_at) / timedelta(days=1),
    }
    (clips_dir / f"{snapshot}.json").write_text(json.dumps(manifest), encoding="utf-8")
    chat = data_dir / "interim" / "chat" / f"{v.video_id}.parquet"
    chat.parent.mkdir(parents=True, exist_ok=True)
    frame = chat_frame([float(i) for i in range(messages)], ["hi"] * messages)
    write_chat(chat, frame.with_columns(pl.lit(v.video_id).alias("video_id")))
    write_wav(data_dir / "interim" / "audio" / f"{v.video_id}.wav", VOD_S)
    raw = data_dir / "raw" / "audio" / f"{v.video_id}.ts"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_bytes(b"\x47" * 1880)
    runs = data_dir / "pilot" / "runs"
    (runs / run_id).mkdir(parents=True, exist_ok=True)
    started = NOW + timedelta(hours=1)
    attempt = {
        "record": "attempt",
        "video_id": v.video_id,
        "attempt": 1,
        "started_at": started.isoformat().replace("+00:00", "Z"),
        "duration_s": 20.0,
        "ended_at": (started + timedelta(seconds=20)).isoformat().replace("+00:00", "Z"),
        "state_after": "complete",
        "peak_job_memory": (600 + messages) * pr.MIB,
        "system_memory_free_min": 7 * pr.GIB,
        "raw_chat_peak_bytes": 0,
        "temp_files_left": [],
    }
    limits = {"retries": 0, "timeout_base_s": 3600.0, "timeout_per_hour_s": 1800.0}
    start = {"record": "start", "run_id": run_id, "limits": limits}
    lines = [start, attempt, {"record": "summary"}]
    (runs / f"{run_id}.jsonl").write_text(
        "".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8"
    )
    stamp = lambda s: (started + timedelta(seconds=s)).isoformat().replace("+00:00", "Z")  # noqa: E731
    log = [
        f"{stamp(1)} err logger=autoclipper.ingest.vod ingest step=vod_meta video_id=1 status=ok",
        f"{stamp(3)} err logger=autoclipper.ingest.vod ingest step=clips video_id=1 status=ok",
        f"{stamp(6)} err logger=autoclipper.ingest.vod ingest step=chat video_id=1 status=ok",
        f"{stamp(16)} err logger=autoclipper.ingest.media downloaded audio video_id=1 path=x",
        f"{stamp(19)} err logger=autoclipper.ingest.vod ingest step=audio video_id=1 status=ok",
    ]
    (runs / run_id / f"{v.video_id}-1.log").write_text("\n".join(log) + "\n", encoding="utf-8")


@pytest.fixture
def pilot(tmp_path: Path) -> dict[str, Path]:
    """Two channels, one active VOD each, and one historical VOD of a dropped channel."""
    data_dir = tmp_path / "data"
    a, b = make_vod("100", A_ID, 10), make_vod("200", B_ID, 12)
    old = make_vod("300", OLD_ID, 20)
    params = sp.Params(per_channel=1, backups=0)
    channels = {"chan_a": (A_ID, a), "chan_b": (B_ID, b)}
    selections = []
    for n, (login, (owner, v)) in enumerate(channels.items()):
        at = NOW + timedelta(minutes=n)
        selection = sp.select(
            [Broadcaster(owner, login, login, "partner", at)],
            {login: Listing((v,), truncated=False)},
            at,
            params,
        )
        path = sp.selections_dir(data_dir) / f"selection-{at:%Y%m%dT%H%M%SZ}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(sp.selection_to_json(selection)), encoding="utf-8")
        selections.append(path)
    sp.write_broadcasters(
        [
            Broadcaster(A_ID, "chan_a", "chan_a", "partner", NOW),
            Broadcaster(B_ID, "chan_b", "chan_b", "partner", NOW),
            Broadcaster(OLD_ID, "chan_old", "chan_old", "partner", NOW),
        ],
        data_dir,
    )
    vod_rows = [
        {
            "video_id": v.video_id,
            "broadcaster_id": v.broadcaster_id,
            "stream_id": None,
            "title": "t",
            "created_at": v.created_at,
            "duration_s": v.duration_s,
            "type": "archive",
            "language": "en",
            "view_count": 1,
            "muted_segments": [],
            "game_ids": None,
            "fetched_at": NOW,
        }
        for v in (a, b, old)
    ]
    vods_path = data_dir / "processed" / "vods.parquet"
    pq.write_table(pa.Table.from_pylist(vod_rows, schema=tables.VODS), vods_path)
    ingest_fake(data_dir, a, 3, "20261106T130000Z", messages=4)
    ingest_fake(data_dir, b, 2, "20261106T140000Z", messages=8)
    ingest_fake(data_dir, old, 0, "20261106T120000Z")
    (data_dir / "raw" / "chat").mkdir(parents=True)
    (data_dir / "pilot" / "run.lock").write_bytes(b"")
    (data_dir / "pilot" / "notes.md").write_text("private notes\n", encoding="utf-8")
    vod_list = tmp_path / "pilot_vods.txt"
    vod_list.write_text(f"{vod_url('100')}\n{vod_url('200')}\n", encoding="utf-8")
    channels_file = tmp_path / "channels.yaml"
    channels_file.write_text(
        "channels:\n  - login: chan_a\n    reason: a\n  - login: chan_b\n    reason: b\n",
        encoding="utf-8",
    )
    return {"data": data_dir, "list": vod_list, "channels": channels_file}


def build(pilot: dict[str, Path]) -> pr.Report:
    return pr.build_report(
        pilot["data"],
        vod_list=pilot["list"],
        selection_paths=sorted(sp.selections_dir(pilot["data"]).glob("selection-*.json")),
        channels=["chan_a", "chan_b"],
    )


def digests(root: Path) -> dict[str, str]:
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


# --- The report over the fake directory --------------------------------------------------------


def test_report_measures_active_and_historical_vods(pilot: dict[str, Path]) -> None:
    report = build(pilot)
    assert report.ok, [c for c in report.checks if not c.ok]
    assert [v.video_id for v in report.group("active")] == ["100", "200"]
    assert [(v.video_id, v.login) for v in report.group("historical")] == [("300", "chan_old")]
    first = report.group("active")[0]
    assert first.login == "chan_a"
    assert first.clips is not None
    assert first.clips.n_clips == 3
    assert first.chat is not None
    assert first.chat.n_messages == 4
    assert first.audio is not None
    assert first.audio.wav_duration_s == VOD_S
    assert first.audio.wav_header_bytes == 44
    assert first.run is not None
    assert first.run.phases_s == {
        "metadata": 1.0,
        "clips": 2.0,
        "chat": 3.0,
        "audio_download": 10.0,
        "normalize": 3.0,
    }
    totals = pr.totals(report.group("active"), pilot["data"])
    assert (totals.n_vods, totals.clips, totals.messages, totals.chat_ok) == (2, 5, 12, 2)
    assert totals.authors == 1  # the same pseudonym in both VODs is one chatter
    assert totals.hours == pytest.approx(2 * VOD_S / 3600)


def test_report_reads_no_more_than_it_needs_and_changes_nothing(pilot: dict[str, Path]) -> None:
    before = digests(pilot["data"])
    pr.render_blocks(build(pilot), pilot["data"])
    assert digests(pilot["data"]) == before


def test_storage_reconciles_with_the_directory(pilot: dict[str, Path]) -> None:
    report = build(pilot)
    on_disk = sum(p.stat().st_size for p in pilot["data"].rglob("*") if p.is_file())
    assert sum(r.bytes for r in report.storage) == on_disk
    by_category = {r.category: r for r in report.storage}
    assert by_category["raw_audio"].by_group == {"active": 2 * 1880, "historical": 1880}
    assert by_category["notes"].files == 1
    assert report.unclassified == ()


def failing(report: pr.Report) -> list[str]:
    return [c.name.split(":")[0] for c in report.checks if not c.ok]


def test_an_unredacted_mention_fails_the_chat_check(pilot: dict[str, Path]) -> None:
    path = pilot["data"] / "interim" / "chat" / "200.parquet"
    frame = pl.read_parquet(path)
    write_chat(path, frame.with_columns(pl.lit("hey @realname").alias("text")))
    assert failing(build(pilot)) == ["chat"]


def test_a_raw_chat_replay_fails_the_privacy_check(pilot: dict[str, Path]) -> None:
    (pilot["data"] / "raw" / "chat" / "100.json").write_text("{}", encoding="utf-8")
    assert failing(build(pilot)) == ["privacy"]


def test_unknown_files_fail_the_storage_check(pilot: dict[str, Path]) -> None:
    (pilot["data"] / "stray.bin").write_bytes(b"x")
    (pilot["data"] / "raw" / "audio" / "999.ts").write_bytes(b"x")  # a VOD nobody cataloged
    report = build(pilot)
    assert failing(report) == ["storage"]
    assert report.unclassified == ("stray.bin",)


def test_a_failed_step_or_immature_snapshot_fails(pilot: dict[str, Path]) -> None:
    record(
        pilot["data"],
        CatalogEntry("audio", "100", "failed", "yt-dlp@x", "1", NOW, "DownloadError: x"),
    )
    manifest = pilot["data"] / "processed" / "clips" / "200-20261106T120000Z.json"
    content = json.loads(manifest.read_text(encoding="utf-8"))
    manifest.write_text(json.dumps({**content, "mature": False}), encoding="utf-8")
    report = build(pilot)
    assert failing(report) == ["catalog", "clips"]
    assert {c.name.split(":")[0]: c.detail for c in report.checks}["catalog"] == "100"


def test_metadata_of_another_channel_fails(pilot: dict[str, Path]) -> None:
    path = pilot["data"] / "processed" / "vods.parquet"
    rows = pq.read_table(path, schema=tables.VODS).to_pylist()
    rows = [{**r, "broadcaster_id": B_ID} if r["video_id"] == "100" else r for r in rows]
    pq.write_table(pa.Table.from_pylist(rows, schema=tables.VODS), path)
    # The clips (of chan_a) no longer match the VOD's channel either.
    assert failing(build(pilot)) == ["metadata", "clips"]


def test_a_listed_vod_that_is_not_a_primary_fails(pilot: dict[str, Path]) -> None:
    pilot["list"].write_text(f"{vod_url('100')}\n{vod_url('300')}\n", encoding="utf-8")
    report = build(pilot)
    assert "list" in failing(report)
    assert "not primaries: 300" in report.checks[0].detail


# --- Document blocks and the command line ------------------------------------------------------


def test_update_then_check_document(pilot: dict[str, Path]) -> None:
    blocks = pr.render_blocks(build(pilot), pilot["data"])
    doc = "# Report\n\nProse.\n\n" + "".join(
        f"{pr.BEGIN.format(n)}\nstale\n{pr.END.format(n)}\n\nMore prose.\n" for n in blocks
    )
    assert pr.check_document(doc, blocks) == [f"block {n!r} differs from the data" for n in blocks]
    updated, missing = pr.update_document(doc, blocks)
    assert missing == []
    assert pr.check_document(updated, blocks) == []
    assert updated.count("More prose.") == len(blocks)
    assert pr.check_document(updated.replace("| chan_a |", "| chan_x |"), blocks)
    partial = updated.replace(pr.BEGIN.format("checks"), "").replace(pr.END.format("checks"), "")
    assert pr.check_document(partial, blocks) == ["block 'checks' is missing"]


def test_blocks_show_the_issue_metrics(pilot: dict[str, Path]) -> None:
    blocks = pr.render_blocks(build(pilot), pilot["data"])
    metrics = blocks["issue-metrics"]
    assert "| Chat success rate | 2 / 2 VODs (100 %) |" in metrics
    assert "| Clips without a VOD offset | 0 of 5 (0.0 %) |" in metrics
    assert "| Clip-search ranges that hit the clip limit | 0 of 4 ranges queried |" in metrics
    assert "| **all** | 2 |" in blocks["channels"]
    assert "chan_old" in blocks["historical"]
    assert "| **Total (dataset)** |" in blocks["storage"]
    assert "notes" not in blocks["storage"]
    provenance = blocks["provenance"]
    assert "| Runner settings (3 of 3 runs) | retries 0, timeout 60 min + 30 min per" in provenance
    assert "| Collection window (UTC, all runs) | 2026-11-06T13:00:00 to 2026-11-06T13:00:20 |" in (
        provenance
    )


def run_cli(pilot: dict[str, Path], *args: str) -> Result:
    return CliRunner().invoke(
        pr.app,
        [
            "--data-dir",
            str(pilot["data"]),
            "--vods",
            str(pilot["list"]),
            "--channels",
            str(pilot["channels"]),
            *args,
        ],
    )


def test_cli_prints_updates_and_checks(pilot: dict[str, Path], tmp_path: Path) -> None:
    printed = run_cli(pilot)
    assert printed.exit_code == ExitCode.SUCCESS, printed.output
    assert "<!-- BEGIN pilot-report:issue-metrics -->" in printed.stdout
    doc = tmp_path / "report.md"
    doc.write_text(printed.stdout, encoding="utf-8")
    assert run_cli(pilot, "--check", str(doc)).exit_code == ExitCode.SUCCESS
    doc.write_text(printed.stdout.replace("| 2 / 2 VODs", "| 1 / 2 VODs"), encoding="utf-8")
    stale = run_cli(pilot, "--check", str(doc))
    assert stale.exit_code == ExitCode.USER_ERROR
    assert "'issue-metrics' differs from the data" in stale.stderr
    assert run_cli(pilot, "--update", str(doc)).exit_code == ExitCode.SUCCESS
    assert doc.read_text(encoding="utf-8") == printed.stdout
    out = tmp_path / "report.json"
    assert run_cli(pilot, "--json", str(out)).exit_code == ExitCode.SUCCESS
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["ok"] is True
    assert [v["video_id"] for v in data["vods"]] == ["100", "200", "300"]


def test_provenance_names_the_list_that_was_read(pilot: dict[str, Path]) -> None:
    # Regression: the provenance row always said `configs/pilot_vods.txt`, even for --vods FILE.
    other = pilot["list"].with_name("other_list.txt")
    other.write_bytes(pilot["list"].read_bytes())
    result = run_cli(pilot, "--vods", str(other))
    assert result.exit_code == ExitCode.SUCCESS, result.output
    digest = hashlib.sha256(other.read_bytes()).hexdigest()
    assert f"| `<outside the repository>/other_list.txt` | `{digest}` |" in result.stdout
    assert "configs/pilot_vods.txt" not in result.stdout
    assert str(other.parent) not in result.stdout  # no absolute local path in the report


def test_printed_tables_are_utf8_whatever_the_locale(pilot: dict[str, Path]) -> None:
    # Regression: on Windows a redirected stdout uses the legacy code page (cp1252), which can't
    # encode the tables' "≥", so `pilot_report.py > tables.md` crashed. PYTHONIOENCODING=cp1252
    # reproduces that default on every OS. (The script makes no network request.)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("TWITCH_", "AUTOCLIPPER_"))}
    env |= {"PYTHONIOENCODING": "cp1252", "PYTHONUTF8": "0"}
    completed = subprocess.run(
        [
            sys.executable,
            str(sp.REPO_ROOT / "scripts" / "pilot_report.py"),
            *("--data-dir", str(pilot["data"]), "--vods", str(pilot["list"])),
            *("--channels", str(pilot["channels"])),
        ],
        capture_output=True,
        env=env,
        check=False,
        timeout=120,
    )
    assert completed.returncode == ExitCode.SUCCESS, completed.stderr.decode("utf-8", "replace")
    printed = completed.stdout.decode("utf-8")  # strict: the output must be valid UTF-8
    assert "Negative (≥ 60 s away)" in printed
    assert printed == run_cli(pilot).stdout.replace("\n", os.linesep)


def test_list_label_is_repository_relative_or_the_file_name(tmp_path: Path) -> None:
    assert pr.list_label(sp.DEFAULT_LIST) == "configs/pilot_vods.txt"
    assert pr.list_label(sp.REPO_ROOT / "configs" / "alt.txt") == "configs/alt.txt"
    assert pr.list_label(tmp_path / "a" / "b.txt") == "<outside the repository>/b.txt"


def test_cli_exits_1_when_a_check_fails(pilot: dict[str, Path]) -> None:
    (pilot["data"] / "raw" / "chat" / "100.json").write_text("{}", encoding="utf-8")
    result = run_cli(pilot)
    assert result.exit_code == ExitCode.USER_ERROR
    assert "check failed: privacy" in result.stderr


def test_cli_refuses_a_directory_without_a_catalog(tmp_path: Path) -> None:
    result = CliRunner().invoke(pr.app, ["--data-dir", str(tmp_path)])
    assert result.exit_code == ExitCode.USER_ERROR
    assert "no catalog" in result.stderr


def test_label_preview_splits_windows_by_distance_to_moments() -> None:
    # 200 s: windows [0, 10) ... [190, 200). One moment [50, 70) from two overlapping clips.
    clips = clip_frame([{"vod_offset_s": 50.0, "duration_s": 15.0}, {"vod_offset_s": 55.0}])
    clips = clips.with_columns(pl.col("duration_s").clip(upper_bound=15.0))
    preview = pr.label_preview(clips, video_id="100", vod_duration_s=200.0)
    assert preview.n_windows == 20
    assert preview.n_moments == 1
    assert preview.n_positive == 2  # [50, 60) and [60, 70); [70, 80) only touches the moment
    assert preview.n_negative == 7  # starting 60 s or more after it ends: [130, 140) ... [190, 200)
    assert preview.n_buffer == 11
    empty = pr.label_preview(clips.clear(), video_id="100", vod_duration_s=200.0)
    assert (empty.n_positive, empty.n_negative, empty.n_buffer) == (0, 20, 0)


def test_estimated_expiry_follows_the_channel_type(pilot: dict[str, Path]) -> None:
    first = build(pilot).group("active")[0]
    assert first.retention_days == 60  # a partner
    assert (
        first.estimated_expiry == (NOW - timedelta(days=10) + timedelta(days=60)).date().isoformat()
    )
