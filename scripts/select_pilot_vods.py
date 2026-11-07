"""Choose the pilot VODs (#35) from the channels in ``configs/channels.yaml``.

Run from the repository root::

    uv run python scripts/select_pilot_vods.py fetch       # Helix metadata only; saves a selection
    uv run python scripts/select_pilot_vods.py show        # recompute and print it, offline
    uv run python scripts/select_pilot_vods.py write-list --approve <video_id> ...

``fetch`` resolves the channels (Get Users) and lists their past broadcasts (Get Videos,
``type=archive``) through `autoclipper.ingest.twitch_api.HelixClient`. It downloads no media,
chat or clips. It writes, under the data directory (``AUTOCLIPPER_DATA_DIR``, never the repo):

- ``pilot/selections/selection-<UTC time>.json``: every VOD listed, its Helix metadata, the
  parameters and the decision for it. A selection file is never overwritten. ``show`` recomputes
  the decisions from a saved file, so the same file always gives the same table.
- ``processed/broadcasters.parquet``: the channels in the ``broadcasters`` schema
  (docs/architecture/data-model.md); a channel's row is replaced when it is fetched again.

**Hard rules.** A VOD that fails one is never picked, however few VODs a channel has:

- it is a past broadcast (``type == "archive"``) of a channel in ``configs/channels.yaml``, with
  a duration above zero;
- it is at least 7 days old (from ``created_at``) when the selection is made. This is the
  clip-snapshot maturity rule (docs/data/labeling.md § 7), and ``--min-age-days`` can't lower it.
  A VOD only gets older, so it still holds when the VOD is ingested later.

**Availability and expiry** are separate from the maturity rule. *Known*: Helix listed the VOD
at fetch time (whether it can be downloaded, e.g. when subscriber-only, is only known at
ingest). *Estimated*: when Twitch deletes it, from ``broadcaster_type``: past broadcasts are kept
7 days, 14 for Affiliates and 60 for Partners (docs/data/data-sources.md § VOD retention). A
streamer can delete a VOD earlier, and Prime/Turbo can keep it longer, which the type doesn't
show. The expiry risk is ``low`` with at least ``--expiry-margin-days`` (default 3) estimated
days left, ``high`` with fewer, ``past estimate`` when Helix still lists a VOD older than its
estimated retention (the estimate is then wrong for this channel), and ``unknown`` for an
unrecognized type. Expiry is a ranking preference, not an exclusion: issue #35 asks only that a
VOD is at least 7 days old and still available. The 3-day margin covers the time from selection
to ingest, which runs oldest first; a VOD gone by then is recorded ``unavailable`` at ingest and
an approved backup replaces it.

**Preferences** only rank eligible VODs: 7-30 days old, 2-12 h long, under 20 % muted, in
English, low expiry risk. A preference whose value isn't known counts as not met, so missing
information never improves a VOD's rank. That includes the muted share: Helix sends
``muted_segments: null`` for "none", but the client reads ``null`` and ``[]`` the same way and
doesn't confirm that muting is reported to other apps, so "no segments" is shown as unknown.

**Picking.** Per channel, eligible VODs are taken from the fewest preferences missed upwards;
within a group the next pick is the VOD furthest in time from those already picked (the oldest
first), ties going to the lower video ID. The first ``--per-channel`` picks are primaries, the
next ``--backups`` are backups. A channel with too few eligible VODs is reported as short.

Not checkable from metadata: whether a VOD is subscriber-only (found out at download; the
backups cover it), and its category (Get Videos has none).
"""

import dataclasses
import json
import subprocess
from collections import Counter
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Final, Literal, Protocol

import httpx
import pyarrow as pa
import pyarrow.parquet as pq
import typer
import yaml

from autoclipper import tables
from autoclipper.cli import ExitCode
from autoclipper.config import ConfigError, MissingSettingError, Settings, get_settings
from autoclipper.ingest.catalog import write_table_atomically
from autoclipper.ingest.media import vod_url
from autoclipper.ingest.twitch_api import (
    Broadcaster,
    HelixClient,
    Listing,
    MutedSegment,
    TwitchAPIError,
    TwitchNotFoundError,
    Vod,
)
from autoclipper.ingest.vod import MIN_VOD_AGE
from autoclipper.logging import configure_logging
from autoclipper.schemas import parse_vod

REPO_ROOT: Final = Path(__file__).resolve().parents[1]
DEFAULT_CHANNELS: Final = REPO_ROOT / "configs" / "channels.yaml"
DEFAULT_LIST: Final = REPO_ROOT / "configs" / "pilot_vods.txt"

FORMAT_VERSION: Final = 1
"""Bump when the layout of a selection file changes."""
OVERRIDES_FORMAT_VERSION: Final = 2
"""The version of files with ``overrides``: code that predates overrides refuses such a file
instead of silently ignoring its changed backups. Files without overrides stay version 1."""

