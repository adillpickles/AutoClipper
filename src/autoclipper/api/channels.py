"""Channel VOD listing endpoint (docs/architecture/api-contract.md § 9.7).

Resolves a Twitch channel link or login via Helix Get Users, checks whether the channel is
currently live via Get Streams so an in-progress recording isn't offered as a past broadcast,
and lists recent archive VODs via Get Videos (paginated up to `limit`). Marks VODs that already
have an analysis result on disk or a finished job in the current server session. In ``mock``
mode, serves matching synthetic scenarios without requiring Twitch credentials.
"""

import logging
import re
import threading
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from pathlib import Path
from typing import Annotated, Final

from fastapi import FastAPI, Query, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from autoclipper.analyze import result_path
from autoclipper.api import mock
from autoclipper.api.service import ApiException, MockService
from autoclipper.config import MissingSettingError, TwitchCredentials, get_settings
from autoclipper.ingest.twitch_api import (
    Broadcaster,
    HelixClient,
    TwitchAPIError,
    TwitchAuthError,
    TwitchNotFoundError,
    TwitchRateLimitError,
    Vod,
    resolve_thumbnail_url,
)
from autoclipper.schemas import (
    ChannelInfo,
    ChannelVod,
    ChannelVodsResponse,
    parse_channel,
    parse_vod,
)

log = logging.getLogger(__name__)

DEFAULT_CHANNEL_VOD_LIMIT: Final = 20
MAX_CHANNEL_VOD_LIMIT: Final = 100
# Twitch Get Videos reference specifies 320x180 for VOD thumbnails (docs/data/data-sources.md § 1).
THUMBNAIL_WIDTH: Final = 320
THUMBNAIL_HEIGHT: Final = 180

_JSON_TYPE: Final = "application/json; charset=utf-8"
_NO_STORE: Final = {"Cache-Control": "no-store"}
_INT_QUERY_PATTERN: Final = re.compile(r"^[+-]?[0-9]+$")

type HelixFactory = Callable[[], HelixClient]


