"""The fixed scenario behind the synthetic dataset, and the rows it produces for a seed.

The scenario is a hand-written plan (`VOD_PLANS`): which VOD has which planted highlights, how its
viewer clips sit around them (overlapping, touching, near-touching, disjoint), where chat is quiet
and which audio is muted. The seed only jitters positions, durations, counts, text and IDs inside
that plan, so every seed exercises the same edge cases with different data. Everything random
comes from generators seeded with the seed and a purpose string, never from the clock.
"""

import random
import string
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from pydantic import SecretStr

from autoclipper.privacy import pseudonymize

SYNTHETIC_SALT = SecretStr("autoclipper-synthetic-data-not-a-secret")
"""Public HMAC key for the synthetic ``*_hash`` columns.

Synthetic chatters are hashed exactly like real ones (docs/data/data-governance.md §2), but with
this fixed, published key instead of ``AUTOCLIPPER_CHAT_HASH_SALT``: the output then depends only
on the seed, and no synthetic hash can be computed with, or compared against, a real salt.
"""

BASE_TIME = datetime(2026, 9, 7, 18, 0, tzinfo=UTC)
"""Every wall-clock time in the dataset is this fixed instant plus a seeded offset."""

CHAT_LAG_S = (3.0, 8.0)
"""Range of the delay between a planted highlight and its chat burst (docs/data/labeling.md §5)."""

_BROADCASTERS = (
    # broadcaster_id, login, display_name, broadcaster_type
    ("9000000001", "user_001", "User_001", "partner"),
    ("9000000002", "user_002", "User_002", "affiliate"),
)
_CHATTER_IDS = tuple(f"93{n:08d}" for n in range(1, 81))
# Each broadcaster's chat community; the 20 chatters in both pools chat on both channels.
_CHATTER_POOLS = (_CHATTER_IDS[:50], _CHATTER_IDS[30:])
_NATIVE_EMOTES = {"Kappa": "9500000001", "LUL": "9500000002", "PogChamp": "9500000003"}
_THIRD_PARTY_EMOTES = ("KEKW", "OMEGALUL", "Pog", "monkaS")  # counted in n_emotes only
_CALM_LINES = (
    "hi chat", "what game is this", "lol", "gg", "true", "nice", "o7", "this song is good",
    "how long has stream been live", "hydrate", "first time here", "W", "same", "chat is slow",
    "that was close", "brb",
)  # fmt: skip
_HYPE_LINES = ("NO WAY", "CLIP IT", "WHAT", "LETS GO", "insane", "HOLY", "no shot")
_CLIP_TITLES = ("no way", "LMAO", "the timing", "clip of the year", "he really did that", "ggs")


@dataclass(frozen=True, slots=True)
class EventPlan:
    """A planted highlight: a loud audio burst, then a chat burst, and maybe viewer clips.

    Attributes:
        base_s: Event time in seconds from VOD start, before the seed's jitter of ±3 s.
        case: How its clips relate: ``overlap``, ``touch`` (end equals next start),
            ``near_touch`` (a gap of a few seconds), ``disjoint`` (one clip, far from others)
            or ``unclipped`` (no clips: a highlight that only chat and audio reveal).
        clips: ``(start relative to the event, duration)`` in seconds for each clip.
        strength: 0-1, scales loudness, chat rate and clip views.
    """

    base_s: int
    case: str
    clips: tuple[tuple[float, float], ...]
    strength: float


@dataclass(frozen=True, slots=True)
class VodPlan:
    """One fake VOD. Times are seconds from VOD start; `base_duration_s` is jittered by ±8 s."""

    video_id: str
    broadcaster: int
    base_duration_s: int
    events: tuple[EventPlan, ...]
    quiet: tuple[float, float]
    muted: tuple[float, float] | None  # (offset_s, duration_s)
    stream_id: str | None
    game_ids: tuple[str, ...] | None
    title: str