# Days Twitch keeps past broadcasts, by `broadcaster_type` (docs/data/data-sources.md § VOD
# retention). Prime/Turbo can extend an account's storage, which `broadcaster_type` doesn't show;
# an unrecognized type has no estimate.
RETENTION_DAYS: Final = {"partner": 60, "affiliate": 14, "": 7}

MAX_ARCHIVES_PER_CHANNEL: Final = 100
"""Past broadcasts listed per channel: one Get Videos page, more than 60 days of VODs."""

type Decision = Literal["primary", "backup", "eligible", "excluded"]
type ExpiryRisk = Literal["low", "high", "past estimate", "unknown"]
PREFERENCES: Final = ("age", "duration", "muted", "language", "expiry")


@dataclass(frozen=True, slots=True)
class Params:
    """The selection's parameters; recorded in the selection file.

    Attributes:
        per_channel: Primary VODs per channel.
        backups: Backup VODs per channel.
        min_age_days: Hard rule: minimum age; at least the 7-day maturity rule.
        expiry_margin_days: Preferred: at least this many estimated days before Twitch deletes
            the VOD (below it the expiry risk is ``high``).
        preferred_age_days: Preferred age range, inclusive.
        preferred_hours: Preferred duration range in hours, inclusive.
        max_muted_fraction: Preferred: muted share below this.
        language: Preferred VOD language (Helix ``language``).
    """

    per_channel: int = 5
    backups: int = 2
    min_age_days: float = MIN_VOD_AGE / timedelta(days=1)
    expiry_margin_days: float = 3.0
    preferred_age_days: tuple[float, float] = (7.0, 30.0)
    preferred_hours: tuple[float, float] = (2.0, 12.0)
    max_muted_fraction: float = 0.2
    language: str = "en"

    def __post_init__(self) -> None:
        """Reject parameters that would break a hard rule or make no sense.

        Raises:
            ValueError: A parameter is out of range.
        """
        if timedelta(days=self.min_age_days) < MIN_VOD_AGE:
            raise ValueError(f"min_age_days can't be below {MIN_VOD_AGE.days} (labeling.md § 7)")
        if self.per_channel < 1 or self.backups < 0:
            raise ValueError("per_channel must be at least 1 and backups at least 0")
        if self.expiry_margin_days < 0:
            raise ValueError("expiry_margin_days can't be negative")
        for name in ("preferred_age_days", "preferred_hours"):
            low, high = getattr(self, name)
            if not 0 <= low <= high:
                raise ValueError(f"{name} must be a range low <= high, both >= 0")
        if not 0 < self.max_muted_fraction <= 1:
            raise ValueError("max_muted_fraction must be in (0, 1]")


@dataclass(frozen=True, slots=True)
class Assessment:
    """How one listed VOD fared.

    Attributes:
        vod: The VOD's Helix metadata.
        login: Its channel's login.
        age: Its age when the selection was made.
        retention_days: Estimated days Twitch keeps it; None if the channel's type is unknown.
        expiry_risk: ``low``, ``high``, ``past estimate`` or ``unknown`` (see the module notes).
        muted_fraction: Share of the VOD Helix reports muted; None when Helix reports nothing.
        failed_rules: Why it's excluded; empty when eligible.
        preferences: Each preference: met (True), missed (False) or unknown (None).
        decision: ``primary``, ``backup``, ``eligible`` (not picked) or ``excluded``.
        pick: 1-based pick order within the channel, for primaries and backups.
    """

    vod: Vod
    login: str
    age: timedelta
    retention_days: int | None
    expiry_risk: ExpiryRisk
    muted_fraction: float | None
    failed_rules: tuple[str, ...]
    preferences: dict[str, bool | None]
    decision: Decision = "excluded"
    pick: int | None = None

    @property
    def eligible(self) -> bool:
        """True when every hard rule passes."""
        return not self.failed_rules

    @property
    def misses(self) -> int:
        """Preferences not met, counting unknown ones."""
        return sum(1 for met in self.preferences.values() if met is not True)

    @property
    def days_left(self) -> float | None:
        """Estimated days before Twitch deletes the VOD; None when retention is unknown."""
        if self.retention_days is None:
            return None
        return self.retention_days - self.age / timedelta(days=1)


@dataclass(frozen=True, slots=True)
class Override:
    """An approved change to one backup, applied after the selection is computed.

    Attributes:
        video_id: The VOD.
        login: Its channel; must be the VOD's.
        decision: ``backup`` promotes an eligible, unpicked VOD; ``eligible`` demotes a backup
            back to the eligible pool. Primaries can't be changed.
        reason: Why (required).
        approved_at: When it was approved (timezone-aware).
    """

    video_id: str
    login: str
    decision: Literal["backup", "eligible"]
    reason: str
    approved_at: datetime


