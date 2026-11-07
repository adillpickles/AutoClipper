"""Tests for scripts/select_pilot_vods.py: rules, picking, files and the command line.

Helix is the real client on ``httpx.MockTransport``; real network connections are blocked.
"""

import json
import random
import socket
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pyarrow.parquet as pq
import pytest
import select_pilot_vods as sp
from pydantic import SecretStr
from typer.testing import CliRunner

from autoclipper import tables
from autoclipper.cli import ExitCode
from autoclipper.config import TwitchCredentials
from autoclipper.ingest.twitch_api import Broadcaster, HelixClient, Listing, MutedSegment, Vod

NOW = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
CHANNEL_ID = "9000000001"
FAKE_SECRET = "zz-fake-client-secret-never-printed"
FAKE_TOKEN = "zz-fake-app-token-never-printed"


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail any test that opens a real network connection."""

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError(f"network access attempted: {args!r}")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


def test_the_network_guard_blocks_real_connections() -> None:
    with pytest.raises(AssertionError, match="network access attempted"):
        socket.create_connection(("127.0.0.1", 9), timeout=1)
    with socket.socket() as sock, pytest.raises(AssertionError, match="network access attempted"):
        sock.connect(("127.0.0.1", 9))


def vod(
    video_id: str,
    *,
    age_days: float = 10.0,
    hours: float = 6.0,
    broadcaster_id: str = CHANNEL_ID,
    language: str = "en",
    muted: tuple[tuple[float, float], ...] = (),
    type: str = "archive",
    views: int = 1000,
) -> Vod:
    return Vod(
        video_id=video_id,
        broadcaster_id=broadcaster_id,
        stream_id=f"s{video_id}",
        title=f"Stream {video_id}",
        created_at=NOW - timedelta(days=age_days),
        duration_s=hours * 3600,
        type=type,
        language=language,
        view_count=views,
        muted_segments=tuple(MutedSegment(offset_s=o, duration_s=d) for o, d in muted),
        game_ids=None,
        fetched_at=NOW,
    )


def channel(
    login: str = "chan_a", broadcaster_id: str = CHANNEL_ID, kind: str = "partner"
) -> Broadcaster:
    return Broadcaster(broadcaster_id, login, login.title(), kind, NOW)


def assess(v: Vod, *, kind: str = "partner", **params: object) -> sp.Assessment:
    return sp.assess(v, channel(kind=kind), NOW, sp.Params(**params))  # type: ignore[arg-type]


def run_select(vods: list[Vod], *, kind: str = "partner", **params: object) -> sp.Selection:
    b = channel(kind=kind)
    p = sp.Params(**params)  # type: ignore[arg-type]
    return sp.select([b], {b.login: Listing(tuple(vods), truncated=False)}, NOW, p)


def decisions(selection: sp.Selection) -> dict[str, tuple[str, int | None]]:
    return {a.vod.video_id: (a.decision, a.pick) for a in selection.assessments}


# --- Hard rules --------------------------------------------------------------------------------


def test_exactly_seven_days_old_is_eligible() -> None:
    assert assess(vod("1", age_days=7)).eligible


def test_one_second_under_seven_days_is_excluded() -> None:
    a = assess(vod("1", age_days=7 - 1 / 86400))
    assert not a.eligible
    assert a.failed_rules == ("under 7 days old",)


def test_the_maturity_rule_cannot_be_lowered() -> None:
    with pytest.raises(ValueError, match="below 7"):
        sp.Params(min_age_days=6.99)
    assert sp.Params(min_age_days=10).min_age_days == 10


SECOND = 1 / 86400


@pytest.mark.parametrize(
    ("kind", "age_days", "risk"),
    [
        # Partner (60 d): low risk while at least 3 days are left.
        ("partner", 57, "low"),
        ("partner", 57 + SECOND, "high"),
        ("partner", 60, "past estimate"),
        # Affiliate (14 d): a 7-day-old VOD has 7 days left; at 11 days, 3 are left.
        ("affiliate", 7, "low"),
        ("affiliate", 11, "low"),
        ("affiliate", 11 + SECOND, "high"),
        ("affiliate", 20, "past estimate"),
        # No type (7 d): a VOD old enough to qualify is past its estimate if Helix still lists it.
        ("", 7, "past estimate"),
    ],
)
def test_expiry_is_a_risk_estimate_not_an_exclusion(kind: str, age_days: float, risk: str) -> None:
    a = assess(vod("1", age_days=age_days), kind=kind)
    assert a.eligible
    assert a.failed_rules == ()
    assert a.expiry_risk == risk
    assert a.preferences["expiry"] is (risk == "low")


def test_the_expiry_margin_sets_where_risk_becomes_high() -> None:
    assert assess(vod("1", age_days=50), expiry_margin_days=10).expiry_risk == "low"
    assert assess(vod("1", age_days=50 + SECOND), expiry_margin_days=10).expiry_risk == "high"
    with pytest.raises(ValueError, match="can't be negative"):
        sp.Params(expiry_margin_days=-1)


def test_unknown_channel_type_has_unknown_expiry_and_says_so() -> None:
    a = assess(vod("1"), kind="unexpected-type")
    assert a.eligible
    assert (a.retention_days, a.days_left, a.expiry_risk) == (None, None, "unknown")
    assert a.preferences["expiry"] is None
    assert "unknown: muted, expiry" in sp._notes(a)
    assert sp._expiry_cell(a) == "unknown (channel type not recognized)"


@pytest.mark.parametrize(
    ("kind", "age_days", "cell"),
    [
        ("partner", 10, "50.0 d left, low risk"),
        ("partner", 58, "2.0 d left, high risk"),
        ("affiliate", 16, "past estimate by 2.0 d, still listed"),
    ],
)
def test_expiry_cell_states_the_estimate(kind: str, age_days: float, cell: str) -> None:
    assert sp._expiry_cell(assess(vod("1", age_days=age_days), kind=kind)) == cell


@pytest.mark.parametrize("risk_age", [58, 60, 61])
def test_vods_at_expiry_risk_come_after_low_risk_ones(risk_age: float) -> None:
    risky = vod("1", age_days=risk_age, muted=((0, 60),))
    safe = vod("2", age_days=40, muted=((0, 60),))  # same misses otherwise (age > 30)
    result = run_select([risky, safe], per_channel=1, backups=1)
    assert decisions(result) == {"2": ("primary", 1), "1": ("backup", 2)}
    assert "expiry risk not low for picks 1" in sp.render_table(result)


@pytest.mark.parametrize(
    ("changes", "rule"),
    [
        ({"type": "highlight"}, "not a past broadcast (type 'highlight')"),
        ({"hours": 0}, "no duration"),
        ({"broadcaster_id": "9000000099"}, "listed under another channel"),
    ],
)
def test_other_hard_rules(changes: dict[str, object], rule: str) -> None:
    a = assess(vod("1", **changes))  # type: ignore[arg-type]
    assert rule in a.failed_rules


def test_excluded_vods_are_never_picked_even_when_a_channel_is_short() -> None:
    result = run_select([vod("1", age_days=3), vod("2", type="highlight"), vod("3", age_days=12)])
    assert decisions(result) == {
        "1": ("excluded", None),
        "2": ("excluded", None),
        "3": ("primary", 1),
    }
    table = sp.render_table(result)
    assert "only 1 of 5 primaries" in table
    assert "only 0 of 2 backups" in table


# --- Unknown and reported values ---------------------------------------------------------------


def test_no_reported_muted_segments_is_unknown_not_zero() -> None:
    a = assess(vod("1"))
    assert a.muted_fraction is None
    assert a.preferences["muted"] is None
    assert sp._muted_cell(a) == "unknown (none reported)"


def test_muted_share_merges_overlaps_and_stays_inside_the_vod() -> None:
    # 1 h VOD: [0, 600) and [300, 900) overlap -> 900 s; [3400, 3800) is cut at 3600 -> 200 s.
    v = vod("1", hours=1, muted=((0, 600), (300, 600), (3400, 400)))
    assert sp.muted_fraction(v) == pytest.approx(1100 / 3600)


@pytest.mark.parametrize(("muted_s", "met"), [(0.1 * 21600, True), (0.25 * 21600, False)])
def test_reported_muted_share_is_compared_with_the_limit(muted_s: float, met: bool) -> None:
    assert assess(vod("1", muted=((0, muted_s),))).preferences["muted"] is met


def test_unknown_muting_never_ranks_above_a_reported_low_share() -> None:
    reported = vod("2", age_days=20, muted=((0, 60),))  # under 1 %, reported
    unknown = vod("1", age_days=20)
    result = run_select([unknown, reported], per_channel=1, backups=0)
    assert decisions(result) == {"1": ("eligible", None), "2": ("primary", 1)}


@pytest.mark.parametrize(("language", "met"), [("en", True), ("de", False), ("other", None)])
def test_language_preference(language: str, met: bool | None) -> None:
    assert assess(vod("1", language=language)).preferences["language"] is met


@pytest.mark.parametrize(
    ("changes", "preference"),
    [({"age_days": 40}, "age"), ({"hours": 1}, "duration"), ({"hours": 13}, "duration")],
)
def test_vods_missing_a_preference_come_after_those_meeting_more(
    changes: dict[str, object], preference: str
) -> None:
    worse = vod("1", **changes)  # type: ignore[arg-type]
    better = vod("2", age_days=12, muted=((0, 60),))
    assert assess(worse).preferences[preference] is False
    result = run_select([worse, better], per_channel=1, backups=1)
    assert decisions(result) == {"2": ("primary", 1), "1": ("backup", 2)}


# --- Picking -----------------------------------------------------------------------------------


def daily_vods() -> list[Vod]:
    # One VOD a day, 8 to 30 days old; IDs grow with recency, like Twitch's.
    return [vod(str(1000 + 30 - age), age_days=age) for age in range(8, 31)]


def test_picks_spread_across_dates_starting_with_the_oldest() -> None:
    result = run_select(daily_vods())
    picks = sorted((a.pick, round(a.age / timedelta(days=1))) for a in result.assessments if a.pick)
    # Oldest (30), newest (8), then the middle of the widest gap; ties go to the lower ID (so 25
    # before 24, 14 before 13, and 22 before 11 at equal gaps of 3 days).
    assert [age for _, age in picks] == [30, 8, 19, 25, 14, 22, 11]
    counts = [a.decision for a in result.assessments]
    assert (counts.count("primary"), counts.count("backup"), counts.count("eligible")) == (5, 2, 16)


def test_the_same_metadata_gives_the_same_decisions_in_any_order() -> None:
    expected = decisions(run_select(daily_vods()))
    for seed in range(5):
        shuffled = daily_vods()
        random.Random(seed).shuffle(shuffled)
        assert decisions(run_select(shuffled)) == expected


def test_a_vod_listed_twice_is_assessed_once() -> None:
    result = run_select([vod("1"), vod("1")])
    assert [a.vod.video_id for a in result.assessments] == ["1"]


# --- Selection files ---------------------------------------------------------------------------


def roundtrip(selection: sp.Selection) -> sp.Selection:
    return sp.selection_from_json(json.loads(json.dumps(sp.selection_to_json(selection))))


def test_a_saved_selection_recomputes_to_the_same_decisions_and_table() -> None:
    original = run_select([*daily_vods(), vod("5", age_days=3), vod("6", muted=((0, 99),))])
    again = roundtrip(original)
    assert decisions(again) == decisions(original)
    assert again.assessments == original.assessments
    assert sp.render_table(again) == sp.render_table(original)


def test_a_selection_file_of_another_format_is_refused() -> None:
    data = sp.selection_to_json(run_select([vod("1")]))
    data["format_version"] = 999
    with pytest.raises(ValueError, match="unsupported selection format"):
        sp.selection_from_json(data)


def test_selection_files_are_never_overwritten(tmp_path: Path) -> None:
    selection = run_select([vod("1")])
    path = sp.save_selection(selection, tmp_path)
    assert path == tmp_path / "pilot" / "selections" / "selection-20261106T120000Z.json"
    content = json.loads(path.read_text(encoding="utf-8"))
    assert content["format_version"] == sp.FORMAT_VERSION
    assert set(content["source"]) == {"commit", "dirty"}
    with pytest.raises(FileExistsError):
        sp.save_selection(selection, tmp_path)


def test_broadcasters_table_has_the_data_model_schema_and_replaces_rows(tmp_path: Path) -> None:
    sp.write_broadcasters(
        [channel("chan_a"), channel("chan_b", "9000000002", "affiliate")], tmp_path
    )
    renamed = Broadcaster(CHANNEL_ID, "chan_a", "Renamed", "partner", NOW + timedelta(hours=1))
    path = sp.write_broadcasters([renamed], tmp_path)

    assert path == tmp_path / "processed" / "broadcasters.parquet"
    table = pq.read_table(path)
    assert table.schema.equals(tables.BROADCASTERS)
    rows = table.to_pylist()
    assert [(r["broadcaster_id"], r["display_name"]) for r in rows] == [
        (CHANNEL_ID, "Renamed"),
        ("9000000002", "Chan_B"),
    ]
    assert not list(path.parent.glob("*.part"))


def test_write_list_writes_approved_links_oldest_first(tmp_path: Path) -> None:
    result = run_select(daily_vods())
    picks = sorted((a for a in result.assessments if a.pick), key=lambda a: a.pick or 0)
    newest, oldest, backup = picks[1].vod.video_id, picks[0].vod.video_id, picks[5].vod.video_id
    out = tmp_path / "pilot_vods.txt"

    approved = [newest, f"https://www.twitch.tv/videos/{oldest}", backup]
    written = sp.write_list([result], approved, out, channels=["chan_a"])

    assert written == [oldest, backup, newest]
    assert out.read_bytes() == b"".join(
        f"https://www.twitch.tv/videos/{i}\n".encode() for i in written
    )


@pytest.mark.parametrize("which", ["eligible", "excluded", "unknown", "repeated"])
def test_write_list_refuses_ids_that_were_not_picked(tmp_path: Path, which: str) -> None:
    result = run_select([*daily_vods(), vod("5", age_days=3)])
    by = {a.decision: a.vod.video_id for a in result.assessments}
    ids = {
        "eligible": [by["eligible"]],
        "excluded": ["5"],
        "unknown": ["123456"],
        "repeated": [by["primary"], by["primary"]],
    }[which]
    out = tmp_path / "pilot_vods.txt"
    with pytest.raises(ValueError, match=r"not a primary or backup|repeated"):
        sp.write_list([result], ids, out, channels=["chan_a"])
    assert not out.exists()


def test_write_list_never_replaces_an_existing_list(tmp_path: Path) -> None:
    result = run_select([vod("1")])
    out = tmp_path / "pilot_vods.txt"
    out.write_text("keep me\n", encoding="utf-8")
    with pytest.raises(FileExistsError):
        sp.write_list([result], ["1"], out, channels=["chan_a"])
    assert out.read_text(encoding="utf-8") == "keep me\n"


# --- Combining selections ----------------------------------------------------------------------

CHAN_B = "9000000002"


def select_channel(b: Broadcaster, vods: list[Vod]) -> sp.Selection:
    return sp.select([b], {b.login: Listing(tuple(vods), truncated=False)}, NOW, sp.Params())


def two_selections() -> tuple[sp.Selection, sp.Selection]:
    """chan_a and chan_b fetched separately, with interleaved recording dates."""
    first = select_channel(channel("chan_a"), [vod("100", age_days=20), vod("101", age_days=10)])
    later = select_channel(
        channel("chan_b", CHAN_B),
        [
            vod("200", age_days=25, broadcaster_id=CHAN_B),
            vod("201", age_days=15, broadcaster_id=CHAN_B),
        ],
    )
    return first, later


def test_write_list_combines_selections_oldest_first(tmp_path: Path) -> None:
    out = tmp_path / "pilot_vods.txt"
    written = sp.write_list(
        list(two_selections()), ["101", "200", "100", "201"], out, channels=["chan_a", "chan_b"]
    )
    assert written == ["200", "100", "201", "101"]  # 25, 20, 15 and 10 days old
    assert out.read_text(encoding="utf-8").splitlines() == [
        f"https://www.twitch.tv/videos/{i}" for i in written
    ]


def test_write_list_writes_only_the_approved_ids_of_several_selections(tmp_path: Path) -> None:
    out = tmp_path / "pilot_vods.txt"
    written = sp.write_list(list(two_selections()), ["201"], out, channels=["chan_a", "chan_b"])
    assert written == ["201"]


def test_write_list_refuses_a_vod_picked_in_two_selections(tmp_path: Path) -> None:
    same = select_channel(channel("chan_a"), [vod("100")])
    again = select_channel(channel("chan_a"), [vod("100")])
    out = tmp_path / "pilot_vods.txt"
    with pytest.raises(ValueError, match="100 is a pick of more than one selection"):
        sp.write_list([same, again], ["100"], out, channels=["chan_a"])
    assert not out.exists()


def test_write_list_refuses_an_id_in_none_of_the_selections(tmp_path: Path) -> None:
    out = tmp_path / "pilot_vods.txt"
    with pytest.raises(ValueError, match="not a primary or backup of these selections: 999"):
        sp.write_list(list(two_selections()), ["100", "999"], out, channels=["chan_a", "chan_b"])
    assert not out.exists()


def test_write_list_refuses_vods_of_channels_not_in_the_channels_file(tmp_path: Path) -> None:
    out = tmp_path / "pilot_vods.txt"
    with pytest.raises(ValueError, match="channels not in the channels file: chan_b"):
        sp.write_list(list(two_selections()), ["100", "200"], out, channels=["chan_a"])
    assert not out.exists()


def test_a_selection_may_list_a_channel_outside_the_file_if_none_of_its_vods_is_approved(
    tmp_path: Path,
) -> None:
    out = tmp_path / "pilot_vods.txt"
    written = sp.write_list(list(two_selections()), ["100"], out, channels=["chan_a"])
    assert written == ["100"]


def test_write_list_refuses_selections_that_disagree_about_a_channel(tmp_path: Path) -> None:
    first = select_channel(channel("chan_a"), [vod("100")])
    other = select_channel(
        channel("chan_a", CHAN_B), [vod("200", broadcaster_id=CHAN_B)]
    )  # same login, another account
    out = tmp_path / "pilot_vods.txt"
    with pytest.raises(ValueError, match="disagree about chan_a's broadcaster ID"):
        sp.write_list([first, other], ["100"], out, channels=["chan_a"])
    assert not out.exists()


# --- Channels file -----------------------------------------------------------------------------


def test_the_repository_channels_file_loads() -> None:
    logins = sp.load_channel_logins(sp.DEFAULT_CHANNELS)
    assert len(logins) == 5
    assert all(login == login.lower() for login in logins)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("other: 1\n", "no 'channels' list"),
        ("channels: []\n", "no 'channels' list"),
        ("channels:\n  - reason: x\n", r"channels\[0\] has no login"),
        ("channels:\n  - login: a\n  - login: A\n", "repeated logins a"),
    ],
)
def test_malformed_channels_files_are_refused(tmp_path: Path, text: str, message: str) -> None:
    path = tmp_path / "channels.yaml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        sp.load_channel_logins(path)


# --- Command line, against a scripted Helix ----------------------------------------------------


def _helix_duration(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600}h{s % 3600 // 60}m{s % 60}s"


def _helix_video(v: Vod, login: str) -> dict[str, object]:
    return {
        "id": v.video_id,
        "stream_id": v.stream_id,
        "user_id": v.broadcaster_id,
        "user_login": login,
        "user_name": login,
        "title": v.title,
        "description": "",
        "created_at": v.created_at.isoformat().replace("+00:00", "Z"),
        "published_at": v.created_at.isoformat().replace("+00:00", "Z"),
        "url": f"https://www.twitch.tv/videos/{v.video_id}",
        "thumbnail_url": "",
        "viewable": "public",
        "view_count": v.view_count,
        "language": v.language,
        "type": v.type,
        "duration": _helix_duration(v.duration_s),
        "muted_segments": [
            {"offset": s.offset_s, "duration": s.duration_s} for s in v.muted_segments
        ]
        or None,
    }


class ScriptedHelix:
    """MockTransport handler for Get Users and Get Videos; records every request."""

    def __init__(self, channels: list[Broadcaster], vods: dict[str, list[Vod]]) -> None:
        self.channels = channels
        self.vods = vods
        self.requests: list[httpx.Request] = []
        self.fail_videos = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host == "id.twitch.tv":
            return httpx.Response(
                200, json={"access_token": FAKE_TOKEN, "expires_in": 3600, "token_type": "bearer"}
            )
        if request.url.path == "/helix/users":
            wanted = request.url.params.get_list("login")
            data = [
                {
                    "id": b.broadcaster_id,
                    "login": b.login,
                    "display_name": b.display_name,
                    "type": "",
                    "broadcaster_type": b.broadcaster_type,
                    "description": "",
                    "profile_image_url": "",
                    "offline_image_url": "",
                    "view_count": 0,
                    "created_at": "2020-01-01T00:00:00Z",
                }
                for b in self.channels
                if b.login in wanted
            ]
            return httpx.Response(200, json={"data": data})
        if request.url.path == "/helix/videos":
            if self.fail_videos:
                return httpx.Response(400, json={"error": "Bad Request", "message": "nope"})
            user_id = request.url.params["user_id"]
            (b,) = (b for b in self.channels if b.broadcaster_id == user_id)
            videos = [_helix_video(v, b.login) for v in self.vods.get(b.login, [])]
            return httpx.Response(200, json={"data": videos, "pagination": {}})
        raise AssertionError(f"unexpected request {request.method} {request.url}")


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    data_dir = tmp_path / "data"
    monkeypatch.setenv("AUTOCLIPPER_DATA_DIR", str(data_dir))
    monkeypatch.setenv("TWITCH_CLIENT_ID", "zz-fake-client-id")
    monkeypatch.setenv("TWITCH_CLIENT_SECRET", FAKE_SECRET)
    return data_dir


@pytest.fixture
def helix(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Callable[..., ScriptedHelix]:
    def install(channels: list[Broadcaster], vods: dict[str, list[Vod]]) -> ScriptedHelix:
        fake = ScriptedHelix(channels, vods)
        credentials = TwitchCredentials(SecretStr("zz-fake-client-id"), SecretStr(FAKE_SECRET))

        def open_helix() -> HelixClient:
            http = httpx.Client(transport=httpx.MockTransport(fake))
            return HelixClient(credentials, http_client=http, sleep=lambda _: None)

        monkeypatch.setattr(sp, "open_helix", open_helix)
        channels_file = tmp_path / "channels.yaml"
        channels_file.write_text(
            "channels:\n" + "".join(f"  - login: {b.login}\n" for b in channels), encoding="utf-8"
        )
        return fake

    return install


@pytest.fixture
def two_channels() -> tuple[list[Broadcaster], dict[str, list[Vod]]]:
    a, b = channel("chan_a"), channel("chan_b", "9000000002")
    vods = {
        "chan_a": daily_vods(),
        "chan_b": [
            vod(str(2000 + i), age_days=8 + 3 * i, broadcaster_id="9000000002") for i in range(4)
        ],
    }
    return [a, b], vods


@pytest.fixture
def frozen_now(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sp, "utc_now", lambda: NOW)


def test_fetch_records_a_selection_and_the_channels(
    env: Path,
    helix: Callable[..., ScriptedHelix],
    two_channels: tuple[list[Broadcaster], dict[str, list[Vod]]],
    tmp_path: Path,
    frozen_now: None,
) -> None:
    fake = helix(*two_channels)

    result = CliRunner().invoke(sp.app, ["fetch", "--channels", str(tmp_path / "channels.yaml")])

    assert result.exit_code == ExitCode.SUCCESS, result.output
    # Metadata only: one token request, Get Users, one Get Videos per channel.
    calls = [(r.method, r.url.host, r.url.path) for r in fake.requests]
    assert calls == [
        ("POST", "id.twitch.tv", "/oauth2/token"),
        ("GET", "api.twitch.tv", "/helix/users"),
        ("GET", "api.twitch.tv", "/helix/videos"),
        ("GET", "api.twitch.tv", "/helix/videos"),
    ]
    assert all(r.url.params.get("type") == "archive" for r in fake.requests[2:])
    (selection_file,) = (env / "pilot" / "selections").glob("selection-*.json")
    assert (env / "processed" / "broadcasters.parquet").is_file()
    assert "| chan_a | primary #1 |" in result.output
    assert "chan_b: only 4 of 5 primaries" in result.output
    # Nothing secret anywhere.
    for text in (result.output, selection_file.read_text(encoding="utf-8")):
        assert FAKE_SECRET not in text
        assert FAKE_TOKEN not in text
    assert FAKE_SECRET.encode() not in (env / "processed" / "broadcasters.parquet").read_bytes()

    shown = CliRunner().invoke(sp.app, ["show"])
    assert shown.exit_code == ExitCode.SUCCESS, shown.output
    # Logs go to stderr; the table on stdout is the same, recomputed offline.
    assert shown.stdout.split("From ")[0] == result.stdout.split("Selection saved to ")[0]


def test_write_list_command_uses_the_newest_selection(
    env: Path,
    helix: Callable[..., ScriptedHelix],
    two_channels: tuple[list[Broadcaster], dict[str, list[Vod]]],
    tmp_path: Path,
    frozen_now: None,
) -> None:
    helix(*two_channels)
    channels_file = str(tmp_path / "channels.yaml")
    CliRunner().invoke(sp.app, ["fetch", "--channels", channels_file])
    out = tmp_path / "list.txt"
    args = ["write-list", "--channels", channels_file, "--out", str(out)]

    result = CliRunner().invoke(sp.app, [*args, "--approve", "2000", "--approve", "2003"])

    assert result.exit_code == ExitCode.SUCCESS, result.output
    assert out.read_text(encoding="utf-8") == (
        "https://www.twitch.tv/videos/2003\nhttps://www.twitch.tv/videos/2000\n"
    )
    refused = CliRunner().invoke(sp.app, [*args, "--approve", "2000"])
    assert refused.exit_code == ExitCode.USER_ERROR
    assert "exists" in refused.output


def test_write_list_command_combines_saved_selections_offline(
    env: Path,
    helix: Callable[..., ScriptedHelix],
    two_channels: tuple[list[Broadcaster], dict[str, list[Vod]]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helix(*two_channels)
    paths = []
    for minute, login in enumerate(["chan_a", "chan_b"]):
        monkeypatch.setattr(sp, "utc_now", lambda m=minute: NOW + timedelta(minutes=m))
        one = tmp_path / f"{login}.yaml"
        one.write_text(f"channels:\n  - login: {login}\n", encoding="utf-8")
        CliRunner().invoke(sp.app, ["fetch", "--channels", str(one)])
        (path,) = set((env / "pilot" / "selections").glob("*.json")) - set(paths)
        paths.append(path)
    before = {p: p.read_bytes() for p in paths}

    def no_helix() -> HelixClient:
        raise AssertionError("write-list must not call Twitch")

    monkeypatch.setattr(sp, "open_helix", no_helix)
    out = tmp_path / "list.txt"
    selections = [arg for p in paths for arg in ("--selection", str(p))]
    base = ["write-list", *selections, "--out", str(out), "--approve", "2001", "--approve", "1008"]

    mismatch = CliRunner().invoke(sp.app, [*base, "--channels", str(tmp_path / "chan_a.yaml")])
    assert mismatch.exit_code == ExitCode.USER_ERROR
    assert "channels not in the channels file: chan_b" in mismatch.output
    assert not out.exists()

    result = CliRunner().invoke(sp.app, [*base, "--channels", str(tmp_path / "channels.yaml")])

    assert result.exit_code == ExitCode.SUCCESS, result.output
    # 1008 (chan_a) is 22 days old, 2001 (chan_b) 11 days.
    assert out.read_text(encoding="utf-8") == (
        "https://www.twitch.tv/videos/1008\nhttps://www.twitch.tv/videos/2001\n"
    )
    assert {p: p.read_bytes() for p in paths} == before  # the selections are left as they were


def test_fetch_without_credentials_stops_before_any_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTOCLIPPER_DATA_DIR", str(tmp_path / "data"))
    result = CliRunner().invoke(sp.app, ["fetch"])
    assert result.exit_code == ExitCode.USER_ERROR
    assert "TWITCH_CLIENT_ID" in result.output
    assert not (tmp_path / "data").exists()


def test_fetch_reports_an_unknown_channel(
    env: Path, helix: Callable[..., ScriptedHelix], tmp_path: Path
) -> None:
    helix([channel("chan_a")], {})
    channels_file = tmp_path / "missing.yaml"
    channels_file.write_text("channels:\n  - login: chan_a\n  - login: nobody\n", encoding="utf-8")
    result = CliRunner().invoke(sp.app, ["fetch", "--channels", str(channels_file)])
    assert result.exit_code == ExitCode.USER_ERROR
    assert "nobody" in result.output
    assert not (env / "pilot").exists()


def test_fetch_reports_a_helix_failure_as_external(
    env: Path,
    helix: Callable[..., ScriptedHelix],
    two_channels: tuple[list[Broadcaster], dict[str, list[Vod]]],
    tmp_path: Path,
) -> None:
    fake = helix(*two_channels)
    fake.fail_videos = True
    result = CliRunner().invoke(sp.app, ["fetch", "--channels", str(tmp_path / "channels.yaml")])
    assert result.exit_code == ExitCode.EXTERNAL_FAILURE
    assert "Twitch API request failed" in result.output
    assert FAKE_SECRET not in result.output
    assert not (env / "pilot").exists()


def test_fetch_refuses_a_lower_maturity_rule(env: Path) -> None:
    result = CliRunner().invoke(sp.app, ["fetch", "--min-age-days", "6"])
    assert result.exit_code == ExitCode.USER_ERROR
    assert "below 7" in result.output


def test_show_without_a_selection_is_a_user_error(env: Path) -> None:
    result = CliRunner().invoke(sp.app, ["show"])
    assert result.exit_code == ExitCode.USER_ERROR
    assert "run `fetch` first" in result.output


# --- Overrides ---------------------------------------------------------------------------------

APPROVED = NOW + timedelta(days=1)


def override(
    video_id: str, decision: str, login: str = "chan_a", reason: str = "stronger clips"
) -> sp.Override:
    return sp.Override(video_id, login, decision, reason, APPROVED)  # type: ignore[arg-type]


def picks_of(selection: sp.Selection) -> dict[str, tuple[str, int | None]]:
    return {a.vod.video_id: (a.decision, a.pick) for a in selection.assessments if a.pick}


def by_decision(selection: sp.Selection, decision: str) -> list[str]:
    return [a.vod.video_id for a in selection.assessments if a.decision == decision]


@pytest.fixture
def base() -> sp.Selection:
    return run_select([*daily_vods(), vod("5", age_days=3)])  # 5 is under 7 days old


def test_an_override_swaps_a_backup_and_leaves_the_primaries(base: sp.Selection) -> None:
    weak = next(i for i, (d, p) in picks_of(base).items() if d == "backup" and p == 7)
    strong = by_decision(base, "eligible")[0]
    result = sp.apply_overrides(base, [override(weak, "eligible"), override(strong, "backup")])

    after = picks_of(result)
    assert after[strong] == ("backup", 7)
    assert weak not in after
    assert next(a for a in result.assessments if a.vod.video_id == weak).decision == "eligible"
    assert by_decision(result, "primary") == by_decision(base, "primary")
    assert result.overrides == (override(weak, "eligible"), override(strong, "backup"))
    assert f"override ({APPROVED:%Y-%m-%d}): stronger clips" in sp.render_table(result)


def test_an_override_is_saved_and_replays_identically(base: sp.Selection) -> None:
    weak = next(i for i, (d, p) in picks_of(base).items() if d == "backup" and p == 7)
    changed = sp.apply_overrides(
        base, [override(weak, "eligible"), override(by_decision(base, "eligible")[0], "backup")]
    )
    data = json.loads(json.dumps(sp.selection_to_json(changed)))

    assert data["format_version"] == sp.OVERRIDES_FORMAT_VERSION
    assert [o["video_id"] for o in data["overrides"]] == [weak, by_decision(base, "eligible")[0]]
    once, twice = sp.selection_from_json(data), sp.selection_from_json(json.loads(json.dumps(data)))
    assert once.assessments == twice.assessments == changed.assessments
    assert sp.render_table(once) == sp.render_table(changed)


def test_a_selection_without_overrides_keeps_its_old_format(base: sp.Selection) -> None:
    data = sp.selection_to_json(base)
    assert data["format_version"] == sp.FORMAT_VERSION == 1
    assert "overrides" not in data
    assert sp.apply_overrides(base, []) is base


@pytest.mark.parametrize(
    ("make", "message"),
    [
        (lambda s: [override(by_decision(s, "primary")[0], "eligible")], "can't change primary"),
        (lambda s: [override(by_decision(s, "primary")[0], "backup")], "can't change primary"),
        (lambda s: [override("5", "backup")], "only promote an eligible VOD; 5 is excluded"),
        (lambda s: [override("424242", "backup")], "doesn't list"),
        (
            lambda s: [override(by_decision(s, "eligible")[0], "backup", login="chan_b")],
            "the VOD is chan_a's",
        ),
        (
            lambda s: [override(by_decision(s, "backup")[0], "backup")],
            "only promote an eligible VOD",
        ),
        (lambda s: [override(by_decision(s, "eligible")[0], "eligible")], "only demote a backup"),
        (
            lambda s: [override(by_decision(s, "eligible")[0], "backup", reason=" ")],
            "has no reason",
        ),
        (lambda s: [override(by_decision(s, "eligible")[0], "backup")] * 2, "is repeated"),
        (lambda s: [override(by_decision(s, "eligible")[0], "promote")], "unknown decision"),
    ],
    ids=[
        "demote-primary",
        "promote-primary",
        "promote-under-7-days",
        "unknown-vod",
        "wrong-channel",
        "promote-a-backup",
        "demote-an-eligible",
        "no-reason",
        "repeated",
        "unknown-decision",
    ],
)
def test_invalid_overrides_are_refused(
    base: sp.Selection, make: Callable[[sp.Selection], list[sp.Override]], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        sp.apply_overrides(base, make(base))


def test_an_override_needs_a_time_zone(base: sp.Selection) -> None:
    naive_time = datetime(2026, 11, 7)  # noqa: DTZ001 - the case under test
    naive = sp.Override(by_decision(base, "eligible")[0], "chan_a", "backup", "x", naive_time)
    with pytest.raises(ValueError, match="without time zone"):
        sp.apply_overrides(base, [naive])


def with_override_json(base: sp.Selection) -> dict[str, object]:
    eligible = by_decision(base, "eligible")[0]
    return json.loads(
        json.dumps(sp.selection_to_json(sp.apply_overrides(base, [override(eligible, "backup")])))
    )


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda d: d.update(format_version=1), "format version 1 has no overrides"),
        (lambda d: d.pop("overrides"), "format version 2 needs overrides"),
        (lambda d: d.update(overrides=[]), "non-empty list"),
        (lambda d: d["overrides"][0].pop("reason"), "exactly the fields"),
        (lambda d: d["overrides"][0].update(extra="x"), "exactly the fields"),
        (lambda d: d["overrides"][0].update(decision="primary"), "unknown decision"),
        (lambda d: d["overrides"][0].update(reason=7), "must be strings"),
        (lambda d: d.update(format_version=3), "unsupported selection format 3"),
    ],
    ids=[
        "version-1-with-overrides",
        "version-2-without",
        "empty-list",
        "missing-field",
        "extra-field",
        "bad-decision",
        "non-string",
        "unknown-version",
    ],
)
def test_malformed_override_files_are_refused(
    base: sp.Selection, change: Callable[[dict[str, Any]], object], message: str
) -> None:
    data: dict[str, Any] = with_override_json(base)
    change(data)
    with pytest.raises(ValueError, match=message):
        sp.selection_from_json(data)


def test_write_list_takes_a_promoted_backup_and_refuses_a_demoted_one(
    base: sp.Selection, tmp_path: Path
) -> None:
    weak = next(i for i, (d, p) in picks_of(base).items() if d == "backup" and p == 7)
    strong = by_decision(base, "eligible")[0]
    changed = sp.selection_from_json(
        json.loads(
            json.dumps(
                sp.selection_to_json(
                    sp.apply_overrides(
                        base, [override(weak, "eligible"), override(strong, "backup")]
                    )
                )
            )
        )
    )
    assert sp.write_list([changed], [strong], tmp_path / "a.txt", channels=["chan_a"]) == [strong]
    with pytest.raises(ValueError, match="not a primary or backup"):
        sp.write_list([changed], [weak], tmp_path / "b.txt", channels=["chan_a"])


def test_save_selection_records_provenance(base: sp.Selection, tmp_path: Path) -> None:
    path = sp.save_selection(
        base, tmp_path, provenance={"derived_from": "evaluation.json", "derived_from_sha256": "ab"}
    )
    source = json.loads(path.read_text(encoding="utf-8"))["source"]
    assert source["derived_from"] == "evaluation.json"
    assert source["derived_from_sha256"] == "ab"
    assert {"commit", "dirty"} <= set(source)
