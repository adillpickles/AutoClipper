"""Tests for scripts/run_pilot.py.

Most tests drive the batch with a fake process tree, clock, disk and memory, against a catalog in
``tmp_path``. The process-tree tests start real, harmless local Python processes (a child that
starts a grandchild) and check that both are gone after the runner stops them. Nothing here
contacts Twitch or downloads anything; real network connections are blocked.
"""

import json
import os
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import run_pilot as rp
import select_pilot_vods as sp
from typer.testing import CliRunner

from autoclipper.cli import ExitCode
from autoclipper.ingest.catalog import CatalogEntry, read_catalog, record
from autoclipper.ingest.media import normalized_audio_path, raw_audio_dir
from autoclipper.ingest.twitch_api import Broadcaster, Listing, Vod
from autoclipper.ingest.vod import STEPS, chat_path, raw_chat_path

NOW = datetime.now(tz=UTC).replace(microsecond=0)
A_ID, B_ID = "9000000001", "9000000002"
GB = rp.GB
FAKE_SECRET = "zz-fake-client-secret-never-stored"
FAKE_SALT = "zz-fake-chat-salt-never-stored-0123456789"
WINDOWS = sys.platform == "win32"


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail any test that opens a real network connection."""

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError(f"network access attempted: {args!r}")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


# --- Selections --------------------------------------------------------------------------------


def vod(video_id: str, age_days: float, *, owner: str = A_ID, hours: float = 2.0) -> Vod:
    return Vod(
        video_id=video_id,
        broadcaster_id=owner,
        stream_id=None,
        title="t",
        created_at=NOW - timedelta(days=age_days),
        duration_s=hours * 3600,
        type="archive",
        language="en",
        view_count=1,
        muted_segments=(),
        game_ids=None,
        fetched_at=NOW,
    )


def select(login: str, owner: str, vods: list[Vod], at: datetime = NOW) -> sp.Selection:
    b = Broadcaster(owner, login, login, "partner", at)
    params = sp.Params(per_channel=2, backups=1)
    return sp.select([b], {login: Listing(tuple(vods), truncated=False)}, at, params)


@pytest.fixture
def selections() -> list[sp.Selection]:
    """chan_a (three VODs: 2 primaries, 1 backup) and chan_b fetched a minute later."""
    a = select("chan_a", A_ID, [vod("100", 20), vod("101", 10), vod("102", 15)])
    b = select(
        "chan_b",
        B_ID,
        [vod("200", 25, owner=B_ID), vod("201", 12, owner=B_ID)],
        at=NOW + timedelta(minutes=1),
    )
    return [a, b]


def picks(selections: list[sp.Selection]) -> dict[str, str]:
    return {i: a.decision for i, a in sp.approved_picks(selections).items()}


def plan(selections: list[sp.Selection], ids: Sequence[str], **kw: object) -> list[rp.PlannedVod]:
    options: dict[str, object] = {
        "channels": ["chan_a", "chan_b"],
        "broadcasters": None,
        "allow_backup": False,
        "now": NOW + timedelta(minutes=5),
        **kw,
    }
    return rp.plan_vods(selections, ids, **options)  # type: ignore[arg-type]


# --- Plan --------------------------------------------------------------------------------------


def test_fixture_picks(selections: list[sp.Selection]) -> None:
    assert picks(selections) == {
        "100": "primary",
        "101": "primary",
        "102": "backup",
        "200": "primary",
        "201": "primary",
    }


def test_plan_takes_primaries_of_several_selections_oldest_first(
    selections: list[sp.Selection],
) -> None:
    result = plan(selections, ["101", "200", "100", "201"])
    assert [v.video_id for v in result] == ["200", "100", "201", "101"]
    assert [v.login for v in result] == ["chan_b", "chan_a", "chan_b", "chan_a"]
    assert {v.designation for v in result} == {"primary"}
    assert result[0].broadcaster_id == B_ID


def test_plan_of_one_vod_has_exactly_one(selections: list[sp.Selection]) -> None:
    assert [v.video_id for v in plan(selections, ["https://www.twitch.tv/videos/101"])] == ["101"]


@pytest.mark.parametrize(
    ("ids", "message"),
    [
        (["999"], "not a primary or backup of these selections: 999"),
        (["100", "100"], "repeated"),
        ([], "no VODs requested"),
    ],
)
def test_plan_refuses_vods_that_were_not_approved(
    selections: list[sp.Selection], ids: list[str], message: str
) -> None:
    with pytest.raises(rp.PlanError, match=message):
        plan(selections, ids)


def test_backups_run_only_when_allowed_and_keep_their_designation(
    selections: list[sp.Selection],
) -> None:
    with pytest.raises(rp.PlanError, match="102 is a backup"):
        plan(selections, ["102"])
    (backup,) = plan(selections, ["102"], allow_backup=True)
    assert backup.designation == "backup"


def test_plan_refuses_a_channel_not_in_the_channels_file(selections: list[sp.Selection]) -> None:
    with pytest.raises(rp.PlanError, match="channels not in the channels file: chan_b"):
        plan(selections, ["200"], channels=["chan_a"])


def test_plan_refuses_a_vod_of_another_broadcaster(selections: list[sp.Selection]) -> None:
    a, b = selections
    tampered = tuple(
        replace(x, vod=replace(x.vod, broadcaster_id=B_ID)) if x.vod.video_id == "100" else x
        for x in a.assessments
    )
    with pytest.raises(rp.PlanError, match=f"100 belongs to {B_ID}, not chan_a"):
        plan([replace(a, assessments=tampered), b], ["100"])


def test_plan_refuses_when_broadcasters_parquet_disagrees(selections: list[sp.Selection]) -> None:
    good = {"chan_a": A_ID, "chan_b": B_ID}
    assert plan(selections, ["100"], broadcasters=good)
    with pytest.raises(rp.PlanError, match=r"broadcasters\.parquet disagrees"):
        plan(selections, ["100"], broadcasters={**good, "chan_a": "123"})


def test_the_seven_day_rule_is_checked_again_at_run_time(selections: list[sp.Selection]) -> None:
    created = sp.approved_picks(selections)["101"].vod.created_at  # 10 days before NOW
    exactly = created + timedelta(days=7)
    assert plan(selections, ["101"], now=exactly)
    with pytest.raises(rp.PlanError, match="101 is under 7 days old"):
        plan(selections, ["101"], now=exactly - timedelta(seconds=1))


def test_expiry_risk_is_reported_not_enforced() -> None:
    old = select("chan_a", A_ID, [vod("100", 58)])
    (planned,) = plan([old], ["100"], now=NOW)
    assert planned.expiry_risk == "high"


def test_selections_that_disagree_are_refused(selections: list[sp.Selection]) -> None:
    other = select("chan_a", "777", [vod("300", 9, owner="777")], at=NOW + timedelta(minutes=2))
    with pytest.raises(rp.PlanError, match="disagree about chan_a"):
        plan([*selections, other], ["100"])


def test_a_pilot_list_reads_as_unique_ids_and_is_left_unchanged(tmp_path: Path) -> None:
    path = tmp_path / "pilot_vods.txt"
    ids = [str(9100000000 + n) for n in range(25)]  # synthetic VOD IDs
    links = [f"https://www.twitch.tv/videos/{i}" for i in ids]
    path.write_text("\n".join(links) + "\n\n", encoding="utf-8")  # a trailing blank line too
    before = path.read_bytes()
    assert rp.read_vod_list(path) == ids
    assert path.read_bytes() == before


def test_a_bad_line_in_the_pilot_list_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "list.txt"
    path.write_text("https://www.twitch.tv/videos/100\n\nnot a vod\n", encoding="utf-8")
    with pytest.raises(rp.PlanError, match=r"list\.txt:3"):
        rp.read_vod_list(path)


# --- States and retries ------------------------------------------------------------------------


def entry(step: str, video_id: str, status: str, error: str | None = None) -> CatalogEntry:
    return CatalogEntry(step, video_id, status, "test", "1", NOW, error)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("statuses", "state"),
    [
        (["ok", "ok", "ok", "ok"], "complete"),
        (["ok", "ok", "ok", "unavailable"], "partial"),
        (["unavailable"] * 4, "unavailable"),
        (["ok", "ok", "failed", "unavailable"], "failed"),
        (["ok", "ok", None, None], "incomplete"),
        ([None] * 4, "incomplete"),
    ],
)
def test_vod_state(statuses: list[str | None], state: str) -> None:
    entries = {
        step: None if s is None else entry(step, "1", s)
        for step, s in zip(STEPS, statuses, strict=True)
    }
    assert rp.vod_state(entries) == state


@pytest.mark.parametrize(
    ("error", "retry"),
    [
        ("DownloadNetworkError: connection reset", True),
        ("TwitchUnavailableError: 503", True),
        ("DownloadIntegrityError: damaged bytes", True),
        ("ReadTimeout: timed out", True),
        (
            "ChatDownloadError: TwitchDownloaderCLI chatdownload failed for video_id=1 with exit "
            "code 1. ...",
            True,
        ),
        ("ChatDownloadError: unsupported chat format version 9.9.9", False),
        ("NoAudioOnlyFormatError: no audio-only format", False),
        ("MediaLengthError: wrong length", False),
        ("TwitchAuthError: 401", False),
        ("AUTOCLIPPER_CHAT_HASH_SALT is not configured", False),
        (None, False),
    ],
)
def test_only_failures_that_can_pass_are_retried(error: str | None, retry: bool) -> None:
    assert rp.is_retryable(error) is retry


# --- Redaction ---------------------------------------------------------------------------------


def test_redactor_removes_secrets_tokens_and_url_queries() -> None:
    redact = rp.Redactor([FAKE_SECRET, FAKE_SALT, "short"])
    line = (
        f"secret {FAKE_SECRET} salt={FAKE_SALT} "
        "url https://usher.ttvnw.net/vod/1.m3u8?sig=abc123&token=%7B%22a%22%7D end "
        'client_secret=xyz "access_token": "tok123" Authorization: Bearer abcdefghijk short'
    )
    out = redact(line)
    for leaked in (FAKE_SECRET, FAKE_SALT, "abc123", "%7B", "xyz", "tok123", "abcdefghijk"):
        assert leaked not in out
    assert "https://usher.ttvnw.net/vod/1.m3u8?[redacted]" in out
    assert out.endswith("short")  # values under 8 characters aren't treated as secrets
    assert (
        redact("ingest step=chat video_id=1 status=ok") == "ingest step=chat video_id=1 status=ok"
    )


def test_redactor_cuts_long_lines() -> None:
    assert len(rp.Redactor()("x" * 5000)) == rp.MAX_LINE_CHARS + len("...[cut]")


# --- Fakes for the batch -----------------------------------------------------------------------


@dataclass
class Scenario:
    """What one fake ingest run does."""

    exit_code: int | None = 0  # None: never exits (until terminated)
    polls: int = 2  # polls before it exits
    statuses: dict[str, tuple[str, str | None]] = field(
        default_factory=lambda: dict.fromkeys(STEPS, ("ok", None))
    )
    peak: list[int] = field(default_factory=list)
    lines: list[tuple[str, str]] = field(default_factory=list)
    interrupt_at_poll: int | None = None
    raw_chat_bytes: int = 0
    fail_launch: bool = False


class FakeTree:
    def __init__(self, scenario: Scenario, video_id: str, data_dir: Path, sink: rp.Sink) -> None:
        self.scenario, self.video_id, self.data_dir = scenario, video_id, data_dir
        self.count = 0
        self.terminated = self.closed = False
        for stream, line in scenario.lines:
            sink(stream, line)
        if scenario.raw_chat_bytes:
            raw = raw_chat_path(data_dir, video_id)
            raw.parent.mkdir(parents=True, exist_ok=True)
            raw.write_bytes(b"x" * scenario.raw_chat_bytes)

    def poll(self) -> int | None:
        self.count += 1
        if self.scenario.interrupt_at_poll == self.count:
            raise KeyboardInterrupt
        if self.scenario.exit_code is None or self.count <= self.scenario.polls:
            return None
        if self.count == self.scenario.polls + 1:
            for step, (status, error) in self.scenario.statuses.items():
                record(self.data_dir, entry(step, self.video_id, status, error))
        return self.scenario.exit_code

    def terminate(self) -> None:
        self.terminated = True

    def peak_memory(self) -> int | None:
        peaks = self.scenario.peak
        return peaks[min(self.count, len(peaks)) - 1] if peaks else None

    def close(self) -> None:
        self.closed = True


class FakeLauncher:
    def __init__(self, data_dir: Path, *scenarios: Scenario) -> None:
        self.data_dir = data_dir
        self.scenarios = list(scenarios)
        self.commands: list[list[str]] = []
        self.trees: list[FakeTree] = []
        self.caps: list[int | None] = []

    def __call__(self, command: Sequence[str], sink: rp.Sink, memory_cap: int | None) -> rp.Tree:
        self.commands.append(list(command))
        self.caps.append(memory_cap)
        scenario = self.scenarios.pop(0)
        if scenario.fail_launch:
            raise rp.JobSetupError("can't put the child in its job: access denied")
        tree = FakeTree(scenario, command[-1], self.data_dir, sink)
        self.trees.append(tree)
        return tree


class FakeTime:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def clock(self) -> datetime:
        return NOW + timedelta(seconds=self.now)


@dataclass
class Harness:
    data_dir: Path
    launcher: FakeLauncher
    time: FakeTime
    log: rp.RunLog
    disk: list[int]
    memory: list[int | None]

    def deps(self) -> rp.Deps:
        def disk_free(_: Path) -> int:
            return self.disk.pop(0) if len(self.disk) > 1 else self.disk[0]

        def memory_free() -> int | None:
            return self.memory.pop(0) if len(self.memory) > 1 else self.memory[0]

        import random

        return rp.Deps(
            launch=self.launcher,
            clock=self.time.clock,
            monotonic=self.time.monotonic,
            sleep=self.time.sleep,
            disk_free=disk_free,
            memory_free=memory_free,
            rng=random.Random(1),
        )

    def run(self, plan: Sequence[rp.PlannedVod], **limits: object) -> list[rp.VodResult]:
        defaults: dict[str, object] = {"poll_s": 1.0}
        return rp.run_batch(
            plan,
            data_dir=self.data_dir,
            run_dir=self.data_dir / "pilot" / "runs" / "r1",
            command_for=lambda video_id: ["ingest", video_id],
            limits=rp.Limits(**{**defaults, **limits}),  # type: ignore[arg-type]
            hours_by_id={v.video_id: v.hours for v in plan},
            redact=rp.Redactor([FAKE_SECRET, FAKE_SALT]),
            log=self.log,
            deps=self.deps(),
        )

    def records(self, kind: str | None = None) -> list[dict[str, object]]:
        lines = (
            self.log.path.read_text(encoding="utf-8").splitlines() if self.log.path.exists() else []
        )
        rows = [json.loads(line) for line in lines]
        return [r for r in rows if kind is None or r["record"] == kind]


@pytest.fixture
def harness(tmp_path: Path) -> Callable[..., Harness]:
    def make(*scenarios: Scenario, disk: int = 500 * GB, memory: int | None = 8 * GB) -> Harness:
        data_dir = tmp_path / "data"
        return Harness(
            data_dir,
            FakeLauncher(data_dir, *scenarios),
            FakeTime(),
            rp.RunLog(data_dir / "pilot" / "runs" / "r1.jsonl"),
            [disk],
            [memory],
        )

    return make


def planned(video_id: str, hours: float = 2.0, login: str = "chan_a") -> rp.PlannedVod:
    return rp.PlannedVod(video_id, login, A_ID, "primary", NOW, hours * 3600, 10.0, "low")


def failed(step: str, error: str) -> dict[str, tuple[str, str | None]]:
    statuses: dict[str, tuple[str, str | None]] = dict.fromkeys(STEPS, ("ok", None))
    statuses[step] = ("failed", error)
    return statuses


NETWORK = "DownloadNetworkError: connection reset"


# --- Scheduling --------------------------------------------------------------------------------


def test_vods_run_one_at_a_time_in_order_with_a_pause_between(
    harness: Callable[..., Harness],
) -> None:
    h = harness(Scenario(), Scenario())
    results = h.run([planned("100"), planned("101")])
    assert [r.result for r in results] == ["complete", "complete"]
    assert h.launcher.commands == [["ingest", "100"], ["ingest", "101"]]
    pauses = [s for s in h.time.sleeps if s != 1.0]  # 1 s is the poll interval
    assert len(pauses) == 1
    assert 30 <= pauses[0] <= 120
    assert [r["video_id"] for r in h.records("attempt")] == ["100", "101"]


def test_one_vod_runs_once_with_no_pause(harness: Callable[..., Harness]) -> None:
    h = harness(Scenario())
    assert [r.result for r in h.run([planned("100")])] == ["complete"]
    assert len(h.launcher.commands) == 1
    assert set(h.time.sleeps) == {1.0}


def test_a_complete_vod_is_skipped_without_starting_ingest(
    harness: Callable[..., Harness],
) -> None:
    h = harness(Scenario())
    for step in STEPS:
        record(h.data_dir, entry(step, "100", "ok"))
    results = h.run([planned("100"), planned("101")])
    assert [r.result for r in results] == ["already complete", "complete"]
    assert h.launcher.commands == [["ingest", "101"]]
    assert [s for s in h.time.sleeps if s != 1.0] == []  # no network work before 101


def test_a_second_run_repeats_nothing(harness: Callable[..., Harness]) -> None:
    h = harness(Scenario())
    h.run([planned("100")])
    rows = len(read_catalog(h.data_dir))
    again = h.run([planned("100")])
    assert [r.result for r in again] == ["already complete"]
    assert len(h.launcher.commands) == 1
    assert len(read_catalog(h.data_dir)) == rows == 4


def test_a_partly_done_vod_estimates_only_what_is_left(harness: Callable[..., Harness]) -> None:
    h = harness(Scenario())
    record(h.data_dir, entry("vod_meta", "100", "ok"))
    record(h.data_dir, entry("clips", "100", "ok"))
    record(h.data_dir, entry("chat", "100", "ok"))
    h.run([planned("100", hours=2)])
    (attempt,) = h.records("attempt")
    rates = rp.Rates()
    assert attempt["estimated_bytes"] == int(
        ((rates.raw_audio + rates.wav) * 2 + rates.fixed) * 1.5
    )
    assert attempt["state_before"] == "incomplete"
    assert attempt["state_after"] == "complete"
    # The rows of the finished steps are unchanged: only the audio step ran.
    assert attempt["steps_changed"] == ["audio"]


# --- Retries and failures ----------------------------------------------------------------------


def test_retryable_failures_are_retried_at_most_twice(harness: Callable[..., Harness]) -> None:
    fail = Scenario(exit_code=2, statuses=failed("audio", NETWORK))
    h = harness(fail, replace(fail), replace(fail))
    (result,) = h.run([planned("100")])
    assert (result.result, result.attempts) == ("failed", 3)
    waits = [s for s in h.time.sleeps if s != 1.0]
    assert waits == [600, 1800]
    assert [r["retryable"] for r in h.records("attempt")] == [True, True, True]


def test_a_retry_that_succeeds_ends_the_vod(harness: Callable[..., Harness]) -> None:
    h = harness(Scenario(exit_code=2, statuses=failed("chat", NETWORK)), Scenario())
    (result,) = h.run([planned("100")])
    assert (result.result, result.attempts) == ("complete", 2)


def test_failures_that_cannot_pass_are_not_retried(harness: Callable[..., Harness]) -> None:
    h = harness(Scenario(exit_code=2, statuses=failed("audio", "NoAudioOnlyFormatError: none")))
    (result,) = h.run([planned("100")])
    assert (result.result, result.attempts) == ("failed", 1)
    (attempt,) = h.records("attempt")
    assert attempt["errors"] == [
        {
            "step": "audio",
            "type": "NoAudioOnlyFormatError",
            "message": "NoAudioOnlyFormatError: none",
        }
    ]


def test_a_user_error_exit_is_not_retried(harness: Callable[..., Harness]) -> None:
    h = harness(Scenario(exit_code=1, statuses={}))
    (result,) = h.run([planned("100")])
    assert (result.result, result.attempts) == ("incomplete", 1)


@pytest.mark.parametrize(
    ("statuses", "result"),
    [
        (
            {
                **dict.fromkeys(STEPS, ("ok", None)),
                "audio": ("unavailable", "VodAccessRestrictedError: sub-only"),
            },
            "partial",
        ),
        (dict.fromkeys(STEPS, ("unavailable", "gone")), "unavailable"),
    ],
)
def test_unavailable_data_is_final_and_never_counted_as_complete(
    harness: Callable[..., Harness], statuses: dict[str, tuple[str, str | None]], result: str
) -> None:
    h = harness(Scenario(statuses=statuses))
    (r,) = h.run([planned("100")])
    assert (r.result, r.attempts) == (result, 1)


def test_one_failed_vod_does_not_stop_the_next(harness: Callable[..., Harness]) -> None:
    h = harness(Scenario(exit_code=2, statuses=failed("audio", "MediaLengthError: x")), Scenario())
    results = h.run([planned("100"), planned("101")])
    assert [r.result for r in results] == ["failed", "complete"]


def test_a_timeout_ends_the_attempt_and_is_retried(harness: Callable[..., Harness]) -> None:
    h = harness(Scenario(exit_code=None), Scenario())
    (result,) = h.run([planned("100", hours=1)], timeout_base_s=10, timeout_per_hour_s=5)
    assert (result.result, result.attempts) == ("complete", 2)
    first = h.launcher.trees[0]
    assert first.terminated
    assert first.closed
    timeout = h.records("attempt")[0]
    assert (timeout["outcome"], timeout["timeout_s"]) == ("timeout", 15)


def test_a_launch_failure_stops_the_batch(harness: Callable[..., Harness]) -> None:
    h = harness(Scenario(fail_launch=True), Scenario())
    results = h.run([planned("100"), planned("101")])
    assert [r.result for r in results] == ["incomplete", "not run"]
    (attempt,) = h.records("attempt")
    assert attempt["outcome"] == "launch_failed"
    assert "JobSetupError" in str(attempt["detail"])


# --- Interruption and cleanup ------------------------------------------------------------------


def test_ctrl_c_ends_the_tree_removes_only_this_vods_raw_chat_and_stops(
    harness: Callable[..., Harness],
) -> None:
    h = harness(Scenario(exit_code=None, interrupt_at_poll=2, raw_chat_bytes=10), Scenario())
    other_raw = raw_chat_path(h.data_dir, "555")
    other_raw.parent.mkdir(parents=True, exist_ok=True)
    other_raw.write_bytes(b"another VOD's file")
    kept = chat_path(h.data_dir, "100")
    kept.parent.mkdir(parents=True, exist_ok=True)
    kept.write_bytes(b"finished output")

    results = h.run([planned("100"), planned("101")])

    assert [r.result for r in results] == ["incomplete", "not run"]
    tree = h.launcher.trees[0]
    assert tree.terminated
    assert tree.closed
    (attempt,) = h.records("attempt")
    assert (attempt["outcome"], attempt["raw_chat_removed"]) == ("interrupted", True)
    assert not raw_chat_path(h.data_dir, "100").exists()
    assert other_raw.read_bytes() == b"another VOD's file"
    assert kept.read_bytes() == b"finished output"


def test_ctrl_c_between_vods_stops_the_batch(harness: Callable[..., Harness]) -> None:
    h = harness(Scenario(), Scenario())
    deps = h.deps()

    def sleep(seconds: float) -> None:
        if seconds != 1.0:
            raise KeyboardInterrupt
        h.time.sleep(seconds)

    deps.sleep = sleep
    results = rp.run_batch(
        [planned("100"), planned("101")],
        data_dir=h.data_dir,
        run_dir=h.data_dir / "pilot" / "runs" / "r1",
        command_for=lambda i: ["ingest", i],
        limits=rp.Limits(poll_s=1.0),
        hours_by_id={},
        redact=rp.Redactor(),
        log=h.log,
        deps=deps,
    )
    assert [r.result for r in results] == ["complete", "not run"]
    assert h.records("interrupted")


def test_temporary_files_left_behind_are_reported_not_deleted(
    harness: Callable[..., Harness],
) -> None:
    h = harness(Scenario(exit_code=None, interrupt_at_poll=1))
    partial = raw_audio_dir(h.data_dir) / "100.download.ts.part"
    partial.parent.mkdir(parents=True, exist_ok=True)
    partial.write_bytes(b"resumable")
    h.run([planned("100")])
    (attempt,) = h.records("attempt")
    assert attempt["temp_files_left"] == ["100.download.ts.part"]
    assert partial.exists()


# --- Disk and memory ---------------------------------------------------------------------------


def test_estimates_cover_audio_and_chat_with_the_safety_factor() -> None:
    rates = rp.Rates()
    empty: dict[str, CatalogEntry | None] = dict.fromkeys(STEPS)
    full = int(((rates.raw_audio + rates.wav + rates.raw_chat + rates.chat) * 3 + rates.fixed) * 2)
    assert rp.estimate_bytes(3, empty, rates, 2) == full
    done = {s: entry(s, "1", "unavailable" if s == "audio" else "ok") for s in STEPS}
    assert rp.estimate_bytes(3, done, rates, 2) == int(rates.fixed * 2)  # type: ignore[arg-type]


def test_rates_are_measured_from_vods_on_disk(tmp_path: Path) -> None:
    for video_id, hours in (("100", 2.0), ("101", 4.0)):
        for path, per_hour in (
            (raw_audio_dir(tmp_path) / f"{video_id}.ts", 70),
            (normalized_audio_path(tmp_path, video_id), 115),
            (chat_path(tmp_path, video_id), 10),
        ):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"\0" * int(per_hour * hours * 1000))
    rates = rp.measured_rates(tmp_path, {"100": 2.0, "101": 4.0, "999": 1.0}, [5000.0])
    assert (rates.raw_audio, rates.wav, rates.chat, rates.raw_chat) == (70000, 115000, 10000, 5000)
    assert rp.measured_rates(tmp_path, {}, []) == rp.Rates()


def test_a_vod_that_would_cross_the_margin_is_not_started(harness: Callable[..., Harness]) -> None:
    h = harness(Scenario(), disk=26 * GB)
    results = h.run([planned("100", hours=2), planned("101")])
    assert [r.result for r in results] == ["refused_disk", "not run"]
    assert h.launcher.commands == []
    (refused,) = h.records("refused")
    assert refused["disk_free"] == 26 * GB
    assert int(refused["estimated_bytes"]) > 1 * GB  # type: ignore[call-overload]


def test_low_disk_during_a_run_stops_it_and_the_batch(harness: Callable[..., Harness]) -> None:
    h = harness(Scenario(exit_code=None), Scenario())
    h.disk[:] = [100 * GB, 100 * GB, 100 * GB, 24 * GB]
    results = h.run([planned("100"), planned("101")])
    assert [r.result for r in results] == ["incomplete", "not run"]
    (attempt,) = h.records("attempt")
    assert attempt["outcome"] == "stopped_disk"
    assert attempt["disk_free_min"] == 24 * GB
    assert h.launcher.trees[0].terminated


def test_low_system_memory_stops_the_run(harness: Callable[..., Harness]) -> None:
    h = harness(Scenario(exit_code=None), Scenario())
    h.memory[:] = [4 * GB, int(0.5 * GB)]
    results = h.run([planned("100"), planned("101")])
    assert [r.result for r in results] == ["incomplete", "not run"]
    (attempt,) = h.records("attempt")
    assert attempt["outcome"] == "stopped_memory"
    assert attempt["system_memory_free_min"] == int(0.5 * GB)


def test_job_memory_over_the_limit_stops_the_run(harness: Callable[..., Harness]) -> None:
    h = harness(Scenario(exit_code=None, peak=[1 * GB, 9 * GB]))
    h.run([planned("100")], max_job_memory=8 * GB)
    (attempt,) = h.records("attempt")
    assert (attempt["outcome"], attempt["peak_job_memory"]) == ("stopped_memory", 9 * GB)
    assert h.launcher.caps == [10 * GB]  # the job's hard cap backs up the check


def test_peak_memory_and_raw_chat_size_are_recorded(harness: Callable[..., Harness]) -> None:
    h = harness(Scenario(polls=3, peak=[1 * GB, 3 * GB, 2 * GB, 3 * GB], raw_chat_bytes=4096))
    h.run([planned("100")])
    (attempt,) = h.records("attempt")
    assert attempt["peak_job_memory"] == 3 * GB
    assert attempt["raw_chat_peak_bytes"] == 4096
    assert attempt["raw_chat_removed"] is True  # ingest would delete it; the fake doesn't


# --- Records and privacy -----------------------------------------------------------------------


def test_logs_and_records_hold_no_secrets(harness: Callable[..., Harness]) -> None:
    url = "https://usher.ttvnw.net/vod/x.m3u8?sig=deadbeef&token=%7Bsecret%7D"
    h = harness(
        Scenario(
            exit_code=2,
            lines=[("err", f"warning {url} {FAKE_SECRET}"), ("out", f"salt is {FAKE_SALT}")],
            statuses=failed("audio", f"MediaLengthError: bad {url} key {FAKE_SECRET}"),
        )
    )
    h.run([planned("100")])
    stored = [h.log.path.read_text(encoding="utf-8")]
    stored += [
        p.read_text(encoding="utf-8") for p in (h.data_dir / "pilot" / "runs" / "r1").glob("*.log")
    ]
    assert len(stored) == 2
    for text in stored:
        for leaked in (FAKE_SECRET, FAKE_SALT, "deadbeef", "%7Bsecret"):
            assert leaked not in text
    assert "x.m3u8?[redacted]" in stored[1]


def test_every_attempt_record_has_the_required_fields(harness: Callable[..., Harness]) -> None:
    h = harness(Scenario())
    h.run([planned("100")])
    (attempt,) = h.records("attempt")
    for key in (
        "video_id",
        "login",
        "attempt",
        "started_at",
        "ended_at",
        "duration_s",
        "exit_code",
        "outcome",
        "steps",
        "errors",
        "disk_free_before",
        "disk_free_after",
        "peak_job_memory",
        "output_bytes",
        "log_file",
    ):
        assert key in attempt
    assert attempt["steps"] == dict.fromkeys(STEPS, "ok")


# --- Lock and keep-awake -----------------------------------------------------------------------


def test_the_lock_admits_one_runner_and_is_released(tmp_path: Path) -> None:
    path = tmp_path / "pilot" / "run.lock"
    with rp.run_lock(path), pytest.raises(rp.RunLockedError), rp.run_lock(path):
        pass
    with rp.run_lock(path):
        pass


def test_the_os_releases_the_lock_of_a_runner_that_dies(tmp_path: Path) -> None:
    path = tmp_path / "run.lock"
    code = (
        "import os, sys; sys.path.insert(0, sys.argv[1]); import run_pilot as rp\n"
        "cm = rp.run_lock(rp.Path(sys.argv[2])); cm.__enter__(); os._exit(0)\n"
    )
    subprocess.run(
        [sys.executable, "-c", code, str(rp.REPO_ROOT / "scripts"), str(path)], check=True
    )
    with rp.run_lock(path):
        pass


def test_keep_awake_is_set_and_released(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []
    monkeypatch.setattr(rp, "_set_execution_state", calls.append)
    with pytest.raises(RuntimeError), rp.keep_awake():
        raise RuntimeError("boom")
    if WINDOWS:
        assert calls == [rp.ES_CONTINUOUS | rp.ES_SYSTEM_REQUIRED, rp.ES_CONTINUOUS]
    else:
        assert calls == []


# --- Real process trees ------------------------------------------------------------------------

CHILD = """\
import os, pathlib, subprocess, sys, time
out = pathlib.Path(sys.argv[1])
(out / "child-started").write_text("1")
grandchild = (
    "import os, pathlib, sys, time; pathlib.Path(sys.argv[1]).write_text(str(os.getpid()));"
    " time.sleep(120)"
)
subprocess.Popen([sys.executable, "-c", grandchild, str(out / "grandchild.pid")])
(out / "child.pid").write_text(str(os.getpid()))
print("child ready https://example.invalid/x?token=SECRETVALUE", flush=True)
if len(sys.argv) > 2:
    hold = bytearray(int(sys.argv[2]))
