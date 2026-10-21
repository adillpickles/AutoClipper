"""Live smoke test of the Helix client against the real Twitch API.

Deselected by default (tests/conftest.py); run with ``uv run pytest -m live`` only with the app
credentials in ``.env`` and the source authorization in docs/data/data-governance.md. It reads
public metadata of Twitch's own developer channel and stores nothing.
"""

import pytest

from autoclipper.config import MissingSettingError, get_settings
from autoclipper.ingest.twitch_api import HelixClient

# Twitch's developer-relations channel, the example account in Twitch's API reference.
_PUBLIC_LOGIN = "twitchdev"


@pytest.mark.live
def test_helix_client_resolves_a_channel_and_lists_its_archives() -> None:
    try:
        credentials = get_settings().require_twitch_credentials()
    except MissingSettingError as exc:
        pytest.skip(f"needs Twitch app credentials: {exc}")

    with HelixClient(credentials) as client:
        (user,) = client.get_users([_PUBLIC_LOGIN])
        listing = client.get_archive_videos(user.broadcaster_id, max_results=1)

    assert user.login == _PUBLIC_LOGIN
    assert isinstance(user.broadcaster_id, str)
    assert user.broadcaster_id
    assert len(listing.items) <= 1
    for vod in listing.items:
        assert vod.broadcaster_id == user.broadcaster_id
        assert vod.type == "archive"
        assert vod.duration_s >= 0
