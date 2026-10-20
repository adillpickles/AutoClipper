"""AutoClipper's settings, read from environment variables and a ``.env`` file.

This is the only module that reads the environment (docs/development/conventions.md §4). Entry
points (CLI commands, later the API app) call `get_settings()`. Library functions take the values
they need (a directory, `TwitchCredentials`, the hash salt) as parameters, so tests can pass them
in directly.

Each setting is looked up in this order, and the first source that has a value wins:

1. The process environment.
2. The ``.env`` file in the current working directory. Parent directories are not searched, so
   run ``autoclipper`` from the directory that holds your ``.env`` (normally the repository root).
3. The defaults on `Settings`.

An empty value counts as unset, so the blank ``TWITCH_CLIENT_ID=`` lines copied from
``.env.example`` mean "no credential", not an empty one. Settings are read once per process:
`get_settings()` caches its result, and tests reset it with ``get_settings.cache_clear()``.
"""

import functools
import io
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from dotenv.parser import parse_stream
from pydantic import Field, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
"""The values ``AUTOCLIPPER_LOG_LEVEL`` accepts, in any letter case."""

ENV_FILE: Final = Path(".env")
"""The dotenv file `get_settings()` reads, relative to the current working directory."""

_SETUP_DOCS: Final = "docs/development/setup.md"


class ConfigError(Exception):
    """The settings are invalid, or a setting that an operation needs is missing.

    Messages name the environment variables involved but never their values, which may be
    secrets.
    """


class MissingSettingError(ConfigError):
    """A setting that the requested operation needs is unset or blank.

    Attributes:
        names: The environment variables that need a value, e.g. ``("TWITCH_CLIENT_ID",)``.
    """

    def __init__(self, names: Sequence[str], *, needed_for: str, docs: str) -> None:
        self.names: tuple[str, ...] = tuple(names)
        verb, pronoun = ("is", "it") if len(self.names) == 1 else ("are", "them")
        super().__init__(
            f"{' and '.join(self.names)} {verb} not set, but {needed_for} needs {pronoun}. Set "
            f"{pronoun} in the environment or in the .env file in the directory you run "
            f"autoclipper from (copy .env.example), as described in {docs}."
        )


@dataclass(frozen=True, slots=True)
class TwitchCredentials:
    """The Twitch application's credentials for the client-credentials flow.

    Both values are `SecretStr`, so printing or logging this object shows ``**********``. Call
    ``get_secret_value()`` only where the value is sent to Twitch.

    Attributes:
        client_id: The application's Client ID (``TWITCH_CLIENT_ID``).
        client_secret: The application's Client Secret (``TWITCH_CLIENT_SECRET``).
    """

    client_id: SecretStr
    client_secret: SecretStr