@dataclass(frozen=True, slots=True)
class Selection:
    """A complete selection: its inputs and the decision for every VOD listed."""

    selected_at: datetime
    params: Params
    broadcasters: tuple[Broadcaster, ...]
    listings: dict[str, Listing[Vod]]  # by login, in channel order
    assessments: tuple[Assessment, ...]
    overrides: tuple[Override, ...] = ()


class ArchiveSource(Protocol):
    """The two Helix calls `fetch_listings` needs; `HelixClient` provides them."""

    def get_users(self, logins: Iterable[str]) -> list[Broadcaster]:
        """Resolve channel logins."""
        ...

    def get_archive_videos(self, user_id: str, *, max_results: int = ...) -> Listing[Vod]:
        """List a channel's past broadcasts, newest first."""
        ...


# --- Inputs ------------------------------------------------------------------------------------


def load_channel_logins(path: Path) -> list[str]:
    """The channel logins in a ``channels.yaml`` (``channels: [{login: ...}, ...]``).

    Raises:
        ValueError: The file isn't in that shape, or a login is blank or repeated.
    """
    data: object = yaml.safe_load(path.read_text(encoding="utf-8"))
    entries = data.get("channels") if isinstance(data, dict) else None
    if not isinstance(entries, list) or not entries:
        raise ValueError(f"{path} has no 'channels' list")
    logins: list[str] = []
    for i, entry in enumerate(entries):
        login = entry.get("login") if isinstance(entry, dict) else None
        if not isinstance(login, str) or not login.strip():
            raise ValueError(f"{path}: channels[{i}] has no login")
        logins.append(login.strip().lower())
    repeated = sorted(login for login, n in Counter(logins).items() if n > 1)
    if repeated:
        raise ValueError(f"{path}: repeated logins {', '.join(repeated)}")
    return logins


def fetch_listings(
    helix: ArchiveSource, logins: Sequence[str], *, max_results: int = MAX_ARCHIVES_PER_CHANNEL
) -> tuple[tuple[Broadcaster, ...], dict[str, Listing[Vod]]]:
    """Resolve the channels and list each one's past broadcasts (metadata only).

    Raises:
        TwitchNotFoundError: A login doesn't exist.
        TwitchAPIError: Helix failed.
    """
    broadcasters = tuple(helix.get_users(logins))
    listings = {
        b.login.lower(): helix.get_archive_videos(b.broadcaster_id, max_results=max_results)
        for b in broadcasters
    }
    return broadcasters, listings


# --- Rules -------------------------------------------------------------------------------------


def muted_fraction(vod: Vod) -> float | None:
    """Share of the VOD in Helix's muted segments (overlaps counted once), or None if none."""
    if not vod.muted_segments or vod.duration_s <= 0:
        return None
    spans = sorted(
        (max(0.0, s.offset_s), min(vod.duration_s, s.offset_s + s.duration_s))
        for s in vod.muted_segments
    )
    total, end = 0.0, 0.0
    for start, stop in spans:
        start = max(start, end)
        if stop > start:
            total += stop - start
            end = stop
    return total / vod.duration_s


def assess(vod: Vod, broadcaster: Broadcaster, now: datetime, params: Params) -> Assessment:
    """Apply the hard rules and preferences to one VOD (no decision yet)."""
    age = now - vod.created_at
    retention = RETENTION_DAYS.get(broadcaster.broadcaster_type)
    failed: list[str] = []
    if vod.type != "archive":
        failed.append(f"not a past broadcast (type {vod.type!r})")
    if vod.broadcaster_id != broadcaster.broadcaster_id:
        failed.append("listed under another channel")
    if vod.duration_s <= 0:
        failed.append("no duration")
    if age < timedelta(days=params.min_age_days):
        failed.append(f"under {params.min_age_days:g} days old")
    risk = expiry_risk(age, retention, params.expiry_margin_days)
    fraction = muted_fraction(vod)
    age_days = age / timedelta(days=1)
    hours = vod.duration_s / 3600
    language: bool | None = None
    if vod.language not in ("", "other"):
        language = vod.language == params.language
    preferences: dict[str, bool | None] = {
        "age": params.preferred_age_days[0] <= age_days <= params.preferred_age_days[1],
        "duration": params.preferred_hours[0] <= hours <= params.preferred_hours[1],
        "muted": None if fraction is None else fraction < params.max_muted_fraction,
        "language": language,
        "expiry": None if risk == "unknown" else risk == "low",
    }
    login = broadcaster.login.lower()
    return Assessment(vod, login, age, retention, risk, fraction, tuple(failed), preferences)


def expiry_risk(age: timedelta, retention_days: int | None, margin_days: float) -> ExpiryRisk:
    """How likely Twitch deletes the VOD before it is ingested (an estimate; see module notes)."""
    if retention_days is None:
        return "unknown"
    left = timedelta(days=retention_days) - age
    if left <= timedelta(0):
        return "past estimate"
    return "high" if left < timedelta(days=margin_days) else "low"


