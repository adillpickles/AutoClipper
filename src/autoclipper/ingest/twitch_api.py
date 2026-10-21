"""Client for the Twitch Helix API: app access token, Get Users and Get Videos.

Follows docs/data/data-sources.md §1, checked against Twitch's API reference and guide
(https://dev.twitch.tv/docs/api/reference, https://dev.twitch.tv/docs/api/guide) on 2026-10-21.
The HTTP client, the clock and the sleep function are injected, so tests make no network calls
and never really wait. For every request, `HelixClient`:

- gets an app access token with the client-credentials flow and caches it until
  `TOKEN_EXPIRY_MARGIN_S` before it expires. If Helix answers ``401``, it fetches a new token and
  retries once; a second ``401`` raises `TwitchAuthError`.
- reads the ``Ratelimit-Limit``, ``Ratelimit-Remaining`` and ``Ratelimit-Reset`` headers. When
  the bucket is empty, or on ``429``, it waits until ``Ratelimit-Reset`` (a Unix epoch time in
  seconds), bounded by `RetryPolicy`.
- retries ``5xx`` responses and transport errors with exponential backoff, up to a cap.

Other endpoints (Get Clips, issue #17) build on `HelixClient.get`, `HelixClient.get_pages` and
`ResponseRecord` instead of repeating that logic.

Every failure is a `TwitchAPIError`. Its ``user_error`` class attribute tells the CLI how to
report it (docs/development/conventions.md §7): True means exit code 1 (fix the input or the
credentials), False means exit code 2 (Twitch or the network failed). Messages never contain the
client secret, the Client ID or the token, nor the values of fields that failed validation.
"""

import logging
import math
import re
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import ClassVar, Final, Self

import httpx
from pydantic import SecretStr

from autoclipper.config import TwitchCredentials

log = logging.getLogger(__name__)

HELIX_URL: Final = "https://api.twitch.tv/helix"
TOKEN_URL: Final = "https://id.twitch.tv/oauth2/token"
MAX_PAGE_SIZE: Final = 100
"""Largest ``first`` (page size) that Get Videos accepts."""
MAX_IDS_PER_REQUEST: Final = 100
"""Most ``id`` or ``login`` values Get Users and Get Videos accept in one request."""
TOKEN_EXPIRY_MARGIN_S: Final = 300.0
"""A cached token is replaced this many seconds before ``expires_in`` runs out (at most half of
its lifetime), so a request never starts with a token that is about to expire."""
DEFAULT_MAX_RESULTS: Final = 500
"""Default cap on the items one paginated listing returns, so no caller pages forever."""
DEFAULT_TIMEOUT_S: Final = 10.0
"""Timeout of the HTTP client `HelixClient` creates when none is injected."""
_MIN_RATE_LIMIT_WAIT_S: Final = 1.0  # after a 429 whose reset time is past (clock skew)
_MAX_MESSAGE_CHARS: Final = 200
_DURATION = re.compile(r"(?:([0-9]+)h)?(?:([0-9]+)m)?(?:([0-9]+)s)?")


class TwitchAPIError(Exception):
    """A Twitch API call failed. Catch this to handle every error from this module.

    Attributes:
        status_code: HTTP status of the failing response; None for transport failures and
            invalid response bodies.
        user_error: True if the user can fix the cause (exit code 1), False if Twitch or the
            network failed (exit code 2).
    """

    user_error: ClassVar[bool] = False

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class TwitchAuthError(TwitchAPIError):
    """Twitch rejected the app credentials, or rejected a freshly fetched token. Exit code 1."""

    user_error = True


class TwitchRequestError(TwitchAPIError):
    """Helix rejected the request with a ``4xx`` other than 401 and 429. Not retried. Exit 1."""

    user_error = True


class TwitchNotFoundError(TwitchRequestError):
    """A requested user or video doesn't exist (or isn't visible). Exit code 1.

    Attributes:
        missing: The logins or video IDs that were not found.
    """

    def __init__(
        self, message: str, *, missing: Sequence[str] = (), status_code: int | None = None
    ) -> None:
        super().__init__(message, status_code=status_code)
        self.missing: tuple[str, ...] = tuple(missing)