class Settings(BaseSettings):
    """Every AutoClipper setting. Each field maps to one variable in ``.env.example``.

    Get it from `get_settings()`, which turns invalid values into a `ConfigError`; constructing
    ``Settings()`` directly raises pydantic's ``ValidationError`` instead, whose message repeats
    the rejected values. Instances are immutable. Tests that need specific values set
    environment variables or write ``.env`` in the working directory, then clear the cache.

    Attributes:
        twitch_client_id: ``TWITCH_CLIENT_ID``, or None if unset. Code that calls the Helix API
            uses `require_twitch_credentials` instead of reading it directly.
        twitch_client_secret: ``TWITCH_CLIENT_SECRET``, or None if unset.
        data_dir: ``AUTOCLIPPER_DATA_DIR``: root for raw, interim and processed data
            (docs/architecture/overview.md §5). Defaults to ``data``.
        artifacts_dir: ``AUTOCLIPPER_ARTIFACTS_DIR``: root for trained models, evaluation
            reports and exported clips. Defaults to ``artifacts``.
        chat_hash_salt: ``AUTOCLIPPER_CHAT_HASH_SALT``: the secret HMAC key for pseudonymizing
            Twitch user IDs, or None if unset. Use `require_chat_hash_salt` to get it.
        log_level: ``AUTOCLIPPER_LOG_LEVEL``: minimum level of AutoClipper's own log records.
            Defaults to ``INFO``.

    Both directories are absolute. A relative value, including the defaults, is taken relative to
    the working directory at the time the settings are loaded (the directory ``.env`` is read
    from), and ``~`` expands to the home directory. Loading never creates the directories.
    """

    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        # Explicit, so reading .env never depends on the Windows locale's code page. python-dotenv
        # drops a leading byte-order mark (Windows PowerShell 5.1 writes one); a test checks it.
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        # A misspelled or unknown variable in .env is an error instead of being silently ignored.
        # (Unknown names in the process environment can't be checked; it holds every program's
        # variables.)
        extra="forbid",
        frozen=True,
        # Run the validators on defaults too, so the default directories become absolute.
        validate_default=True,
        # Fields are read only under their environment variable names. Don't enable
        # `validate_by_name`: pydantic-settings would then also read bare variables such as
        # DATA_DIR or LOG_LEVEL, which other programs set.
    )

    # SecretStr for the Client ID too: Twitch treats it as public, but the team shares the pair
    # privately (docs/development/setup.md), and keeping it out of logs costs nothing.
    twitch_client_id: SecretStr | None = Field(default=None, validation_alias="TWITCH_CLIENT_ID")
    twitch_client_secret: SecretStr | None = Field(
        default=None, validation_alias="TWITCH_CLIENT_SECRET"
    )
    data_dir: Path = Field(default=Path("data"), validation_alias="AUTOCLIPPER_DATA_DIR")
    artifacts_dir: Path = Field(
        default=Path("artifacts"), validation_alias="AUTOCLIPPER_ARTIFACTS_DIR"
    )
    chat_hash_salt: SecretStr | None = Field(
        default=None, validation_alias="AUTOCLIPPER_CHAT_HASH_SALT"
    )
    log_level: LogLevel = Field(default="INFO", validation_alias="AUTOCLIPPER_LOG_LEVEL")

    @field_validator("twitch_client_id", "twitch_client_secret", "chat_hash_salt", mode="before")
    @classmethod
    def _blank_secret_is_unset(cls, value: object) -> object:
        # Empty values never reach here (env_ignore_empty). A whitespace-only value is no more
        # usable as a credential or salt, so it is unset too. Other values are kept verbatim.
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("data_dir", "artifacts_dir", mode="before")
    @classmethod
    def _check_path_text(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        if not value.strip():
            raise ValueError(
                "must be a directory path; leave it empty or unset it to use the default"
            )
        if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
            # python-dotenv decodes escapes inside double quotes, so "C:\new" becomes "C:" + a
            # newline + "ew". Directory names never contain control characters.
            raise ValueError(
                "contains a control character such as a newline or tab. In .env, double quotes "
                "turn backslash sequences like \\n and \\t in Windows paths into control "
                "characters: write the path without quotes or with forward slashes"
            )
        return value

    @field_validator("data_dir", "artifacts_dir", mode="after")
    @classmethod
    def _make_absolute(cls, value: Path) -> Path:
        # Anchoring relative paths once, at load time, keeps them stable if the process changes
        # directory later. `absolute()` does not resolve symlinks or mapped drives, so a path
        # that is already absolute stays exactly as written.
        try:
            expanded = value.expanduser()
        except RuntimeError as exc:  # pathlib raises this when the home directory is unknown
            raise ValueError(f"starts with ~ but the home directory is unknown ({exc})") from None
        if sys.platform == "win32" and expanded.root and not expanded.drive:
            # On Windows "\server\share" and "/data" are relative to the current drive, and
            # `absolute()` would quietly put them on it. The usual cause is a network path that
            # lost a backslash: inside quotes, python-dotenv decodes "\\" to "\".
            raise ValueError(
                "starts with a slash or backslash but has no drive letter, so it would point to "
                "the drive you happen to run from. Include the drive letter, or for a network "
                "share write \\\\server\\share\\folder without quotes (in .env, quotes turn \\\\ "
                "into \\) or //server/share/folder"
            )
        return expanded.absolute()

    @field_validator("log_level", mode="before")
    @classmethod
    def _normalize_log_level(cls, value: object) -> object:
        # Accept "info" and stray spaces; the type then rejects anything that isn't a level name.
        return value.strip().upper() if isinstance(value, str) else value

    def require_twitch_credentials(self) -> TwitchCredentials:
        """Return the Twitch app credentials, for code that calls the Helix API.

        Credentials are optional when settings load, so commands that never call Twitch work
        without them. Call this where they are needed, so a missing one fails with a clear
        message before any request is made.

        Raises:
            MissingSettingError: ``TWITCH_CLIENT_ID`` or ``TWITCH_CLIENT_SECRET`` is unset or
                blank. Its ``names`` lists the missing ones.
        """
        client_id, client_secret = self.twitch_client_id, self.twitch_client_secret
        if client_id is None or client_secret is None:
            missing = [
                name
                for name, value in (
                    ("TWITCH_CLIENT_ID", client_id),
                    ("TWITCH_CLIENT_SECRET", client_secret),
                )
                if value is None
            ]
            raise MissingSettingError(
                missing,
                needed_for="calling the Twitch API",
                docs=f"{_SETUP_DOCS} § Twitch credentials",
            )
        return TwitchCredentials(client_id=client_id, client_secret=client_secret)

    def require_chat_hash_salt(self) -> SecretStr:
        """Return the salt for `autoclipper.privacy.pseudonymize`.

        Raises:
            MissingSettingError: ``AUTOCLIPPER_CHAT_HASH_SALT`` is unset or blank.
        """
        if self.chat_hash_salt is None:
            raise MissingSettingError(
                ["AUTOCLIPPER_CHAT_HASH_SALT"],
                needed_for="pseudonymizing Twitch user IDs",
                docs=f"{_SETUP_DOCS} § Chat hash salt",
            )
        return self.chat_hash_salt


@functools.cache
def get_settings() -> Settings:
    """Load the settings on first call and return the same `Settings` on every later call.

    Tests that change the environment or ``.env`` call ``get_settings.cache_clear()`` so the next
    call reads them again. A failed load is not cached.

    Raises:
        ConfigError: A value is invalid, ``.env`` contains a variable that is not an AutoClipper
            setting, or ``.env`` can't be read or parsed. The message names each problem by
            variable name or line number.
    """
    _check_env_file(ENV_FILE)
    try:
        return Settings()
    except ValidationError as exc:
        # `from None` keeps pydantic's error out of tracebacks: its message repeats every rejected
        # input value, and an unknown .env line may hold a secret.
        raise ConfigError(_describe_invalid_settings(exc)) from None


def _check_env_file(path: Path) -> None:
    """Reject a ``.env`` file that would be silently misread.

    pydantic-settings reads ``.env`` with python-dotenv, which only logs a warning for a line it
    can't parse (e.g. an unterminated quote) and then drops that setting, so a command would run
    with the default instead. This parses the file with the same parser first and fails instead.
    Messages name line numbers, never contents: a line may hold a secret.

    Raises:
        ConfigError: The file can't be read, isn't UTF-8, or has lines that can't be parsed.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return
    except UnicodeDecodeError:
        raise ConfigError(
            f"{path.absolute()} is not valid UTF-8 text. Save it as UTF-8 (see .env.example)."
        ) from None
    except OSError as exc:
        raise ConfigError(f"can't read {path.absolute()}: {exc.strerror}") from None
    bad_lines = [
        str(binding.original.line) for binding in parse_stream(io.StringIO(text)) if binding.error
    ]
    if bad_lines:
        raise ConfigError(
            f"{path.absolute()} has lines that can't be parsed (line {', '.join(bad_lines)}). "
            "Check for unbalanced quotes; each line should look like NAME=value. "
            f"See .env.example and {_SETUP_DOCS} § Configuration."
        )


def _describe_invalid_settings(exc: ValidationError) -> str:
    problems: list[str] = []
    for error in exc.errors(include_url=False, include_context=False, include_input=False):
        # Known fields report their variable name; an unknown .env key arrives lower-cased.
        name = ".".join(str(part) for part in error["loc"]).upper()
        if error["type"] == "extra_forbidden":
            problem = "not an AutoClipper setting (check the name against .env.example)"
        else:
            problem = error["msg"].removeprefix("Value error, ")
        problems.append(f"  {name}: {problem}")
    return (
        f"invalid settings in the environment or {ENV_FILE.absolute()}:\n"
        + "\n".join(problems)
        + f"\nSee .env.example and {_SETUP_DOCS} § Configuration."
    )
