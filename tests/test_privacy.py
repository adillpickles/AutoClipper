"""Tests for autoclipper.privacy: the must-have pseudonymization tests in testing.md."""

import hashlib
import hmac
import re

import pytest
from pydantic import SecretStr

from autoclipper.config import get_settings
from autoclipper.privacy import pseudonymize

SALT = SecretStr("fake-salt-c7a0")
OTHER_SALT = SecretStr("fake-salt-different-31d5")
USER_ID = "123456789"
HEX_DIGEST = re.compile(r"[0-9a-f]{64}")


def test_matches_the_published_hmac_sha256_test_vector() -> None:
    # Well-known HMAC-SHA256 example: key "key", message "The quick brown fox jumps over the
    # lazy dog". Pins the algorithm, which argument is the key, and the lowercase hex output.
    digest = pseudonymize("The quick brown fox jumps over the lazy dog", SecretStr("key"))

    assert digest == "f7bc83f430538424b13298e6aa6fb143ef4d59a14946175997479dbc2d1a3cd8"


def test_same_id_and_salt_give_the_same_hash() -> None:
    assert pseudonymize(USER_ID, SALT) == pseudonymize(USER_ID, SecretStr("fake-salt-c7a0"))


def test_different_salt_gives_a_different_hash() -> None:
    assert pseudonymize(USER_ID, SALT) != pseudonymize(USER_ID, OTHER_SALT)


def test_different_ids_give_different_hashes() -> None:
    assert pseudonymize(USER_ID, SALT) != pseudonymize("123456780", SALT)


@pytest.mark.parametrize("raw_id", [USER_ID, "user_001", "Ünïcödé_user_002"])
def test_output_is_a_hex_digest_without_the_raw_value(raw_id: str) -> None:
    digest = pseudonymize(raw_id, SALT)

    assert HEX_DIGEST.fullmatch(digest)
    assert raw_id not in digest
    assert raw_id.lower() not in digest


@pytest.mark.parametrize(
    ("raw_id", "variant"),
    [
        pytest.param("123", " 123", id="leading-space"),
        pytest.param("123", "0123", id="leading-zero"),
        pytest.param("user_001", "USER_001", id="letter-case"),
    ],
)
def test_ids_are_hashed_exactly_as_given(raw_id: str, variant: str) -> None:
    assert pseudonymize(raw_id, SALT) != pseudonymize(variant, SALT)


def test_text_is_hashed_as_utf_8() -> None:
    raw_id = "Ünïcödé_user_002"
    expected = hmac.new(b"fake-salt-c7a0", raw_id.encode("utf-8"), hashlib.sha256).hexdigest()

    assert pseudonymize(raw_id, SALT) == expected


@pytest.mark.parametrize("salt", ["", "   "])
def test_empty_or_blank_salt_is_rejected(salt: str) -> None:
    with pytest.raises(ValueError, match="AUTOCLIPPER_CHAT_HASH_SALT"):
        pseudonymize(USER_ID, SecretStr(salt))


def test_empty_id_is_rejected_without_hashing() -> None:
    with pytest.raises(ValueError, match="raw_id is empty"):
        pseudonymize("", SALT)


def test_error_messages_do_not_contain_the_salt() -> None:
    with pytest.raises(ValueError, match="raw_id") as excinfo:
        pseudonymize("", SALT)

    assert SALT.get_secret_value() not in str(excinfo.value)


def test_works_with_the_configured_salt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTOCLIPPER_CHAT_HASH_SALT", "fake-salt-c7a0")

    salt = get_settings().require_chat_hash_salt()

    assert pseudonymize(USER_ID, salt) == pseudonymize(USER_ID, SALT)
