"""Tests for autoclipper.config.

The autouse fixture in conftest.py runs each test in an empty working directory (``tmp_path``)
with no AutoClipper variables in the environment, so a test that writes ``tmp_path / ".env"``
controls exactly what `get_settings` sees.
"""

import ast
import sys
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from autoclipper.config import (
    ConfigError,
    MissingSettingError,
    Settings,
    TwitchCredentials,
    get_settings,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
ENV_EXAMPLE = REPO_ROOT / ".env.example"
PACKAGE_DIR = REPO_ROOT / "src" / "autoclipper"
BACKSLASH = "\\"

# Obviously fake values that are easy to search for in output.
FAKE_CLIENT_ID = "fake-client-id-4f1d"
FAKE_CLIENT_SECRET = "fake-client-secret-9b2e"
FAKE_SALT = "fake-salt-c7a0"

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows path semantics")


def write_env_file(directory: Path, text: str, *, encoding: str = "utf-8") -> None:
    (directory / ".env").write_text(text, encoding=encoding)


def set_all_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TWITCH_CLIENT_ID", FAKE_CLIENT_ID)
    monkeypatch.setenv("TWITCH_CLIENT_SECRET", FAKE_CLIENT_SECRET)
    monkeypatch.setenv("AUTOCLIPPER_CHAT_HASH_SALT", FAKE_SALT)


def env_example_names() -> set[str]:
    names: set[str] = set()
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            names.add(stripped.partition("=")[0].strip())
    return names


# --- Defaults and sources ------------------------------------------------------------------


def test_defaults_without_environment_or_env_file(tmp_path: Path) -> None:
    settings = get_settings()

    assert settings.twitch_client_id is None
    assert settings.twitch_client_secret is None
    assert settings.chat_hash_salt is None
    assert settings.log_level == "INFO"
    assert settings.data_dir == tmp_path / "data"
    assert settings.artifacts_dir == tmp_path / "artifacts"


def test_every_env_example_variable_is_a_setting() -> None:
    aliases = {
        alias
        for field in Settings.model_fields.values()
        if isinstance(alias := field.validation_alias, str)
    }

    assert len(aliases) == len(Settings.model_fields)  # one variable name per field
    assert env_example_names() == aliases


def test_a_copy_of_env_example_gives_the_defaults(tmp_path: Path) -> None:
    defaults = get_settings().model_dump()
    get_settings.cache_clear()
    (tmp_path / ".env").write_bytes(ENV_EXAMPLE.read_bytes())

    assert get_settings().model_dump() == defaults


def test_environment_variables_set_every_field(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_all_secrets(monkeypatch)
    monkeypatch.setenv("AUTOCLIPPER_DATA_DIR", str(tmp_path / "custom-data"))
    monkeypatch.setenv("AUTOCLIPPER_ARTIFACTS_DIR", str(tmp_path / "custom-artifacts"))
    monkeypatch.setenv("AUTOCLIPPER_LOG_LEVEL", "DEBUG")

    settings = get_settings()

    assert settings.require_twitch_credentials() == TwitchCredentials(
        client_id=SecretStr(FAKE_CLIENT_ID), client_secret=SecretStr(FAKE_CLIENT_SECRET)
    )
    assert settings.require_chat_hash_salt().get_secret_value() == FAKE_SALT
    assert settings.data_dir == tmp_path / "custom-data"
    assert settings.artifacts_dir == tmp_path / "custom-artifacts"
    assert settings.log_level == "DEBUG"


def test_env_file_in_the_working_directory_is_read(tmp_path: Path) -> None:
    write_env_file(
        tmp_path,
        f"TWITCH_CLIENT_ID={FAKE_CLIENT_ID}\n"
        f"TWITCH_CLIENT_SECRET={FAKE_CLIENT_SECRET}\n"
        "AUTOCLIPPER_DATA_DIR=from-env-file\n"
        "AUTOCLIPPER_LOG_LEVEL=WARNING\n",
    )

    settings = get_settings()

    assert settings.require_twitch_credentials().client_id.get_secret_value() == FAKE_CLIENT_ID
    assert settings.data_dir == tmp_path / "from-env-file"
    assert settings.log_level == "WARNING"


def test_environment_variable_beats_env_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_env_file(tmp_path, "AUTOCLIPPER_LOG_LEVEL=WARNING\n")
    monkeypatch.setenv("AUTOCLIPPER_LOG_LEVEL", "ERROR")

    assert get_settings().log_level == "ERROR"


def test_env_file_in_a_parent_directory_is_not_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_env_file(tmp_path, "AUTOCLIPPER_LOG_LEVEL=ERROR\n")
    subdirectory = tmp_path / "sub"
    subdirectory.mkdir()
    monkeypatch.chdir(subdirectory)

    assert get_settings().log_level == "INFO"


def test_env_file_with_a_byte_order_mark_is_read(tmp_path: Path) -> None:
    # Windows PowerShell 5.1 writes UTF-8 files with a BOM; it must not hide the first variable.
    write_env_file(tmp_path, "AUTOCLIPPER_LOG_LEVEL=DEBUG\n", encoding="utf-8-sig")

    assert get_settings().log_level == "DEBUG"


def test_variable_names_in_env_file_are_case_insensitive(tmp_path: Path) -> None:
    write_env_file(tmp_path, "autoclipper_log_level=ERROR\n")

    assert get_settings().log_level == "ERROR"


@pytest.mark.parametrize("name", ["DATA_DIR", "LOG_LEVEL", "CHAT_HASH_SALT", "data_dir"])
def test_variables_without_the_full_name_are_ignored(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    # Generic names like LOG_LEVEL belong to other programs and must not leak into settings.
    monkeypatch.setenv(name, "ERROR")

    settings = get_settings()

    assert settings.log_level == "INFO"
    assert settings.chat_hash_salt is None
    assert settings.data_dir.name == "data"


def test_settings_are_immutable() -> None:
    settings = get_settings()

    with pytest.raises(ValidationError):
        settings.log_level = "DEBUG"


def test_loading_creates_no_directories(tmp_path: Path) -> None:
    get_settings()

    assert list(tmp_path.iterdir()) == []


# --- Empty values ----------------------------------------------------------------------------


def test_empty_values_in_env_file_mean_unset(tmp_path: Path) -> None:
    write_env_file(
        tmp_path,
        'TWITCH_CLIENT_ID=\nTWITCH_CLIENT_SECRET=""\n'
        "AUTOCLIPPER_CHAT_HASH_SALT=''\nAUTOCLIPPER_DATA_DIR=\nAUTOCLIPPER_LOG_LEVEL=\n",
    )

    settings = get_settings()

    assert settings.twitch_client_id is None
    assert settings.twitch_client_secret is None
    assert settings.chat_hash_salt is None
    assert settings.data_dir == tmp_path / "data"
    assert settings.log_level == "INFO"


@pytest.mark.parametrize("value", ["", "   ", "\t"])
def test_empty_or_blank_secrets_in_environment_mean_unset(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    for name in ("TWITCH_CLIENT_ID", "TWITCH_CLIENT_SECRET", "AUTOCLIPPER_CHAT_HASH_SALT"):
        monkeypatch.setenv(name, value)

    settings = get_settings()

    assert settings.twitch_client_id is None
    assert settings.twitch_client_secret is None
    assert settings.chat_hash_salt is None


def test_secret_values_are_kept_verbatim(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTOCLIPPER_CHAT_HASH_SALT", " padded salt ")

    assert get_settings().require_chat_hash_salt().get_secret_value() == " padded salt "


def test_blank_directory_is_rejected(tmp_path: Path) -> None:
    write_env_file(tmp_path, 'AUTOCLIPPER_DATA_DIR="   "\n')

    with pytest.raises(ConfigError, match=r"AUTOCLIPPER_DATA_DIR: must be a directory path"):
        get_settings()


# --- Paths -----------------------------------------------------------------------------------


def test_relative_directories_resolve_against_the_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTOCLIPPER_DATA_DIR", "./nested/data")
    monkeypatch.setenv("AUTOCLIPPER_ARTIFACTS_DIR", "../shared-artifacts")

    settings = get_settings()

    assert settings.data_dir.is_absolute()
    assert settings.data_dir == tmp_path / "nested" / "data"
    assert settings.artifacts_dir == tmp_path / ".." / "shared-artifacts"


def test_directories_stay_fixed_when_the_working_directory_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = get_settings()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    assert get_settings() is settings
    assert settings.data_dir == tmp_path / "data"


def test_path_with_spaces_is_kept(tmp_path: Path) -> None:
    write_env_file(tmp_path, "AUTOCLIPPER_DATA_DIR=my clips/raw data\n")

    assert get_settings().data_dir == tmp_path / "my clips" / "raw data"


def test_home_directory_is_expanded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))  # POSIX
    monkeypatch.setenv("USERPROFILE", str(home))  # Windows
    monkeypatch.setenv("AUTOCLIPPER_DATA_DIR", "~/autoclipper-data")

    assert get_settings().data_dir == home / "autoclipper-data"


@windows_only
@pytest.mark.parametrize(
    ("line", "expected"),
    [
        pytest.param(
            f"AUTOCLIPPER_DATA_DIR=C:{BACKSLASH}data{BACKSLASH}autoclipper",
            Path("C:/data/autoclipper"),
            id="backslashes-unquoted",
        ),
        pytest.param(
            f"AUTOCLIPPER_DATA_DIR='D:{BACKSLASH}My Data{BACKSLASH}new clips'",
            Path("D:/My Data/new clips"),
            id="backslashes-and-spaces-single-quoted",
        ),
        pytest.param(
            "AUTOCLIPPER_DATA_DIR=C:/Users/someone/AutoClipper Data",
            Path("C:/Users/someone/AutoClipper Data"),
            id="forward-slashes-with-spaces",
        ),
        pytest.param(
            f'AUTOCLIPPER_DATA_DIR="E:{BACKSLASH}{BACKSLASH}clips"',
            Path("E:/clips"),
            id="escaped-backslash-double-quoted",
        ),
        pytest.param(
            f"AUTOCLIPPER_DATA_DIR={BACKSLASH * 2}server{BACKSLASH}share{BACKSLASH}data",
            Path("//server/share/data"),
            id="unc-share",
        ),
        pytest.param(
            "AUTOCLIPPER_DATA_DIR=//server/share/data",
            Path("//server/share/data"),
            id="unc-share-forward-slashes",
        ),
        pytest.param(
            f'AUTOCLIPPER_DATA_DIR="{BACKSLASH * 4}server{BACKSLASH * 2}share"',
            Path("//server/share"),
            id="unc-share-escaped-double-quoted",
        ),
    ],
)
def test_windows_paths_in_env_file_are_kept_exactly(
    tmp_path: Path, line: str, expected: Path
) -> None:
    write_env_file(tmp_path, line + "\n")

    data_dir = get_settings().data_dir

    assert data_dir.is_absolute()
    assert data_dir == expected


@windows_only
def test_windows_path_from_environment_is_kept_exactly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTOCLIPPER_ARTIFACTS_DIR", f"C:{BACKSLASH}data{BACKSLASH}artifacts")

    assert str(get_settings().artifacts_dir) == f"C:{BACKSLASH}data{BACKSLASH}artifacts"


@windows_only
@pytest.mark.parametrize(
    "value",
    [
        pytest.param(
            f"'{BACKSLASH * 2}synthetic-host{BACKSLASH}pool-share{BACKSLASH}data'",
            id="unc-share-single-quoted",
        ),
        pytest.param(
            f'"{BACKSLASH * 2}synthetic-host{BACKSLASH}pool-share{BACKSLASH}data"',
            id="unc-share-double-quoted",
        ),
        pytest.param(f"{BACKSLASH}pool-share{BACKSLASH}data", id="leading-backslash"),
        pytest.param("/pool-share/data", id="leading-slash"),
    ],
)
def test_windows_path_without_a_drive_letter_is_rejected(tmp_path: Path, value: str) -> None:
    # Regression test: inside quotes python-dotenv decodes "\\" to "\", so a quoted network path
    # became "\synthetic-host\pool-share\data" and was silently placed on the current drive.
    write_env_file(tmp_path, f"AUTOCLIPPER_DATA_DIR={value}\n")

    with pytest.raises(ConfigError, match="no drive letter") as excinfo:
        get_settings()

    assert "AUTOCLIPPER_DATA_DIR" in str(excinfo.value)
    assert "pool-share" not in str(excinfo.value)


@pytest.mark.parametrize("escape", ["n", "t", "r", "a", "b", "f", "v"])
def test_double_quoted_backslash_escape_in_a_path_is_rejected(tmp_path: Path, escape: str) -> None:
    # python-dotenv decodes "\n" and friends inside double quotes, so "C:\new" would silently
    # become "C:" + newline + "ew". The error explains how to quote the path instead.
    write_env_file(tmp_path, f'AUTOCLIPPER_DATA_DIR="C:{BACKSLASH}{escape}ew data"\n')

    with pytest.raises(ConfigError, match="control character") as excinfo:
        get_settings()

    assert "without quotes" in str(excinfo.value)
    assert "AUTOCLIPPER_DATA_DIR" in str(excinfo.value)


@pytest.mark.parametrize(
    "line",
    [
        pytest.param('AUTOCLIPPER_DATA_DIR="D:/expected-data', id="unterminated-double-quote"),
        pytest.param("AUTOCLIPPER_DATA_DIR='D:/expected-data", id="unterminated-single-quote"),
        pytest.param(f'TWITCH_CLIENT_SECRET="{FAKE_CLIENT_SECRET}', id="secret-line"),
    ],
)
def test_unparseable_env_file_line_is_rejected_without_showing_it(
    tmp_path: Path, line: str
) -> None:
    # Regression test: python-dotenv only logs a warning and drops such a line, so the command
    # used to run with the default instead of the value the user wrote.
    write_env_file(tmp_path, f"AUTOCLIPPER_LOG_LEVEL=DEBUG\n{line}\n")

    with pytest.raises(ConfigError, match=r"can't be parsed \(line 2\)") as excinfo:
        get_settings()

    assert "expected-data" not in str(excinfo.value)
    assert FAKE_CLIENT_SECRET not in str(excinfo.value)


def test_env_file_that_is_not_utf8_is_rejected(tmp_path: Path) -> None:
    # Regression test: this used to escape as a bare UnicodeDecodeError.
    write_env_file(tmp_path, "AUTOCLIPPER_DATA_DIR=D:/café\n", encoding="cp1252")

    with pytest.raises(ConfigError, match="not valid UTF-8"):
        get_settings()


def test_unreadable_env_file_is_a_config_error(tmp_path: Path) -> None:
    (tmp_path / ".env").mkdir()

    with pytest.raises(ConfigError, match="can't read"):
        get_settings()


# --- Log level -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [("DEBUG", "DEBUG"), ("info", "INFO"), (" Warning ", "WARNING"), ("critical", "CRITICAL")],
)
def test_log_level_accepts_level_names_in_any_case(
    monkeypatch: pytest.MonkeyPatch, value: str, expected: str
) -> None:
    monkeypatch.setenv("AUTOCLIPPER_LOG_LEVEL", value)

    assert get_settings().log_level == expected


@pytest.mark.parametrize("value", ["VERBOSE", "WARN", "10", "NOTSET"])
def test_invalid_log_level_is_rejected_with_the_allowed_values(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("AUTOCLIPPER_LOG_LEVEL", value)

    with pytest.raises(ConfigError) as excinfo:
        get_settings()

    message = str(excinfo.value)
    assert "AUTOCLIPPER_LOG_LEVEL" in message
    assert "'DEBUG', 'INFO', 'WARNING', 'ERROR' or 'CRITICAL'" in message
    assert ".env.example" in message


# --- Errors never show values -----------------------------------------------------------------


def test_unknown_variable_in_env_file_is_rejected_without_showing_its_value(
    tmp_path: Path,
) -> None:
    write_env_file(tmp_path, f"TWITCH_CLIENT_SECRETT={FAKE_CLIENT_SECRET}\n")

    with pytest.raises(ConfigError) as excinfo:
        get_settings()

    message = str(excinfo.value)
    assert "TWITCH_CLIENT_SECRETT: not an AutoClipper setting" in message
    assert FAKE_CLIENT_SECRET not in message
    # The pydantic error, which repeats the input value, is not chained into the traceback.
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__suppress_context__


def test_invalid_settings_error_lists_every_problem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_env_file(tmp_path, "AUTOCLIPPER_DATA_DIR='  '\nUNRELATED_SETTING=1\n")
    monkeypatch.setenv("AUTOCLIPPER_LOG_LEVEL", "LOUD")

    with pytest.raises(ConfigError) as excinfo:
        get_settings()

    message = str(excinfo.value)
    for name in ("AUTOCLIPPER_DATA_DIR", "AUTOCLIPPER_LOG_LEVEL", "UNRELATED_SETTING"):
        assert f"  {name}: " in message


# --- Secrets never print ---------------------------------------------------------------------


def test_secrets_are_hidden_in_every_rendering(monkeypatch: pytest.MonkeyPatch) -> None:
    set_all_secrets(monkeypatch)
    settings = get_settings()
    credentials = settings.require_twitch_credentials()

    renderings = [
        repr(settings),
        str(settings),
        f"{settings}",
        repr(settings.model_dump()),
        str(settings.model_dump(mode="json")),
        settings.model_dump_json(),
        repr(credentials),
        str(credentials),
        repr(settings.require_chat_hash_salt()),
    ]

    for text in renderings:
        for secret in (FAKE_CLIENT_ID, FAKE_CLIENT_SECRET, FAKE_SALT):
            assert secret not in text
    assert "**********" in repr(settings)


# --- Required settings -----------------------------------------------------------------------


def test_missing_twitch_credentials_raise_an_actionable_error() -> None:
    # Loading succeeded without them (credentials are optional until a Twitch call needs them).
    with pytest.raises(MissingSettingError) as excinfo:
        get_settings().require_twitch_credentials()

    error = excinfo.value
    assert isinstance(error, ConfigError)
    assert error.names == ("TWITCH_CLIENT_ID", "TWITCH_CLIENT_SECRET")
    message = str(error)
    assert "TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET are not set" in message
    assert ".env.example" in message
    assert "docs/development/setup.md" in message


@pytest.mark.parametrize(
    ("present", "missing"),
    [
        ("TWITCH_CLIENT_ID", "TWITCH_CLIENT_SECRET"),
        ("TWITCH_CLIENT_SECRET", "TWITCH_CLIENT_ID"),
    ],
)
def test_error_names_only_the_missing_credential(
    monkeypatch: pytest.MonkeyPatch, present: str, missing: str
) -> None:
    monkeypatch.setenv(present, "fake-value-0000")

    with pytest.raises(MissingSettingError) as excinfo:
        get_settings().require_twitch_credentials()

    assert excinfo.value.names == (missing,)
    assert str(excinfo.value).startswith(f"{missing} is not set")
    assert "fake-value-0000" not in str(excinfo.value)


def test_missing_salt_raises_an_actionable_error(tmp_path: Path) -> None:
    write_env_file(tmp_path, "AUTOCLIPPER_CHAT_HASH_SALT=\n")

    with pytest.raises(MissingSettingError) as excinfo:
        get_settings().require_chat_hash_salt()

    assert excinfo.value.names == ("AUTOCLIPPER_CHAT_HASH_SALT",)
    assert "Chat hash salt" in str(excinfo.value)


# --- Cache -----------------------------------------------------------------------------------


def test_get_settings_returns_the_cached_instance(monkeypatch: pytest.MonkeyPatch) -> None:
    first = get_settings()
    monkeypatch.setenv("AUTOCLIPPER_LOG_LEVEL", "ERROR")

    assert get_settings() is first
    assert get_settings().log_level == "INFO"


def test_cache_clear_makes_get_settings_read_again(monkeypatch: pytest.MonkeyPatch) -> None:
    first = get_settings()
    monkeypatch.setenv("AUTOCLIPPER_LOG_LEVEL", "ERROR")

    get_settings.cache_clear()

    assert get_settings() is not first
    assert get_settings().log_level == "ERROR"


def test_a_failed_load_is_not_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTOCLIPPER_LOG_LEVEL", "LOUD")
    with pytest.raises(ConfigError):
        get_settings()

    monkeypatch.setenv("AUTOCLIPPER_LOG_LEVEL", "DEBUG")

    assert get_settings().log_level == "DEBUG"


# --- Conventions §4 ---------------------------------------------------------------------------

_ENVIRONMENT_ACCESS = {"environ", "environb", "getenv", "getenvb", "putenv", "unsetenv"}


def _environment_access(source: str) -> list[str]:
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Attribute) and node.attr in _ENVIRONMENT_ACCESS:
            found.append(f"line {node.lineno}: .{node.attr}")
        elif isinstance(node, ast.ImportFrom) and node.module in {"os", "dotenv"}:
            found.append(f"line {node.lineno}: from {node.module} import ...")
        elif isinstance(node, ast.Import) and any(a.name == "dotenv" for a in node.names):
            found.append(f"line {node.lineno}: import dotenv")
    return found


def test_only_the_config_module_reads_the_environment() -> None:
    offenders = {
        str(path.relative_to(PACKAGE_DIR)): found
        for path in sorted(PACKAGE_DIR.rglob("*.py"))
        if path != PACKAGE_DIR / "config.py"
        and (found := _environment_access(path.read_text(encoding="utf-8")))
    }

    # docs/development/conventions.md §4: read settings through autoclipper.config.
    assert offenders == {}


def test_environment_access_check_detects_os_environ() -> None:
    assert _environment_access("import os\nx = os.environ['A']\n") == ["line 2: .environ"]
    assert _environment_access("from os import getenv\n") == ["line 1: from os import ..."]
    assert _environment_access("import dotenv\n") == ["line 1: import dotenv"]