def _id_key(video_id: str) -> tuple[int, str]:
    """Order IDs numerically without casting them to int (IDs stay strings)."""
    return (len(video_id), video_id)


def _pick_spread(pool: list[Assessment], picked: list[Assessment], count: int) -> list[Assessment]:
    """Pick up to `count` VODs from `pool`, each furthest in time from all picked so far."""
    chosen: list[Assessment] = []
    remaining = sorted(pool, key=lambda a: (a.vod.created_at, _id_key(a.vod.video_id)))
    while remaining and len(chosen) < count:
        taken = [t.vod.created_at for t in picked + chosen]
        if not taken:
            best = remaining[0]  # the oldest: it expires first
        else:
            gaps = {
                a.vod.video_id: min(abs((a.vod.created_at - t).total_seconds()) for t in taken)
                for a in remaining
            }
            widest = max(gaps.values())
            best = min(
                (a for a in remaining if gaps[a.vod.video_id] == widest),
                key=lambda a: _id_key(a.vod.video_id),
            )
        remaining.remove(best)
        chosen.append(best)
    return chosen


def select(
    broadcasters: Sequence[Broadcaster],
    listings: dict[str, Listing[Vod]],
    now: datetime,
    params: Params,
) -> Selection:
    """Assess every listed VOD and pick each channel's primaries and backups.

    Deterministic: the same inputs and `now` always give the same decisions.
    """
    assessments: list[Assessment] = []
    for broadcaster in broadcasters:
        login = broadcaster.login.lower()
        seen: set[str] = set()
        channel: list[Assessment] = []
        for vod in listings[login].items:
            if vod.video_id not in seen:
                seen.add(vod.video_id)
                channel.append(assess(vod, broadcaster, now, params))
        eligible = [a for a in channel if a.eligible]
        picked: list[Assessment] = []
        wanted = params.per_channel + params.backups
        for misses in sorted({a.misses for a in eligible}):
            tier = [a for a in eligible if a.misses == misses]
            picked += _pick_spread(tier, picked, wanted - len(picked))
        order = {a.vod.video_id: i for i, a in enumerate(picked, start=1)}
        for a in channel:
            pick = order.get(a.vod.video_id)
            if pick is not None:
                decision: Decision = "primary" if pick <= params.per_channel else "backup"
            else:
                decision = "eligible" if a.eligible else "excluded"
            assessments.append(dataclasses.replace(a, decision=decision, pick=pick))
    return Selection(now, params, tuple(broadcasters), dict(listings), tuple(assessments))


def apply_overrides(selection: Selection, overrides: Sequence[Override]) -> Selection:
    """Apply approved backup changes to a computed selection; primaries never change.

    Demotions are applied first, then promotions in the given order; a promoted VOD gets the
    next pick number of its channel. The same selection and overrides always give the same
    result.

    Raises:
        ValueError: An override has no reason or time zone, repeats a VOD, names a VOD the
            selection doesn't list or the wrong channel, targets a primary, promotes a VOD
            that isn't eligible and unpicked (e.g. one under 7 days old), or demotes a VOD that
            isn't a backup.
    """
    if not overrides:
        return selection
    by_id = {a.vod.video_id: a for a in selection.assessments}
    seen: set[str] = set()
    for o in overrides:
        if o.video_id in seen:
            raise ValueError(f"override for {o.video_id} is repeated")
        seen.add(o.video_id)
        if not o.reason.strip():
            raise ValueError(f"override for {o.video_id} has no reason")
        if o.approved_at.tzinfo is None:
            raise ValueError(f"override for {o.video_id} has an approval time without time zone")
        a = by_id.get(o.video_id)
        if a is None:
            raise ValueError(f"override names {o.video_id}, which this selection doesn't list")
        if a.login != o.login.lower():
            raise ValueError(f"override for {o.video_id} names {o.login}; the VOD is {a.login}'s")
        if a.decision == "primary":
            raise ValueError(f"an override can't change primary {o.video_id}")
        if o.decision == "backup" and a.decision != "eligible":
            raise ValueError(
                f"an override can only promote an eligible VOD; {o.video_id} is {a.decision}"
            )
        if o.decision == "eligible" and a.decision != "backup":
            raise ValueError(f"an override can only demote a backup; {o.video_id} is {a.decision}")
        if o.decision not in ("backup", "eligible"):
            raise ValueError(f"override for {o.video_id} has an unknown decision {o.decision!r}")
    changed = dict(by_id)
    for o in overrides:
        if o.decision == "eligible":
            changed[o.video_id] = dataclasses.replace(
                changed[o.video_id], decision="eligible", pick=None
            )
    for o in overrides:
        if o.decision == "backup":
            a = changed[o.video_id]
            picks = [x.pick for x in changed.values() if x.login == a.login and x.pick is not None]
            changed[o.video_id] = dataclasses.replace(
                a, decision="backup", pick=max(picks, default=0) + 1
            )
    assessments = tuple(changed[a.vod.video_id] for a in selection.assessments)
    return dataclasses.replace(selection, assessments=assessments, overrides=tuple(overrides))