VOD_PLANS = (
    VodPlan(
        "9100000001", 0, 255,
        (
            EventPlan(60, "overlap", ((-22, 30), (-16, 28), (-9, 26.5)), 1.0),
            EventPlan(150, "near_touch", ((-24, 20), (-1, 25)), 0.8),
            EventPlan(215, "disjoint", ((-20, 30),), 0.6),
        ),
        quiet=(95, 125), muted=None, stream_id="9200000001", game_ids=("9400000001",),
        title="synthetic stream: ranked grind",
    ),
    VodPlan(
        "9100000002", 0, 190,
        (
            EventPlan(45, "touch", ((-20, 25), (5, 25)), 0.9),
            EventPlan(150, "overlap", ((-25, 30), (-12, 25.5)), 0.7),
        ),
        quiet=(8, 24), muted=(90, 30), stream_id="9200000002",
        game_ids=("9400000001", "9400000002"), title="synthetic stream: variety night",
    ),
    VodPlan(
        "9100000003", 1, 300,
        (EventPlan(80, "unclipped", (), 0.9), EventPlan(215, "unclipped", (), 0.7)),
        quiet=(130, 170), muted=None, stream_id=None, game_ids=None,
        title="synthetic stream: no clips yet",
    ),
)  # fmt: skip
"""The scenario. Positions keep bursts, quiet stretches and muted audio apart for any seed."""


@dataclass(frozen=True, slots=True)
class PlantedEvent:
    """Where the generator put a highlight in one VOD (recorded in the manifest).

    Attributes:
        time_s: The highlight, in seconds from VOD start.
        audio_burst_s: ``(start, end)`` of the loud audio burst.
        chat_burst_s: ``(start, end)`` of the chat burst, `CHAT_LAG_S` after the highlight.
    """

    case: str
    time_s: float
    strength: float
    audio_burst_s: tuple[float, float]
    chat_burst_s: tuple[float, float]
    clip_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SyntheticVod:
    """One generated VOD: its plan, seeded duration and planted highlights."""

    plan: VodPlan
    duration_s: float
    created_at: datetime
    events: tuple[PlantedEvent, ...]


@dataclass(slots=True)
class Scenario:
    """Rows for every table (dicts keyed by column name) plus the VODs they describe."""

    seed: int
    snapshot_id: str
    vods: list[SyntheticVod] = field(default_factory=list)
    broadcasters: list[dict[str, object]] = field(default_factory=list)
    vod_rows: list[dict[str, object]] = field(default_factory=list)
    clips: list[dict[str, object]] = field(default_factory=list)
    chat: dict[str, list[dict[str, object]]] = field(default_factory=dict)
    capture_window: tuple[datetime, datetime] = (BASE_TIME, BASE_TIME)


def rng_for(seed: int, purpose: str) -> random.Random:
    """Return a generator for one purpose, so adding a purpose never shifts the others."""
    return random.Random(f"autoclipper.synthetic:{seed}:{purpose}")