class TwitchRateLimitError(TwitchAPIError):
    """Helix kept answering ``429`` after `RetryPolicy.max_rate_limit_retries`. Exit code 2."""


class TwitchUnavailableError(TwitchAPIError):
    """``5xx`` responses or transport errors outlasted the retries. Exit code 2."""


class TwitchResponseError(TwitchAPIError):
    """A response didn't have the documented shape (missing or malformed field). Exit code 2."""


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Bounds on waiting and retrying. Every wait goes through the injected sleep function.

    Attributes:
        max_rate_limit_retries: Retries after ``429`` before raising `TwitchRateLimitError`.
        max_rate_limit_wait_s: Longest single wait for the rate-limit bucket to refill. Twitch's
            bucket refills within a minute, so a longer ``Ratelimit-Reset`` is treated as skew.
        max_transient_retries: Retries after ``5xx`` responses and transport errors, counted
            together per request. Twitch's API guide advises retrying a ``503`` once.
        backoff_base_s: First backoff wait; each further retry doubles it.
        backoff_max_s: Longest backoff wait.
    """

    max_rate_limit_retries: int = 3
    max_rate_limit_wait_s: float = 60.0
    max_transient_retries: int = 2
    backoff_base_s: float = 1.0
    backoff_max_s: float = 30.0

    def __post_init__(self) -> None:
        if self.max_rate_limit_retries < 0 or self.max_transient_retries < 0:
            raise ValueError("retry counts must be >= 0")
        waits = (self.max_rate_limit_wait_s, self.backoff_base_s, self.backoff_max_s)
        if not all(math.isfinite(w) and w >= 0 for w in waits):
            raise ValueError("waits must be finite and >= 0 seconds")

    def backoff_s(self, attempt: int) -> float:
        """Seconds to wait before retry number ``attempt`` (1-based)."""
        return min(self.backoff_base_s * 2.0 ** (attempt - 1), self.backoff_max_s)


@dataclass(frozen=True, slots=True)
class RateLimit:
    """The ``Ratelimit-*`` headers of the latest Helix response.

    Attributes:
        limit: ``Ratelimit-Limit``: the bucket's size in points.
        remaining: ``Ratelimit-Remaining``: points left in the bucket.
        reset_epoch_s: ``Ratelimit-Reset``: Unix epoch time (seconds) when the bucket is full.
    """

    limit: int
    remaining: int
    reset_epoch_s: int


@dataclass(frozen=True, slots=True)
class Broadcaster:
    """A channel from Get Users: a row of the ``broadcasters`` table (data-model.md).

    Attributes:
        broadcaster_id: Twitch user ID (``id``), an opaque string.
        login: Login name.
        display_name: Display name.
        broadcaster_type: ``"partner"``, ``"affiliate"`` or ``""``.
        fetched_at: When the response was received (UTC).
    """

    broadcaster_id: str
    login: str
    display_name: str
    broadcaster_type: str
    fetched_at: datetime


@dataclass(frozen=True, slots=True)
class MutedSegment:
    """A part of a VOD whose audio Twitch muted, in seconds from VOD start."""

    offset_s: float
    duration_s: float


@dataclass(frozen=True, slots=True)
class Vod:
    """A video from Get Videos: a row of the ``vods`` table (data-model.md).

    Attributes:
        video_id: The video ID (``id``).
        broadcaster_id: The owner's user ID (``user_id``).
        stream_id: The source stream's ID; None for videos that aren't archives.
        title: The title.
        created_at: When Twitch created the video (UTC). Not a verified stream-clock origin.
        duration_s: Length in seconds, parsed from ``duration`` (``"3h8m33s"``).
        type: ``"archive"``, ``"highlight"`` or ``"upload"``.
        language: ISO 639-1 code, or ``"other"``.
        view_count: Views at ``fetched_at``.
        muted_segments: Muted parts; empty when Twitch reports ``null`` (nothing muted).
        game_ids: Always None here: Get Videos has no category field.
        fetched_at: When the response was received (UTC).
    """

    video_id: str
    broadcaster_id: str
    stream_id: str | None
    title: str
    created_at: datetime
    duration_s: float
    type: str
    language: str
    view_count: int
    muted_segments: tuple[MutedSegment, ...]
    game_ids: tuple[str, ...] | None
    fetched_at: datetime


@dataclass(frozen=True, slots=True)
class Listing[T]:
    """The items of a paginated listing.

    Attributes:
        items: The items in the order Twitch returned them.
        truncated: True if paging stopped at the caller's ``max_results`` while Twitch may have
            had more; False if Twitch reported the end of the list.
    """

    items: tuple[T, ...]
    truncated: bool


def parse_duration_s(text: str) -> float:
    """Parse a Helix video ``duration`` such as ``"3h8m33s"``, ``"45m"`` or ``"0s"`` to seconds.

    Twitch documents the format by example (``3m21s``). Accepted: hours, minutes and seconds as
    whole numbers, each optional but in that order, at least one present, and minutes or seconds
    below 60 when a larger unit precedes them. Anything else (``""``, ``"1.5s"``, ``"8m3h"``,
    ``"PT3M21S"``, spaces, signs) is rejected rather than guessed at.

    Raises:
        ValueError: ``text`` is not in that format.
    """
    match = _DURATION.fullmatch(text)
    if match is None or not text:
        raise ValueError(f"not a Twitch duration like '3h8m33s': {text!r}")
    hours, minutes, seconds = (int(group) if group else None for group in match.groups())
    if minutes is not None and hours is not None and minutes >= 60:
        raise ValueError(f"minutes must be below 60 after hours: {text!r}")
    if seconds is not None and (hours, minutes) != (None, None) and seconds >= 60:
        raise ValueError(f"seconds must be below 60 after hours or minutes: {text!r}")
    return float((hours or 0) * 3600 + (minutes or 0) * 60 + (seconds or 0))


def parse_timestamp(text: str) -> datetime:
    """Parse an RFC 3339 timestamp from Helix (``"2018-11-14T21:30:18Z"``) to an aware UTC time.

    Raises:
        ValueError: ``text`` isn't ISO 8601 or has no UTC offset.
    """
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp has no UTC offset")
    return parsed.astimezone(UTC)


class ResponseRecord:
    """One object from a Helix response's ``data`` list, with validated, typed field access.

    Errors are `TwitchResponseError`s naming the endpoint, the record's ``id`` and the field,
    never the field's value.
    """

    def __init__(self, raw: object, *, endpoint: str, index: int) -> None:
        if not isinstance(raw, dict):
            raise TwitchResponseError(f"{endpoint} returned data item {index} that isn't an object")
        self.raw: dict[str, object] = raw
        record_id = raw.get("id")
        label = f"id={record_id}" if isinstance(record_id, str) and record_id else f"#{index}"
        self._where = f"{endpoint} record {label}"

    def invalid(self, field: str, problem: str) -> TwitchResponseError:
        """Return (not raise) the error for a bad ``field``, e.g. ``invalid("x", "is missing")``."""
        return TwitchResponseError(f"{self._where}: field '{field}' {problem}")

    def _get(self, field: str) -> object:
        if field not in self.raw:
            raise self.invalid(field, "is missing")
        return self.raw[field]

    def require_str(self, field: str, *, allow_empty: bool = True) -> str:
        """Return a string field. Raises if it is missing, not a string or (if not allowed) ""."""
        value = self._get(field)
        if not isinstance(value, str):
            raise self.invalid(field, f"is {type(value).__name__}, expected a string")
        if not value and not allow_empty:
            raise self.invalid(field, "is empty")
        return value

    def optional_str(self, field: str) -> str | None:
        """Return a string field that may be ``null``. Raises if it is missing or another type."""
        return None if self._get(field) is None else self.require_str(field)

    def require_int(self, field: str) -> int:
        """Return a non-negative integer field (JSON booleans are rejected)."""
        value = self._get(field)
        if isinstance(value, bool) or not isinstance(value, int):
            raise self.invalid(field, f"is {type(value).__name__}, expected an integer")
        if value < 0:
            raise self.invalid(field, "is negative")
        return value

    def require_timestamp(self, field: str) -> datetime:
        """Return an RFC 3339 timestamp field as an aware UTC datetime."""
        try:
            return parse_timestamp(self.require_str(field))
        except ValueError:
            raise self.invalid(field, "is not an RFC 3339 timestamp with a UTC offset") from None

    def require_duration_s(self, field: str) -> float:
        """Return a ``"3h8m33s"``-style duration field in seconds."""
        try:
            return parse_duration_s(self.require_str(field))
        except ValueError:
            raise self.invalid(field, "is not a duration like '3h8m33s'") from None

    def list_or_empty(self, field: str) -> list[object]:
        """Return a list field, with ``null`` as ``[]``. Raises if missing or another type."""
        value = self._get(field)
        if value is None:
            return []
        if not isinstance(value, list):
            raise self.invalid(field, f"is {type(value).__name__}, expected a list or null")
        return list(value)


@dataclass(frozen=True, slots=True)
class HelixPage:
    """One Helix response: its ``data`` records, its next-page cursor and when it arrived."""

    records: tuple[ResponseRecord, ...]
    cursor: str | None
    fetched_at: datetime


@dataclass(frozen=True, slots=True)
class _AppToken:
    value: SecretStr
    refresh_at: datetime


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


class HelixClient:
    """A Twitch Helix client using an app access token.

    Create one per run and close it (or use it as a context manager). Credentials come from
    ``get_settings().require_twitch_credentials()`` in the entry point.

    Args:
        credentials: The app's Client ID and secret.
        http_client: The HTTP client to send requests with; tests pass one built on
            ``httpx.MockTransport``. If None, the client creates (and closes) its own with a
            `DEFAULT_TIMEOUT_S` timeout.
        clock: Returns the current time as an aware datetime; used for ``fetched_at``, token
            expiry and rate-limit waits.
        sleep: Waits the given number of seconds.
        retry_policy: Retry and wait bounds; defaults to `RetryPolicy()`.
    """

    def __init__(
        self,
        credentials: TwitchCredentials,
        *,
        http_client: httpx.Client | None = None,
        clock: Callable[[], datetime] = _utc_now,
        sleep: Callable[[float], None] = time.sleep,
        retry_policy: RetryPolicy | None = None,
    ) -> None:
        self._credentials = credentials
        self._owns_http = http_client is None
        self._http = httpx.Client(timeout=DEFAULT_TIMEOUT_S) if http_client is None else http_client
        self._clock = clock
        self._sleep = sleep
        self._policy = RetryPolicy() if retry_policy is None else retry_policy
        self._token: _AppToken | None = None
        self.rate_limit: RateLimit | None = None
        """The ``Ratelimit-*`` headers of the latest Helix response, if it had them."""

    def __repr__(self) -> str:
        return f"HelixClient(token_cached={self._token is not None}, rate_limit={self.rate_limit})"

    def close(self) -> None:
        """Close the HTTP client if this object created it."""
        if self._owns_http:
            self._http.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    # --- Endpoints ----------------------------------------------------------------------------

    def get_users(self, logins: Iterable[str]) -> list[Broadcaster]:
        """Resolve channel logins with Get Users, in the order given (duplicates dropped).

        Logins are matched case-insensitively and sent lowercased, in batches of
        `MAX_IDS_PER_REQUEST`.

        Raises:
            TwitchNotFoundError: Some logins don't exist; ``missing`` lists them.
            ValueError: ``logins`` is a single string, empty, or contains a blank login.
        """
        wanted = _unique_values(logins, "logins", normalize=lambda login: login.strip().lower())
        found: dict[str, Broadcaster] = {}
        for batch in _batches(wanted):
            page = self.get("/users", [("login", login) for login in batch])
            for record in page.records:
                user = _parse_broadcaster(record, page.fetched_at)
                found[user.login.lower()] = user
        missing = [login for login in wanted if login not in found]
        if missing:
            raise TwitchNotFoundError(
                f"no Twitch user with login {', '.join(missing)}", missing=missing
            )
        return [found[login] for login in wanted]

    def get_videos(self, video_ids: Iterable[str]) -> list[Vod]:
        """Fetch videos by ID with Get Videos, in the order given (duplicates dropped).

        Raises:
            TwitchNotFoundError: Some videos don't exist or aren't published; ``missing`` lists
                them.
            ValueError: ``video_ids`` is a single string, empty, or contains a blank ID.
        """
        wanted = _unique_values(video_ids, "video_ids", normalize=lambda video_id: video_id)
        found: dict[str, Vod] = {}
        for batch in _batches(wanted):
            try:
                page = self.get("/videos", [("id", video_id) for video_id in batch])
            except TwitchNotFoundError:
                continue  # Get Videos answers 404 only when none of the IDs exist.
            for record in page.records:
                video = _parse_vod(record, page.fetched_at)
                found[video.video_id] = video
        missing = [video_id for video_id in wanted if video_id not in found]
        if missing:
            raise TwitchNotFoundError(
                f"no Twitch video with ID {', '.join(missing)}", missing=missing
            )
        return [found[video_id] for video_id in wanted]

    def get_archive_videos(
        self, user_id: str, *, max_results: int = DEFAULT_MAX_RESULTS
    ) -> Listing[Vod]:
        """List a broadcaster's past broadcasts (``type=archive``), newest first.

        Args:
            user_id: The broadcaster's user ID.
            max_results: Stop after this many videos; ``truncated`` says whether more may exist.

        Raises:
            TwitchResponseError: Twitch returned a video that isn't an archive.
        """
        if not user_id.strip():
            raise ValueError("user_id is blank")
        listing = self.get_pages(
            "/videos", [("user_id", user_id), ("type", "archive")], max_results=max_results
        )
        videos: list[Vod] = []
        for page in listing.items:
            for record in page.records:
                video = _parse_vod(record, page.fetched_at)
                if video.type != "archive":
                    raise record.invalid("type", "is not 'archive' in a type=archive listing")
                videos.append(video)
        return Listing(tuple(videos), truncated=listing.truncated)

    # --- Reusable request core ----------------------------------------------------------------

    def get(self, path: str, params: Sequence[tuple[str, str]]) -> HelixPage:
        """Send one authorized ``GET`` to a Helix path such as ``"/videos"``.

        Handles the token, rate limits and retries described in the module docstring.

        Raises:
            TwitchAPIError: A subclass describing the failure.
        """
        if not path.startswith("/"):
            raise ValueError("path must start with '/'")
        endpoint = f"GET {path}"
        response = self._send("GET", HELIX_URL + path, endpoint, params=tuple(params))
        fetched_at = self._now()
        body = _json_object(response, endpoint)
        data = body.get("data")
        if not isinstance(data, list):
            raise TwitchResponseError(f"{endpoint} response has no 'data' list")
        records = tuple(
            ResponseRecord(item, endpoint=endpoint, index=i) for i, item in enumerate(data)
        )
        return HelixPage(records, _next_cursor(body, endpoint), fetched_at)

    def get_pages(
        self,
        path: str,
        params: Sequence[tuple[str, str]],
        *,
        max_results: int = DEFAULT_MAX_RESULTS,
        page_size: int = MAX_PAGE_SIZE,
    ) -> Listing[HelixPage]:
        """Page forward through a Helix list with ``first`` and the ``after`` cursor.

        Stops when Twitch returns no cursor or an empty page (Twitch's pagination guide: a
        cursor can lead to an empty page near the end), or once ``max_results`` records were
        collected; pages are trimmed so they hold at most ``max_results`` records in total.
        ``params`` holds the endpoint's filters; this method adds ``first`` and ``after``.

        Raises:
            ValueError: ``max_results`` < 1 or ``page_size`` outside 1..`MAX_PAGE_SIZE`.
            TwitchResponseError: Twitch returned the same cursor twice.
        """
        if max_results < 1:
            raise ValueError("max_results must be at least 1")
        if not 1 <= page_size <= MAX_PAGE_SIZE:
            raise ValueError(f"page_size must be between 1 and {MAX_PAGE_SIZE}")
        pages: list[HelixPage] = []
        seen_cursors: set[str] = set()
        total, cursor = 0, None
        while True:
            remaining = max_results - total
            query = [*params, ("first", str(min(page_size, remaining)))]
            if cursor is not None:
                query.append(("after", cursor))
            page = self.get(path, query)
            kept = page.records[:remaining]
            if kept:
                pages.append(replace(page, records=kept))
            total += len(kept)
            trimmed = len(page.records) > remaining
            if not page.records or page.cursor is None or total >= max_results:
                truncated = trimmed or (bool(page.records) and page.cursor is not None)
                log.debug(
                    "paged path=%s pages=%d n=%d truncated=%s", path, len(pages), total, truncated
                )
                return Listing(tuple(pages), truncated=truncated)
            if page.cursor in seen_cursors:
                raise TwitchResponseError(f"GET {path} returned the same pagination cursor twice")
            seen_cursors.add(page.cursor)
            cursor = page.cursor

    def _send(
        self,
        method: str,
        url: str,
        endpoint: str,
        *,
        params: tuple[tuple[str, str], ...] | None = None,
        data: dict[str, str] | None = None,
    ) -> httpx.Response:
        """Send a request with retries. Requests to `TOKEN_URL` go without a token."""
        authorized = url != TOKEN_URL
        rate_limited = transient_failures = 0
        token_refetched = False
        while True:
            headers: dict[str, str] = {}
            if authorized:
                self._wait_if_bucket_empty(endpoint)
                headers = self._auth_headers()
            try:
                response = self._http.request(
                    method, url, params=params, data=data, headers=headers
                )
            except httpx.TransportError as exc:
                transient_failures += 1
                self._backoff_or_raise(endpoint, transient_failures, type(exc).__name__, exc)
                continue
            except httpx.DecodingError as exc:
                # httpx decompresses the body while reading it; a corrupt body isn't a
                # TransportError, but it is a response Twitch sent in the wrong shape.
                raise TwitchResponseError(
                    f"{endpoint} returned a body that couldn't be decoded ({type(exc).__name__})"
                ) from exc
            status = response.status_code
            if authorized:
                self.rate_limit = _read_rate_limit(response, endpoint)
            log.debug(
                "twitch response endpoint=%s status=%d rate_limit=%s",
                endpoint,
                status,
                self.rate_limit,
            )
            if status == 401 and authorized:
                if token_refetched:
                    raise TwitchAuthError(
                        f"{endpoint} was rejected (401) again with a newly fetched app access "
                        f"token: {self._message(response)}. Check TWITCH_CLIENT_ID and "
                        "TWITCH_CLIENT_SECRET.",
                        status_code=status,
                    )
                token_refetched = True
                self._token = None
                log.info("twitch token rejected endpoint=%s status=401 action=refetch", endpoint)
                continue
            if status == 429:
                rate_limited += 1
                if rate_limited > self._policy.max_rate_limit_retries:
                    raise TwitchRateLimitError(
                        f"{endpoint} was still rate limited (429) after "
                        f"{self._policy.max_rate_limit_retries} retries",
                        status_code=status,
                    )
                self._wait_after_429(endpoint, rate_limited)
                continue
            if status >= 500:
                transient_failures += 1
                self._backoff_or_raise(
                    endpoint, transient_failures, f"status {status}", None, status_code=status
                )
                continue
            if 400 <= status < 500:
                raise self._client_error(endpoint, response, authorized=authorized)
            if not 200 <= status < 300:
                raise TwitchResponseError(f"{endpoint} returned unexpected status {status}")
            return response

    def _backoff_or_raise(
        self,
        endpoint: str,
        attempt: int,
        reason: str,
        cause: BaseException | None,
        *,
        status_code: int | None = None,
    ) -> None:
        if attempt > self._policy.max_transient_retries:
            raise TwitchUnavailableError(
                f"{endpoint} failed {attempt} times (last: {reason}); Twitch or the network may "
                "be down, try again later",
                status_code=status_code,
            ) from cause
        wait_s = self._policy.backoff_s(attempt)
        log.info(
            "retrying twitch request endpoint=%s reason=%r retry=%d wait_s=%.1f",
            endpoint,
            reason,
            attempt,
            wait_s,
        )
        self._sleep(wait_s)

    def _wait_after_429(self, endpoint: str, attempt: int) -> None:
        rate_limit = self.rate_limit
        if rate_limit is None:
            basis, wait_s = "backoff", self._policy.backoff_s(attempt)
        else:
            basis, wait_s = "ratelimit_reset", rate_limit.reset_epoch_s - self._now().timestamp()
        wait_s = min(max(wait_s, _MIN_RATE_LIMIT_WAIT_S), self._policy.max_rate_limit_wait_s)
        log.info(
            "twitch rate limited endpoint=%s status=429 retry=%d basis=%s wait_s=%.1f",
            endpoint,
            attempt,
            basis,
            wait_s,
        )
        self._sleep(wait_s)
        self.rate_limit = None  # waited for this reset already; don't wait for it again

    def _wait_if_bucket_empty(self, endpoint: str) -> None:
        rate_limit = self.rate_limit
        if rate_limit is None or rate_limit.remaining > 0:
            return
        wait_s = rate_limit.reset_epoch_s - self._now().timestamp()
        if wait_s > 0:
            wait_s = min(wait_s, self._policy.max_rate_limit_wait_s)
            log.info("twitch rate limit bucket empty endpoint=%s wait_s=%.1f", endpoint, wait_s)
            self._sleep(wait_s)
        self.rate_limit = None

    def _auth_headers(self) -> dict[str, str]:
        now = self._now()
        if self._token is None or now >= self._token.refresh_at:
            self._token = self._fetch_token()
        return {
            "Client-Id": self._credentials.client_id.get_secret_value(),
            "Authorization": f"Bearer {self._token.value.get_secret_value()}",
        }

    def _fetch_token(self) -> _AppToken:
        endpoint = "POST oauth2/token"
        # Twitch's client-credentials flow takes the credentials as a form body, which keeps
        # them out of URLs (and so out of httpx's request log lines).
        form = {
            "client_id": self._credentials.client_id.get_secret_value(),
            "client_secret": self._credentials.client_secret.get_secret_value(),
            "grant_type": "client_credentials",
        }
        body = _json_object(self._send("POST", TOKEN_URL, endpoint, data=form), endpoint)
        access_token, expires_in = body.get("access_token"), body.get("expires_in")
        if not isinstance(access_token, str) or not access_token:
            raise TwitchResponseError(f"{endpoint} response has no 'access_token' string")
        if isinstance(expires_in, bool) or not isinstance(expires_in, int) or expires_in <= 0:
            raise TwitchResponseError(f"{endpoint} response has no positive 'expires_in' integer")
        lifetime_s = min(expires_in, 365 * 24 * 3600)  # keeps the datetime arithmetic in range
        margin_s = min(TOKEN_EXPIRY_MARGIN_S, lifetime_s / 2)
        log.info("fetched twitch app access token expires_in_s=%d", expires_in)
        refresh_at = self._now() + timedelta(seconds=lifetime_s - margin_s)
        return _AppToken(SecretStr(access_token), refresh_at)

    def _client_error(
        self, endpoint: str, response: httpx.Response, *, authorized: bool
    ) -> TwitchAPIError:
        status, message = response.status_code, self._message(response)
        if not authorized:
            return TwitchAuthError(
                f"Twitch rejected the app credentials ({status}: {message}). Check "
                "TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET.",
                status_code=status,
            )
        if status == 404:
            return TwitchNotFoundError(f"{endpoint} found nothing ({message})", status_code=status)
        return TwitchRequestError(
            f"{endpoint} was rejected ({status}: {message})", status_code=status
        )

    def _message(self, response: httpx.Response) -> str:
        """Twitch's error ``message``, shortened, with any credential or token replaced."""
        try:
            body = response.json()
        except ValueError:
            body = None
        message = body.get("message") if isinstance(body, dict) else None
        if not isinstance(message, str) or not message:
            return "no error message"
        secrets = [self._credentials.client_id, self._credentials.client_secret]
        if self._token is not None:
            secrets.append(self._token.value)
        for secret in secrets:
            if value := secret.get_secret_value():
                message = message.replace(value, "[redacted]")
        return message[:_MAX_MESSAGE_CHARS]

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("the clock must return timezone-aware datetimes")
        return now.astimezone(UTC)