time.sleep(120)
"""


def alive(pid: int) -> bool:
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = ctypes.c_void_p
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            kernel32.GetExitCodeProcess(ctypes.c_void_p(handle), ctypes.byref(code))
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(ctypes.c_void_p(handle))
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    stat = Path(f"/proc/{pid}/stat")
    return not (stat.is_file() and stat.read_text().split(")")[-1].split()[0] == "Z")


def wait_for(condition: Callable[[], bool], timeout: float = 30) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return condition()


def pids(directory: Path) -> tuple[int, int]:
    assert wait_for(
        lambda: (directory / "grandchild.pid").is_file() and (directory / "child.pid").is_file()
    )
    time.sleep(0.1)
    return int((directory / "child.pid").read_text()), int(
        (directory / "grandchild.pid").read_text()
    )


@pytest.fixture
def child_script(tmp_path: Path) -> Path:
    path = tmp_path / "child.py"
    path.write_text(CHILD, encoding="utf-8")
    return path


def start(
    child_script: Path, out: Path, *extra: str, sink: rp.Sink | None = None
) -> rp.ProcessTree:
    out.mkdir(exist_ok=True)
    return rp.ProcessTree.start(
        [sys.executable, str(child_script), str(out), *extra],
        cwd=out,
        env=dict(os.environ),
        sink=sink or (lambda stream, line: None),
    )


def test_terminate_ends_the_child_and_its_grandchild(child_script: Path, tmp_path: Path) -> None:
    lines: list[tuple[str, str]] = []
    tree = start(child_script, tmp_path / "out", sink=lambda s, line: lines.append((s, line)))
    child, grandchild = pids(tmp_path / "out")
    # The child prints after writing its PID file; wait for the line so a slow child isn't
    # killed before it prints (that race made this test fail intermittently).
    ready = ("out", "child ready https://example.invalid/x?token=SECRETVALUE")
    assert wait_for(lambda: ready in lines), "the child never printed its line"
    assert alive(child)
    assert alive(grandchild)
    if WINDOWS:
        active = tree.active_processes()
        assert active is not None
        assert active >= 2
    tree.terminate()
    tree.close()
    assert wait_for(lambda: not alive(child)), "child survived"
    assert wait_for(lambda: not alive(grandchild)), "grandchild survived"
    assert ready in lines


def test_a_timed_out_attempt_ends_the_real_tree_and_redacts_its_output(
    child_script: Path, tmp_path: Path
) -> None:
    out = tmp_path / "out"
    out.mkdir()
    data_dir = tmp_path / "data"
    attempt = rp.run_attempt(
        planned("100", hours=0),
        1,
        data_dir=data_dir,
        command=[sys.executable, str(child_script), str(out)],
        log_path=tmp_path / "attempt.log",
        limits=rp.Limits(timeout_base_s=5, poll_s=0.2, min_free_bytes=0, min_free_memory=0),
        rates=rp.Rates(),
        redact=rp.Redactor(),
        deps=rp.Deps(launch=rp.default_launch(out, dict(os.environ))),
    )
    child, grandchild = pids(out)
    assert attempt.outcome == "timeout"
    assert attempt.state_after == "incomplete"
    assert wait_for(lambda: not alive(child))
    assert wait_for(lambda: not alive(grandchild))
    log = (tmp_path / "attempt.log").read_text(encoding="utf-8")
    assert "child ready https://example.invalid/x?[redacted]" in log
    assert "SECRETVALUE" not in log
    if WINDOWS:
        assert attempt.peak_job_memory is not None
        assert attempt.peak_job_memory > 0


@pytest.mark.skipif(not WINDOWS, reason="Windows Job Object behavior")
def test_a_failed_job_assignment_kills_the_child_before_it_runs(
    child_script: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[int] = []

    def refuse(job: object, pid: int) -> None:
        seen.append(pid)
        raise OSError("access denied (test)")

    monkeypatch.setattr(rp, "_assign_process", refuse)
    with pytest.raises(rp.JobSetupError, match="access denied"):
        start(child_script, tmp_path / "out")
    (pid,) = seen
    assert wait_for(lambda: not alive(pid))
    time.sleep(1)
    assert not (tmp_path / "out" / "child-started").exists(), "the child ran before its job"


@pytest.mark.skipif(not WINDOWS, reason="Windows Job Object behavior")
def test_the_child_is_suspended_until_it_is_in_the_job(
    child_script: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert sys.platform == "win32"  # for the type checker; skipped elsewhere
    order: list[str] = []
    real_assign, real_resume = rp._assign_process, rp._resume_process

    def assign(job: object, pid: int) -> None:
        time.sleep(1)  # the child would have started by now if it weren't suspended
        order.append(f"assign started={(tmp_path / 'out' / 'child-started').exists()}")
        real_assign(job, pid)

    def resume(pid: int) -> None:
        order.append("resume")
        real_resume(pid)

    monkeypatch.setattr(rp, "_assign_process", assign)
    monkeypatch.setattr(rp, "_resume_process", resume)
    tree = start(child_script, tmp_path / "out")
    try:
        assert order == ["assign started=False", "resume"]
        assert wait_for(lambda: (tmp_path / "out" / "child-started").exists())
    finally:
        tree.terminate()
        tree.close()


@pytest.mark.skipif(not WINDOWS, reason="Windows Job Object behavior")
def test_the_tree_dies_with_a_runner_that_is_killed(child_script: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    runner = (
        "import os, sys, time; sys.path.insert(0, sys.argv[1]); import run_pilot as rp\n"
        "out = rp.Path(sys.argv[3])\n"
        "rp.ProcessTree.start([sys.executable, sys.argv[2], str(out)], cwd=out,"
        " env=dict(os.environ), sink=lambda s, l: None)\n"
        "deadline = time.monotonic() + 30\n"
        "while not (out / 'grandchild.pid').exists() and time.monotonic() < deadline:\n"
        "    time.sleep(0.05)\n"
        "time.sleep(0.2)\n"
        "os._exit(3)  # no cleanup code runs\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", runner, str(rp.REPO_ROOT / "scripts"), str(child_script), str(out)],
        timeout=120,
        check=False,
    )
    assert done.returncode == 3
    child, grandchild = pids(out)
    assert wait_for(lambda: not alive(child)), "child outlived the runner"
    assert wait_for(lambda: not alive(grandchild)), "grandchild outlived the runner"


@pytest.mark.skipif(not WINDOWS, reason="Windows Job Object accounting")
def test_peak_memory_covers_the_whole_tree(child_script: Path, tmp_path: Path) -> None:
    tree = start(child_script, tmp_path / "out", str(300 * rp.MB))
    try:
        pids(tmp_path / "out")
        assert wait_for(lambda: (tree.peak_memory() or 0) > 300 * rp.MB)
    finally:
        tree.terminate()
        tree.close()


@pytest.mark.skipif(not WINDOWS, reason="Windows handles")
def test_the_job_handle_is_closed_once(
    child_script: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert sys.platform == "win32"  # for the type checker; skipped elsewhere
    closed: list[object] = []
    real_close = rp._close_handle

    def spy(handle: object) -> None:
        closed.append(handle)
        real_close(handle)

    tree = start(child_script, tmp_path / "out")
    pids(tmp_path / "out")
    job = tree._job
    monkeypatch.setattr(rp, "_close_handle", spy)
    tree.terminate()
    tree.close()
    tree.close()
    assert closed.count(job) == 1
    assert tree._job is None


# --- Command line ------------------------------------------------------------------------------


@pytest.fixture
def cli_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, selections: list[sp.Selection]
) -> Path:
    data_dir = tmp_path / "data"
    monkeypatch.setenv("AUTOCLIPPER_DATA_DIR", str(data_dir))
    monkeypatch.setenv("TWITCH_CLIENT_ID", "zz-fake-client-id")
    monkeypatch.setenv("TWITCH_CLIENT_SECRET", FAKE_SECRET)
    monkeypatch.setenv("AUTOCLIPPER_CHAT_HASH_SALT", FAKE_SALT)
    for s in selections:
        sp.save_selection(s, data_dir)
        sp.write_broadcasters(s.broadcasters, data_dir)
    (tmp_path / "channels.yaml").write_text(
        "channels:\n  - login: chan_a\n  - login: chan_b\n", encoding="utf-8"
    )
    monkeypatch.setattr(rp, "prerequisites", lambda settings: [("everything", True, "faked")])
    monkeypatch.setattr(rp, "_set_execution_state", lambda flags: None)
    return data_dir


def files_under(path: Path) -> dict[str, bytes]:
    return {str(p.relative_to(path)): p.read_bytes() for p in path.rglob("*") if p.is_file()}


def test_dry_run_plans_one_vod_and_starts_nothing(
    cli_env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_launch(*args: object, **kwargs: object) -> rp.ProcessTree:
        raise AssertionError("a dry run must not start anything")

    monkeypatch.setattr(rp.ProcessTree, "start", no_launch)
    before = files_under(cli_env)
    result = CliRunner().invoke(
        rp.app, ["--dry-run", "--ids", "100", "--channels", str(tmp_path / "channels.yaml")]
    )
    assert result.exit_code == ExitCode.SUCCESS, result.output
    assert "1 VOD(s) in this order" in result.stdout
    assert "100 chan_a (9000000001) primary" in result.stdout
    assert "Dry run: nothing started; 1 VOD(s) would be processed." in result.stdout
    assert files_under(cli_env) == before


def test_dry_run_with_missing_prerequisites_says_so(
    cli_env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rp, "prerequisites", lambda s: [("ffmpeg", False, "not on PATH")])
    result = CliRunner().invoke(
        rp.app, ["--dry-run", "--ids", "100", "--channels", str(tmp_path / "channels.yaml")]
    )
    assert result.exit_code == ExitCode.USER_ERROR
    assert "[MISSING] ffmpeg: not on PATH" in result.stdout


def test_the_command_refuses_an_unapproved_vod(cli_env: Path, tmp_path: Path) -> None:
    result = CliRunner().invoke(
        rp.app, ["--dry-run", "--ids", "999", "--channels", str(tmp_path / "channels.yaml")]
    )
    assert result.exit_code == ExitCode.USER_ERROR
    assert "999" in result.output


def test_the_command_runs_one_vod_and_writes_a_clean_record(
    cli_env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launcher = FakeLauncher(cli_env, Scenario(lines=[("err", f"x {FAKE_SECRET}")]))
    monkeypatch.setattr(rp, "default_launch", lambda cwd, env: launcher)
    result = CliRunner().invoke(
        rp.app,
        [
            "--ids",
            "100",
            "--channels",
            str(tmp_path / "channels.yaml"),
            "--poll-s",
            "0.5",
            # This test is about the command and its run record, so it shouldn't depend on the
            # host's free disk and memory. The guards are tested with fakes above.
            "--min-free-gb",
            "0",
            "--min-free-memory-gb",
            "0",
        ],
    )
    assert result.exit_code == ExitCode.SUCCESS, result.output
    assert len(launcher.commands) == 1
    assert launcher.commands[0][-2:] == ["ingest", "100"]
    (run_log,) = (cli_env / "pilot" / "runs").glob("*.jsonl")
    kinds = [
        json.loads(line)["record"] for line in run_log.read_text(encoding="utf-8").splitlines()
    ]
    assert kinds == ["start", "attempt", "summary"]
    everything = "".join(
        p.read_text(encoding="utf-8") for p in (cli_env / "pilot" / "runs").rglob("*.*")
    )
    assert FAKE_SECRET not in everything
    assert FAKE_SALT not in everything


def test_the_runner_follows_backup_overrides(selections: list[sp.Selection]) -> None:
    a, b = selections
    a = select("chan_a", A_ID, [vod("100", 20), vod("101", 10), vod("102", 15), vod("103", 18)])
    assert picks([a]) == {"100": "primary", "101": "primary", "102": "backup"}
    when = NOW + timedelta(days=1)
    changed = sp.apply_overrides(
        a,
        [
            sp.Override("102", "chan_a", "eligible", "weak clips", when),
            sp.Override("103", "chan_a", "backup", "stronger clips", when),
        ],
    )
    replayed = sp.selection_from_json(json.loads(json.dumps(sp.selection_to_json(changed))))
    for current in (changed, replayed):
        (backup,) = plan([current, b], ["103"], allow_backup=True)
        assert (backup.video_id, backup.designation) == ("103", "backup")
        with pytest.raises(rp.PlanError, match="103 is a backup"):
            plan([current, b], ["103"])
        with pytest.raises(rp.PlanError, match="not a primary or backup of these selections: 102"):
            plan([current, b], ["102"], allow_backup=True)