def build_scenario(seed: int) -> Scenario:
    """Generate every table's rows for `seed`. The same seed always gives the same rows.

    Raises:
        ValueError: `seed` is negative.
    """
    if seed < 0:
        raise ValueError(f"seed must be a non-negative integer, got {seed}")
    scenario = Scenario(seed=seed, snapshot_id=f"synthetic-seed-{seed}")
    rng = rng_for(seed, "metadata")
    for vod_index, plan in enumerate(VOD_PLANS):
        duration_s = float(plan.base_duration_s + rng.randint(-8, 8))
        created_at = BASE_TIME + timedelta(days=vod_index, minutes=rng.randint(0, 120))
        events: list[PlantedEvent] = []
        for event in plan.events:
            time_s = float(event.base_s + rng.randint(-3, 3))
            clip_ids = tuple(
                _add_clip(scenario, rng, plan, created_at, time_s + start, length, event.strength)
                for start, length in event.clips
            )
            lag_s = round(rng.uniform(*CHAT_LAG_S), 3)
            audio_burst = (time_s - 0.5, time_s - 0.5 + round(rng.uniform(2.5, 4.0), 3))
            chat_burst = (time_s + lag_s, time_s + lag_s + round(rng.uniform(8.0, 12.0), 3))
            events.append(
                PlantedEvent(event.case, time_s, event.strength, audio_burst, chat_burst, clip_ids)
            )
        vod = SyntheticVod(plan, duration_s, created_at, tuple(events))
        scenario.vods.append(vod)
        scenario.vod_rows.append(_vod_row(vod, vod_index, rng))
        scenario.chat[plan.video_id] = _chat_rows(vod, rng_for(seed, f"chat:{plan.video_id}"))
    # A clip of the first VOD whose offset Twitch hasn't filled in yet (processing delay).
    first = scenario.vods[0]
    _add_clip(scenario, rng, first.plan, first.created_at, None, 30.0, 0.3)
    # A clip with no VOD link at all (its VOD was deleted): no video_id, so no offset either.
    _add_clip(scenario, rng, VOD_PLANS[2], BASE_TIME - timedelta(days=3), None, 20.0, 0.4)
    scenario.clips[-1]["video_id"] = None
    scenario.clips[-1]["is_featured"] = None
    _finish_snapshot(scenario)
    for index, (broadcaster_id, login, display_name, kind) in enumerate(_BROADCASTERS):
        fetched_at = BASE_TIME + timedelta(days=9, seconds=index)
        scenario.broadcasters.append(
            {
                "broadcaster_id": broadcaster_id,
                "login": login,
                "display_name": display_name,
                "broadcaster_type": kind,
                "fetched_at": fetched_at,
            }
        )
    return scenario


def _add_clip(
    scenario: Scenario,
    rng: random.Random,
    plan: VodPlan,
    vod_created_at: datetime,
    offset_s: float | None,
    duration_s: float,
    strength: float,
) -> str:
    clip_id = f"SyntheticClip{len(scenario.clips) + 1:02d}-" + "".join(
        rng.choices(string.ascii_letters + string.digits, k=8)
    )
    # Clips are made seconds after the end of the stretch they cover.
    made_after_s = offset_s + duration_s if offset_s is not None else rng.uniform(30, 120)
    created_at = vod_created_at + timedelta(seconds=int(made_after_s + rng.uniform(2, 25)))
    scenario.clips.append(
        {
            "clip_id": clip_id,
            "video_id": plan.video_id,
            "broadcaster_id": _BROADCASTERS[plan.broadcaster][0],
            "creator_hash": pseudonymize(
                rng.choice(_CHATTER_POOLS[plan.broadcaster]), SYNTHETIC_SALT
            ),
            "title": rng.choice(_CLIP_TITLES),
            "game_id": (plan.game_ids or ("9400000003",))[0],
            "language": "en",
            "created_at": created_at,
            "vod_offset_s": offset_s,
            "duration_s": float(duration_s),
            "view_count": int(strength * rng.randint(40, 400)),
            "is_featured": strength >= 1.0,
            "fetched_at": None,  # set by _finish_snapshot
        }
    )
    return clip_id


def _finish_snapshot(scenario: Scenario) -> None:
    """Order the clips like one paginated capture and stamp each row's ``fetched_at``."""
    scenario.clips.sort(key=lambda row: str(row["clip_id"]))
    started_at = BASE_TIME + timedelta(days=10)
    for index, row in enumerate(scenario.clips):
        row["fetched_at"] = started_at + timedelta(milliseconds=250 * index)
    ended_at = started_at + timedelta(milliseconds=250 * len(scenario.clips))
    scenario.capture_window = (started_at, ended_at)