def _parse_broadcaster(record: ResponseRecord, fetched_at: datetime) -> Broadcaster:
    return Broadcaster(
        broadcaster_id=record.require_str("id", allow_empty=False),
        login=record.require_str("login", allow_empty=False),
        display_name=record.require_str("display_name"),
        broadcaster_type=record.require_str("broadcaster_type"),
        fetched_at=fetched_at,
    )


def _parse_vod(record: ResponseRecord, fetched_at: datetime) -> Vod:
    return Vod(
        video_id=record.require_str("id", allow_empty=False),
        broadcaster_id=record.require_str("user_id", allow_empty=False),
        stream_id=record.optional_str("stream_id"),
        title=record.require_str("title"),
        created_at=record.require_timestamp("created_at"),
        duration_s=record.require_duration_s("duration"),
        type=record.require_str("type", allow_empty=False),
        language=record.require_str("language"),
        view_count=record.require_int("view_count"),
        muted_segments=_parse_muted_segments(record),
        game_ids=None,
        fetched_at=fetched_at,
    )


def _parse_muted_segments(record: ResponseRecord) -> tuple[MutedSegment, ...]:
    """Parse ``muted_segments``: ``null`` means nothing is muted; absent is an error."""
    segments: list[MutedSegment] = []
    for i, item in enumerate(record.list_or_empty("muted_segments")):
        if not isinstance(item, dict):
            raise record.invalid(f"muted_segments[{i}]", "is not an object")
        seconds: dict[str, float] = {}
        for key in ("offset", "duration"):
            value = item.get(key)
            # Documented as integer seconds; any finite non-negative number is accepted.
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise record.invalid(f"muted_segments[{i}].{key}", "is missing or not a number")
            if not math.isfinite(value) or value < 0:
                raise record.invalid(f"muted_segments[{i}].{key}", "is negative or not finite")
            seconds[key] = float(value)
        segments.append(MutedSegment(offset_s=seconds["offset"], duration_s=seconds["duration"]))
    return tuple(segments)