def _override_from_json(raw: object) -> Override:
    fields = {"video_id", "login", "decision", "reason", "approved_at"}
    if not isinstance(raw, dict) or set(raw) != fields:
        raise ValueError(f"an override needs exactly the fields {', '.join(sorted(fields))}")
    if not all(isinstance(raw[name], str) for name in fields):
        raise ValueError("override fields must be strings")
    if raw["decision"] not in ("backup", "eligible"):
        raise ValueError(
            f"override for {raw['video_id']} has an unknown decision {raw['decision']!r}"
        )
    return Override(
        raw["video_id"],
        raw["login"],
        raw["decision"],
        raw["reason"],
        datetime.fromisoformat(raw["approved_at"]),
    )


# --- Selection files ---------------------------------------------------------------------------


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _vod_to_json(vod: Vod) -> dict[str, object]:
    row = dataclasses.asdict(vod)
    row["created_at"] = _iso(vod.created_at)
    row["fetched_at"] = _iso(vod.fetched_at)
    row["muted_segments"] = [dataclasses.asdict(s) for s in vod.muted_segments]
    row["game_ids"] = None if vod.game_ids is None else list(vod.game_ids)
    return row


def _vod_from_json(row: dict[str, Any]) -> Vod:
    game_ids = row["game_ids"]
    return Vod(
        **{
            **row,
            "created_at": datetime.fromisoformat(row["created_at"]),
            "fetched_at": datetime.fromisoformat(row["fetched_at"]),
            "muted_segments": tuple(MutedSegment(**s) for s in row["muted_segments"]),
            "game_ids": None if game_ids is None else tuple(game_ids),
        }
    )


def _git_revision() -> dict[str, object]:
    """The repo's commit and whether it had uncommitted changes, for provenance."""
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return {"commit": None, "dirty": None}
    return {"commit": commit, "dirty": bool(status.strip())}


def selection_to_json(selection: Selection) -> dict[str, object]:
    """The selection file's content: inputs, parameters, any overrides and every decision."""
    p = selection.params
    overrides = (
        {
            "overrides": [
                {**dataclasses.asdict(o), "approved_at": _iso(o.approved_at)}
                for o in selection.overrides
            ]
        }
        if selection.overrides
        else {}
    )
    return {
        "format_version": OVERRIDES_FORMAT_VERSION if selection.overrides else FORMAT_VERSION,
        **overrides,
        "selected_at": _iso(selection.selected_at),
        "params": dataclasses.asdict(p),
        "retention_days": RETENTION_DAYS,
        "broadcasters": [
            {**dataclasses.asdict(b), "fetched_at": _iso(b.fetched_at)}
            for b in selection.broadcasters
        ],
        "listings": {
            login: {
                "truncated": listing.truncated,
                "vods": [_vod_to_json(v) for v in listing.items],
            }
            for login, listing in selection.listings.items()
        },
        "decisions": [
            {
                "video_id": a.vod.video_id,
                "login": a.login,
                "decision": a.decision,
                "pick": a.pick,
                "age_days": round(a.age / timedelta(days=1), 3),
                "days_left": None if a.days_left is None else round(a.days_left, 3),
                "retention_days": a.retention_days,
                "expiry_risk": a.expiry_risk,
                "muted_fraction": a.muted_fraction,
                "failed_rules": list(a.failed_rules),
                "preferences": a.preferences,
            }
            for a in selection.assessments
        ],
    }


def selection_from_json(data: dict[str, Any]) -> Selection:
    """Recompute a selection from a saved file's inputs (its stored decisions are ignored).

    A file's ``overrides`` (format version 2) are applied after the recomputation.

    Raises:
        ValueError: The file is from an unknown format version, its overrides don't match its
            version, or an override is invalid (see `apply_overrides`).
    """
    version = data.get("format_version")
    if version not in (FORMAT_VERSION, OVERRIDES_FORMAT_VERSION):
        raise ValueError(f"unsupported selection format {version!r}")
    raw_overrides = data.get("overrides")
    if (version == OVERRIDES_FORMAT_VERSION) != (raw_overrides is not None):
        raise ValueError(
            f"format version {version} {'needs' if version == 2 else 'has no'} overrides"
        )
    if raw_overrides is not None and (not isinstance(raw_overrides, list) or not raw_overrides):
        raise ValueError("overrides must be a non-empty list")
    overrides = tuple(_override_from_json(o) for o in raw_overrides or [])
    raw_params = dict(data["params"])
    for name in ("preferred_age_days", "preferred_hours"):
        raw_params[name] = tuple(raw_params[name])
    params = Params(**raw_params)
    broadcasters = tuple(
        Broadcaster(**{**b, "fetched_at": datetime.fromisoformat(b["fetched_at"])})
        for b in data["broadcasters"]
    )
    listings = {
        login: Listing(tuple(_vod_from_json(v) for v in entry["vods"]), entry["truncated"])
        for login, entry in data["listings"].items()
    }
    computed = select(broadcasters, listings, datetime.fromisoformat(data["selected_at"]), params)
    return apply_overrides(computed, overrides)