def _vod_row(vod: SyntheticVod, vod_index: int, rng: random.Random) -> dict[str, object]:
    plan = vod.plan
    muted = [] if plan.muted is None else [{"offset_s": plan.muted[0], "duration_s": plan.muted[1]}]
    return {
        "video_id": plan.video_id,
        "broadcaster_id": _BROADCASTERS[plan.broadcaster][0],
        "stream_id": plan.stream_id,
        "title": plan.title,
        "created_at": vod.created_at,
        "duration_s": vod.duration_s,
        "type": "archive",
        "language": "en",
        "view_count": rng.randint(100, 5000),
        "muted_segments": muted,
        "game_ids": None if plan.game_ids is None else list(plan.game_ids),
        "fetched_at": BASE_TIME + timedelta(days=9, minutes=vod_index),
    }


def _chat_rows(vod: SyntheticVod, rng: random.Random) -> list[dict[str, object]]:
    """Chat rows for one VOD, sorted by offset.

    Background chat (about one message every 2 s) except in the quiet stretch, a burst after
    every planted highlight, and in the first VOD one message linking its first clip.
    """
    plan = vod.plan
    pool = _CHATTER_POOLS[plan.broadcaster]
    timed: list[tuple[float, str, str, bool]] = []  # (offset_s, chatter, text, in_burst)
    offset_s = rng.expovariate(0.5)
    # Offsets are rounded to milliseconds, like replay timestamps, before any range check.
    while round(offset_s, 3) < vod.duration_s:
        if not plan.quiet[0] <= round(offset_s, 3) < plan.quiet[1]:
            timed.append((round(offset_s, 3), rng.choice(pool), _calm_text(rng), False))
        offset_s += rng.expovariate(0.5)
    for event in vod.events:
        start_s, end_s = event.chat_burst_s
        burst_rate = 6 + 6 * event.strength  # messages per second
        offset_s = start_s + rng.expovariate(burst_rate)
        while offset_s < end_s:
            timed.append((round(offset_s, 3), rng.choice(pool), _hype_text(rng), True))
            offset_s += rng.expovariate(burst_rate)
    if plan is VOD_PLANS[0]:
        first = vod.events[0]
        link = f"https://clips.twitch.tv/{first.clip_ids[0]}"
        link_s = round(first.chat_burst_s[1] + rng.uniform(1.0, 3.0), 3)
        timed.append((link_s, rng.choice(pool), link, False))
    timed.sort(key=lambda item: item[0])
    rows: list[dict[str, object]] = []
    for offset_s, chatter, text, in_burst in timed:
        bits = 100 if in_burst and rng.random() < 0.03 else 0
        tokens = text.split()
        badge_known = chatter != pool[-1]  # one chatter's badges are unknown (nullable columns)
        rows.append(
            {
                "video_id": plan.video_id,
                "offset_s": offset_s,
                "user_hash": pseudonymize(chatter, SYNTHETIC_SALT),
                "text": f"Cheer100 {text}" if bits else text,
                "emote_ids": [_NATIVE_EMOTES[t] for t in tokens if t in _NATIVE_EMOTES],
                "n_emotes": sum(t in _NATIVE_EMOTES or t in _THIRD_PARTY_EMOTES for t in tokens),
                "is_subscriber": (int(chatter) % 3 == 0) if badge_known else None,
                "is_moderator": chatter in pool[:2] if badge_known else None,
                "bits": bits,
                "source": "replay",
            }
        )
    return rows


def _calm_text(rng: random.Random) -> str:
    line = rng.choice(_CALM_LINES)
    if rng.random() < 0.25:
        line += " " + rng.choice((*_NATIVE_EMOTES, *_THIRD_PARTY_EMOTES))
    return line


def _hype_text(rng: random.Random) -> str:
    emote = rng.choice((*_NATIVE_EMOTES, *_THIRD_PARTY_EMOTES))
    words = [emote] * rng.randint(1, 3)
    if rng.random() < 0.5:
        words.insert(0, rng.choice(_HYPE_LINES))
    return " ".join(words)
