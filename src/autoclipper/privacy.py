"""Pseudonymization of Twitch user identifiers (docs/data/data-governance.md §2).

`pseudonymize` is the single implementation behind the ``_hash`` columns in
docs/architecture/data-model.md (``creator_hash`` in ``clips``, ``user_hash`` in
``chat_messages``): an HMAC-SHA256 keyed with the secret salt, as a lowercase hex digest. Ingestion
code gets the salt from ``get_settings().require_chat_hash_salt()`` and passes it in.

This module only provides the primitive. Whether hashes may be kept, shared or joined across
tables and machines is open question OQ-9 (docs/project/open-questions.md). Note that with one
salt, the same user ID gets the same hash in every table that uses it.
"""

import hashlib
import hmac

from pydantic import SecretStr


def pseudonymize(raw_id: str, salt: SecretStr) -> str:
    """Return the salted HMAC-SHA256 digest that stands in for a Twitch user identifier.

    Computes ``HMAC-SHA256(key=salt, message=raw_id)`` over the UTF-8 bytes of both. The
    identifier is hashed exactly as given: no trimming and no case folding. Twitch IDs are opaque
    strings (docs/development/conventions.md §3), and normalizing them could merge different
    users. Prefer the stable user ID where the source has one, since logins can be renamed. A
    caller that must hash something else (such as a login) normalizes it the same way everywhere.

    Args:
        raw_id: The identifier to hide, e.g. the user ID ``"123456789"``.
        salt: The secret HMAC key (``AUTOCLIPPER_CHAT_HASH_SALT``).

    Returns:
        64 lowercase hexadecimal characters. The same ``raw_id`` and ``salt`` always give the
        same digest; a different salt gives a different one.

    Raises:
        ValueError: ``raw_id`` is empty, or ``salt`` is empty or only whitespace. The message
            never contains either value.
    """
    if not raw_id:
        # One shared digest for every missing ID would silently merge unrelated users.
        raise ValueError("raw_id is empty; handle a missing user ID before pseudonymizing")
    key = salt.get_secret_value()
    if not key.strip():
        raise ValueError("the salt is empty or blank; set AUTOCLIPPER_CHAT_HASH_SALT")
    return hmac.new(key.encode("utf-8"), raw_id.encode("utf-8"), hashlib.sha256).hexdigest()