def selections_dir(data_dir: Path) -> Path:
    """``<data_dir>/pilot/selections/``."""
    return data_dir / "pilot" / "selections"


def broadcasters_path(data_dir: Path) -> Path:
    """``<data_dir>/processed/broadcasters.parquet``."""
    return data_dir / "processed" / "broadcasters.parquet"


def save_selection(
    selection: Selection, data_dir: Path, *, provenance: dict[str, object] | None = None
) -> Path:
    """Write a new selection file; never replaces an existing one.

    Args:
        selection: The selection.
        data_dir: The data directory.
        provenance: Extra ``source`` fields, e.g. the record a selection was derived from.

    Raises:
        FileExistsError: A selection with the same timestamp exists.
    """
    path = selections_dir(data_dir) / f"selection-{selection.selected_at:%Y%m%dT%H%M%SZ}.json"
    if path.exists():
        raise FileExistsError(f"{path} already exists")
    content = dict(selection_to_json(selection), source={**_git_revision(), **(provenance or {})})
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.part")
    temp.write_text(json.dumps(content, indent=2) + "\n", encoding="utf-8", newline="\n")
    temp.replace(path)
    return path


def latest_selection(data_dir: Path) -> Path | None:
    """The newest selection file, or None if there is none."""
    files = sorted(selections_dir(data_dir).glob("selection-*.json"))
    return files[-1] if files else None


def write_broadcasters(broadcasters: Iterable[Broadcaster], data_dir: Path) -> Path:
    """Add the channels to ``broadcasters.parquet``, replacing rows of the same channel."""
    path = broadcasters_path(data_dir)
    rows = (
        {}
        if not path.is_file()
        else {
            row["broadcaster_id"]: row
            for row in pq.read_table(path, schema=tables.BROADCASTERS).to_pylist()
        }
    )
    for b in broadcasters:
        rows[b.broadcaster_id] = dataclasses.asdict(b)
    ordered = [rows[key] for key in sorted(rows, key=_id_key)]
    write_table_atomically(pa.Table.from_pylist(ordered, schema=tables.BROADCASTERS), path)
    return path


def approved_picks(selections: Sequence[Selection]) -> dict[str, Assessment]:
    """The primaries and backups of one or more selections, by video ID.

    Raises:
        ValueError: A VOD is a pick of more than one selection, or the selections disagree
            about a channel's broadcaster ID.
    """
    channel_ids: dict[str, str] = {}
    for selection in selections:
        for b in selection.broadcasters:
            known = channel_ids.setdefault(b.login.lower(), b.broadcaster_id)
            if known != b.broadcaster_id:
                raise ValueError(f"the selections disagree about {b.login}'s broadcaster ID")
    picks: dict[str, Assessment] = {}
    for selection in selections:
        for a in selection.assessments:
            if a.decision in ("primary", "backup"):
                if a.vod.video_id in picks:
                    raise ValueError(f"{a.vod.video_id} is a pick of more than one selection")
                picks[a.vod.video_id] = a
    return picks


def pick_order(a: Assessment) -> tuple[datetime, tuple[int, str]]:
    """Sort key for picks: oldest first, then by video ID."""
    return (a.vod.created_at, _id_key(a.vod.video_id))


def resolve_approved(
    picks: dict[str, Assessment], approved: Sequence[str], channels: Collection[str]
) -> list[Assessment]:
    """The approved IDs' picks, oldest first.

    Raises:
        ValueError: An ID (or link) isn't one of `picks`, is repeated, or belongs to a channel
            not in `channels`.
    """
    ids = [parse_vod(text) or text for text in approved]
    unknown = [i for i in ids if i not in picks]
    if unknown:
        raise ValueError(f"not a primary or backup of these selections: {', '.join(unknown)}")
    if len(set(ids)) != len(ids):
        raise ValueError("an approved video ID is repeated")
    wanted = {login.lower() for login in channels}
    outside = sorted({picks[i].login for i in ids} - wanted)
    if outside:
        raise ValueError(
            f"approved VODs of channels not in the channels file: {', '.join(outside)}"
        )
    return sorted((picks[i] for i in ids), key=pick_order)