class ChannelVodsQuery(BaseModel):
    """Strict query parameters for ``GET /api/v1/channels/vods`` (§ 1, § 9.7)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    channel: str = Field(description="Twitch channel URL or login.")
    limit: int = Field(
        default=DEFAULT_CHANNEL_VOD_LIMIT,
        ge=1,
        le=MAX_CHANNEL_VOD_LIMIT,
        description="Maximum number of recent archive VODs to return.",
    )

    @field_validator("limit", mode="before")
    @classmethod
    def _strict_int_query(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("limit must be an integer")
        if isinstance(value, str):
            stripped = value.strip()
            if not _INT_QUERY_PATTERN.fullmatch(stripped):
                raise ValueError("limit must be an integer")
            return int(stripped)
        return value


class _SharedHelix:
    """Concurrency-safe session cache for a `HelixClient`, reusing its app token across lookups."""

    def __init__(
        self,
        *,
        credentials_provider: Callable[[], TwitchCredentials] | None = None,
        client_builder: Callable[[TwitchCredentials], HelixClient] = HelixClient,
    ) -> None:
        self._credentials_provider = (
            credentials_provider
            if credentials_provider is not None
            else lambda: get_settings().require_twitch_credentials()
        )
        self._client_builder = client_builder
        self._lock = threading.Lock()
        self._credentials: TwitchCredentials | None = None
        self._client: HelixClient | None = None

    def run[T](self, fn: Callable[[HelixClient], T]) -> T:
        with self._lock:
            creds = self._credentials_provider()
            if self._client is None or self._credentials != creds:
                if self._client is not None:
                    self._client.close()
                self._client = self._client_builder(creds)
                self._credentials = creds
            return fn(self._client)

    def close(self) -> None:
        with self._lock:
            if self._client is not None:
                self._client.close()
                self._client = None
                self._credentials = None


def _has_saved_analysis(artifacts_dir: Path, video_id: str) -> bool:
    """True when an analysis result JSON already exists on disk for `video_id`."""
    try:
        return result_path(artifacts_dir, video_id).is_file()
    except ValueError:
        return False


def list_channel_vods(
    service: MockService,
    channel: str,
    *,
    limit: int = DEFAULT_CHANNEL_VOD_LIMIT,
    helix_factory: HelixFactory | None = None,
    shared_helix: _SharedHelix | None = None,
) -> ChannelVodsResponse:
    """Resolve `channel` and return up to `limit` completed archive VODs with analyzed status.

    Raises:
        ApiException: With the contract's § 8 / § 9.7 status and error code on invalid input,
            missing Helix credentials, unknown channel, auth error, or Twitch failure.
    """
    login = parse_channel(channel)
    if login is None:
        if parse_vod(channel) is not None:
            message = (
                "That's a VOD link or ID, not a channel. Paste a channel link like "
                "twitch.tv/<name> or the channel name."
            )
        else:
            message = (
                "That isn't a Twitch channel link or login. Paste a link like "
                "twitch.tv/<name> or just the channel name."
            )
        raise ApiException(422, "invalid_channel", message)

    if helix_factory is None and service.job_runner == "mock":
        outcome = mock.channel_scenario_for(
            login, limit=limit, done_jobs=service.done_jobs_by_video()
        )
        if outcome.error is not None:
            assert outcome.error_status is not None
            raise ApiException(
                outcome.error_status,
                outcome.error.code,
                outcome.error.message,
                outcome.error.details,
            )
        assert outcome.response is not None
        return outcome.response

    def _fetch(helix: HelixClient) -> tuple[Broadcaster, list[Vod], bool]:
        (resolved,) = helix.get_users([login])
        live_stream_id = helix.get_live_stream_id(resolved.broadcaster_id)
        fetch_limit = limit + 1 if live_stream_id is not None else limit
        listing = helix.get_archive_videos(resolved.broadcaster_id, max_results=fetch_limit)
        completed = [
            vod
            for vod in listing.items
            if live_stream_id is None or vod.stream_id != live_stream_id
        ]
        is_truncated = listing.truncated or len(completed) > limit
        return resolved, completed[:limit], is_truncated

    try:
        if helix_factory is not None:
            with helix_factory() as helix:
                broadcaster, vod_items, truncated = _fetch(helix)
        else:
            cache = shared_helix if shared_helix is not None else _SharedHelix()
            ctx: AbstractContextManager[object] = (
                nullcontext() if shared_helix is not None else _close_on_exit(cache)
            )
            with ctx:
                broadcaster, vod_items, truncated = cache.run(_fetch)
    except MissingSettingError as exc:
        raise ApiException(
            503,
            "twitch_credentials_missing",
            "Twitch API credentials aren't configured on this server. Set "
            "TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET in .env to browse channel VODs.",
            {"missing": list(exc.names)},
        ) from None
    except TwitchNotFoundError:
        raise ApiException(
            404,
            "channel_not_found",
            "No Twitch channel with that name was found. Check the spelling and try again.",
        ) from None
    except TwitchAuthError as exc:
        log.warning("twitch auth failed during channel lookup login=%s: %s", login, exc)
        raise ApiException(
            502,
            "twitch_auth_failed",
            "Twitch rejected this server's API credentials. Check TWITCH_CLIENT_ID and "
            "TWITCH_CLIENT_SECRET and try again.",
        ) from None
    except TwitchRateLimitError as exc:
        log.warning("twitch rate limit during channel lookup login=%s: %s", login, exc)
        raise ApiException(
            503,
            "upstream_unavailable",
            "Twitch rate-limited this request. Wait a moment and try again.",
        ) from None
    except TwitchAPIError as exc:
        log.warning("twitch API error during channel lookup login=%s: %s", login, exc)
        raise ApiException(
            502,
            "upstream_unavailable",
            "Twitch isn't responding right now. Try again in a moment.",
        ) from None

    # Read completed jobs after the Twitch round trip so jobs that finished mid-lookup are shown.
    done_jobs = service.done_jobs_by_video()
    artifacts_dir = (
        service.artifacts_dir if service.artifacts_dir is not None else service.output_dir
    )
    vods = tuple(
        ChannelVod(
            video_id=vod.video_id,
            title=vod.title,
            created_at=vod.created_at,
            duration_s=vod.duration_s,
            thumbnail_url=resolve_thumbnail_url(
                vod.thumbnail_url, width=THUMBNAIL_WIDTH, height=THUMBNAIL_HEIGHT
            ),
            url=f"https://www.twitch.tv/videos/{vod.video_id}",
            view_count=vod.view_count,
            analyzed=(
                vod.video_id in done_jobs or _has_saved_analysis(artifacts_dir, vod.video_id)
            ),
            last_job_id=done_jobs.get(vod.video_id),
        )
        for vod in vod_items
    )
    return ChannelVodsResponse(
        channel=ChannelInfo(
            broadcaster_id=broadcaster.broadcaster_id,
            login=broadcaster.login,
            display_name=broadcaster.display_name,
            profile_image_url=broadcaster.profile_image_url,
        ),
        vods=vods,
        truncated=truncated,
    )


class _close_on_exit:
    """Small context manager that closes a one-shot `_SharedHelix`."""

    def __init__(self, target: _SharedHelix) -> None:
        self._target = target

    def __enter__(self) -> _SharedHelix:
        return self._target

    def __exit__(self, *_: object) -> None:
        self._target.close()


def register_channel_routes(
    app: FastAPI,
    service: MockService,
    *,
    api_prefix: str,
    helix_factory: HelixFactory | None = None,
) -> Callable[[], None]:
    """Register channel browsing routes on `app` and return a shutdown callback."""
    shared_helix = _SharedHelix() if helix_factory is None else None

    @app.get(f"{api_prefix}/channels/vods", response_model=ChannelVodsResponse)
    def get_channel_vods(query: Annotated[ChannelVodsQuery, Query()]) -> Response:
        result = list_channel_vods(
            service,
            query.channel,
            limit=query.limit,
            helix_factory=helix_factory,
            shared_helix=shared_helix,
        )
        return Response(
            content=result.model_dump_json(),
            status_code=200,
            media_type=_JSON_TYPE,
            headers=_NO_STORE,
        )

    def close() -> None:
        if shared_helix is not None:
            shared_helix.close()

    return close