def _read_rate_limit(response: httpx.Response, endpoint: str) -> RateLimit | None:
    raw = [response.headers.get(f"Ratelimit-{name}") for name in ("Limit", "Remaining", "Reset")]
    if all(value is None for value in raw):
        return None
    try:
        limit, remaining, reset = (int(value) for value in raw if value is not None)
    except ValueError:
        limit = remaining = reset = -1
    if min(limit, remaining, reset) < 0:
        log.warning("ignoring malformed Ratelimit headers endpoint=%s", endpoint)
        return None
    return RateLimit(limit=limit, remaining=remaining, reset_epoch_s=reset)


def _json_object(response: httpx.Response, endpoint: str) -> dict[str, object]:
    try:
        body = response.json()
    except ValueError:
        raise TwitchResponseError(f"{endpoint} response is not valid JSON") from None
    if not isinstance(body, dict):
        raise TwitchResponseError(f"{endpoint} response is not a JSON object")
    return body


def _next_cursor(body: dict[str, object], endpoint: str) -> str | None:
    """Return ``pagination.cursor``; an absent or empty pagination object means no next page."""
    pagination = body.get("pagination", {})
    if not isinstance(pagination, dict):
        raise TwitchResponseError(f"{endpoint} response 'pagination' is not an object")
    cursor = pagination.get("cursor")
    if cursor is not None and not isinstance(cursor, str):
        raise TwitchResponseError(f"{endpoint} response 'pagination.cursor' is not a string")
    return cursor or None


def _unique_values(
    values: Iterable[str], name: str, *, normalize: Callable[[str], str]
) -> list[str]:
    if isinstance(values, str):
        raise ValueError(f"{name} must be a collection of strings, not one string")
    unique = list(dict.fromkeys(normalize(value) for value in values))
    if not unique:
        raise ValueError(f"{name} is empty")
    if any(not value.strip() for value in unique):
        raise ValueError(f"{name} contains a blank value")
    return unique


def _batches(values: list[str]) -> Iterable[list[str]]:
    for start in range(0, len(values), MAX_IDS_PER_REQUEST):
        yield values[start : start + MAX_IDS_PER_REQUEST]