def write_list(
    selections: Sequence[Selection],
    approved: Sequence[str],
    path: Path,
    *,
    channels: Collection[str],
) -> list[str]:
    """Write the approved VODs' links to `path`, oldest first; returns the video IDs written.

    The approved IDs may come from several selections (e.g. one channel fetched later). Each
    must be a primary or backup of exactly one of them, and of a channel in `channels`.

    Raises:
        ValueError: See `approved_picks` and `resolve_approved`.
        FileExistsError: `path` exists (the caller decides whether to replace it).
    """
    picks = approved_picks(selections)
    chosen = resolve_approved(picks, approved, channels)
    if path.exists():
        raise FileExistsError(f"{path} exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(f"{vod_url(a.vod.video_id)}\n" for a in chosen), encoding="utf-8", newline="\n"
    )
    return [a.vod.video_id for a in chosen]


# --- Table -------------------------------------------------------------------------------------


def _hours(seconds: float) -> str:
    return f"{int(seconds // 3600)}h{int(seconds % 3600 // 60):02d}m"


def _muted_cell(a: Assessment) -> str:
    if a.muted_fraction is None:
        return "unknown (none reported)"
    return f"{a.muted_fraction:.1%} reported ({len(a.vod.muted_segments)} segments)"


def _notes(a: Assessment, override: Override | None = None) -> str:
    missed = [name for name in PREFERENCES if a.preferences[name] is False]
    unknown = [name for name in PREFERENCES if a.preferences[name] is None]
    notes = []
    if missed:
        notes.append("misses: " + ", ".join(missed))
    if unknown:
        notes.append("unknown: " + ", ".join(unknown))
    if override is not None:
        notes.append(f"override ({override.approved_at:%Y-%m-%d}): {override.reason}")
    return "; ".join(notes) or "meets every preference"


def _expiry_cell(a: Assessment) -> str:
    if a.days_left is None:
        return "unknown (channel type not recognized)"
    if a.expiry_risk == "past estimate":
        return f"past estimate by {-a.days_left:.1f} d, still listed"
    return f"{a.days_left:.1f} d left, {a.expiry_risk} risk"


def render_table(selection: Selection) -> str:
    """The selection as Markdown: a summary per channel and one row per primary and backup."""
    p = selection.params
    lines = [
        f"Selection at {_iso(selection.selected_at)}: {p.per_channel} primaries and {p.backups} "
        f"backups per channel; hard rule: age >= {p.min_age_days:g} d. Expiry risk is low with "
        f">= {p.expiry_margin_days:g} estimated days left.",
        "",
        "Known: every VOD below was listed by Helix at the selection time. Reported by Helix: "
        "recorded date, duration, views, language and muted segments. Computed: age. Estimated: "
        "expiry, from the channel type. Unknown until download: subscriber-only status.",
        "",
        "| Channel | Pick | VOD | Recorded (UTC) | Duration | Views | Age (d) | Expiry (est.) "
        "| Muted | Lang | Notes |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    channel_order = {b.login.lower(): i for i, b in enumerate(selection.broadcasters)}
    picked = [a for a in selection.assessments if a.decision in ("primary", "backup")]
    picked.sort(key=lambda a: (channel_order[a.login], a.decision != "primary", a.vod.created_at))
    overridden = {o.video_id: o for o in selection.overrides}
    for a in picked:
        vod = a.vod
        lines.append(
            f"| {a.login} | {a.decision} #{a.pick} | [{vod.video_id}]({vod_url(vod.video_id)}) "
            f"| {vod.created_at:%Y-%m-%d %H:%M} | {_hours(vod.duration_s)} | {vod.view_count:,} "
            f"| {a.age / timedelta(days=1):.1f} | {_expiry_cell(a)} | {_muted_cell(a)} "
            f"| {vod.language or 'unknown'} | {_notes(a, overridden.get(vod.video_id))} |"
        )
    lines += [
        "",
        "| Channel | Type | Listed | Eligible | Primaries | Backups | Excluded (reasons) |",
    ]
    lines.append("|---|---|---|---|---|---|---|")
    warnings: list[str] = []
    for b in selection.broadcasters:
        login = b.login.lower()
        mine = [a for a in selection.assessments if a.login == login]
        counts = Counter(a.decision for a in mine)
        reasons = Counter(rule for a in mine for rule in a.failed_rules)
        excluded = "; ".join(f"{n} {rule}" for rule, n in sorted(reasons.items())) or "none"
        lines.append(
            f"| {login} | {b.broadcaster_type or 'none'} | {len(mine)} "
            f"| {sum(1 for a in mine if a.eligible)} | {counts['primary']} | {counts['backup']} "
            f"| {excluded} |"
        )
        if counts["primary"] < p.per_channel:
            warnings.append(f"{login}: only {counts['primary']} of {p.per_channel} primaries")
        if counts["backup"] < p.backups:
            warnings.append(f"{login}: only {counts['backup']} of {p.backups} backups")
        if selection.listings[login].truncated:
            warnings.append(f"{login}: listing stopped at {MAX_ARCHIVES_PER_CHANNEL} VODs")
        risky = [a.vod.video_id for a in mine if a.pick and a.expiry_risk != "low"]
        if risky:
            warnings.append(f"{login}: expiry risk not low for picks {', '.join(risky)}")
    primaries = [a for a in picked if a.decision == "primary"]
    hours = sum(a.vod.duration_s for a in primaries) / 3600
    lines += ["", f"Primaries: {len(primaries)} VODs, {hours:.1f} h in total."]
    lines += [f"- Warning: {w}" for w in warnings]
    return "\n".join(lines) + "\n"


# --- Command line ------------------------------------------------------------------------------

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Choose the pilot VODs (#35): fetch metadata, show the selection, write the list.",
)


def _settings_or_exit() -> Settings:
    try:
        settings = get_settings()
        configure_logging(settings.log_level)
    except ConfigError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(ExitCode.USER_ERROR) from None
    return settings


def open_helix() -> HelixClient:
    """A Helix client with the configured app credentials (tests replace this)."""
    return HelixClient(get_settings().require_twitch_credentials())


def utc_now() -> datetime:
    """The selection time (tests replace this)."""
    return datetime.now(tz=UTC)


def _load(path: Path | None, data_dir: Path) -> tuple[Path, Selection]:
    path = path or latest_selection(data_dir)
    if path is None:
        typer.echo("Error: no selection yet; run `fetch` first", err=True)
        raise typer.Exit(ExitCode.USER_ERROR)
    try:
        return path, selection_from_json(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        typer.echo(f"Error: can't read {path}: {exc}", err=True)
        raise typer.Exit(ExitCode.USER_ERROR) from None


@app.command()
def fetch(
    channels: Annotated[Path, typer.Option(help="The channels file.")] = DEFAULT_CHANNELS,
    per_channel: Annotated[int, typer.Option(help="Primary VODs per channel.")] = 5,
    backups: Annotated[int, typer.Option(help="Backup VODs per channel.")] = 2,
    min_age_days: Annotated[float, typer.Option(help="Minimum age (at least 7).")] = 7.0,
    expiry_margin_days: Annotated[
        float,
        typer.Option(help="Estimated days left below which a VOD's expiry risk is high."),
    ] = 3.0,
) -> None:
    """List the channels' past broadcasts (metadata only), record a selection and print it."""
    settings = _settings_or_exit()
    try:
        params = Params(per_channel, backups, min_age_days, expiry_margin_days)
        logins = load_channel_logins(channels)
    except (OSError, ValueError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(ExitCode.USER_ERROR) from None
    try:
        helix = open_helix()
    except MissingSettingError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(ExitCode.USER_ERROR) from None
    try:
        broadcasters, listings = fetch_listings(helix, logins)
    except TwitchNotFoundError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(ExitCode.USER_ERROR) from None
    except (TwitchAPIError, httpx.HTTPError) as exc:
        typer.echo(f"Error: Twitch API request failed: {type(exc).__name__}: {exc}", err=True)
        raise typer.Exit(ExitCode.EXTERNAL_FAILURE) from None
    finally:
        helix.close()
    selection = select(broadcasters, listings, utc_now(), params)
    path = save_selection(selection, settings.data_dir)
    table = write_broadcasters(broadcasters, settings.data_dir)
    typer.echo(render_table(selection))
    typer.echo(f"Selection saved to {path}\nChannels saved to {table}")


@app.command()
def show(
    selection: Annotated[
        Path | None, typer.Option(help="A selection file. Default: the newest one.")
    ] = None,
) -> None:
    """Recompute a saved selection's decisions (no network) and print the table."""
    settings = _settings_or_exit()
    path, result = _load(selection, settings.data_dir)
    typer.echo(render_table(result))
    typer.echo(f"From {path}")


@app.command("write-list")
def write_list_command(
    approve: Annotated[
        list[str], typer.Option(help="An approved video ID or link; repeat for each VOD.")
    ],
    selection: Annotated[
        list[Path] | None,
        typer.Option(help="A selection file; repeat to combine several. Default: the newest one."),
    ] = None,
    channels: Annotated[Path, typer.Option(help="The channels file.")] = DEFAULT_CHANNELS,
    out: Annotated[Path, typer.Option(help="The list to write.")] = DEFAULT_LIST,
) -> None:
    """Write the approved VODs' links (oldest first) to the pilot list (no network)."""
    settings = _settings_or_exit()
    paths: list[Path | None] = list(selection) if selection else [None]
    results = [_load(path, settings.data_dir)[1] for path in paths]
    try:
        written = write_list(results, approve, out, channels=load_channel_logins(channels))
    except (OSError, ValueError) as exc:  # FileExistsError is an OSError
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(ExitCode.USER_ERROR) from None
    typer.echo(f"Wrote {len(written)} links to {out}")


if __name__ == "__main__":
    app()
