"""Unit and CLI tests for startup preflight checks (`autoclipper.preflight` and `doctor`)."""

import importlib
import json
import shutil
import types
from pathlib import Path

import pytest
from pydantic import SecretStr
from typer.testing import CliRunner

from autoclipper.cli import ExitCode, app
from autoclipper.config import Settings, get_settings
from autoclipper.ingest.chat import PINNED_TOOL_VERSION, TOOL_NAME
from autoclipper.preflight import (
    check_captions,
    check_chat_hash_salt,
    check_ffmpeg,
    check_twitch_credentials,
    check_twitch_downloader,
    format_preflight_summary,
    run_preflight,
)
from autoclipper.schemas import PreflightReport

runner = CliRunner()

FAKE_CLIENT_ID = "fake-client-id-preflight-1111"
FAKE_CLIENT_SECRET = "fake-client-secret-preflight-2222"
FAKE_SALT = "fake-salt-preflight-3333"


def _patch_which(monkeypatch: pytest.MonkeyPatch, available: set[str]) -> None:
    def fake_which(program: str | Path) -> str | None:
        name = str(program)
        if name in available:
            return f"/fake/bin/{name}"
        return None

    monkeypatch.setattr(shutil, "which", fake_which)


def _patch_captions(
    monkeypatch: pytest.MonkeyPatch,
    *,
    available: bool,
    model_loaded: list[bool] | None = None,
) -> None:
    real_import = importlib.import_module

    def fake_import(name: str, package: str | None = None) -> object:
        if name != "faster_whisper":
            return real_import(name, package)
        if not available:
            raise ModuleNotFoundError("No module named 'faster_whisper'")
        module = types.ModuleType("faster_whisper")

        def fail_if_called(*args: object, **kwargs: object) -> object:
            del args, kwargs
            if model_loaded is not None:
                model_loaded.append(True)
            raise AssertionError("WhisperModel must not be instantiated during preflight")

        module.WhisperModel = fail_if_called  # type: ignore[attr-defined]
        return module

    monkeypatch.setattr(importlib, "import_module", fake_import)


def _full_settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("TWITCH_CLIENT_ID", FAKE_CLIENT_ID)
    monkeypatch.setenv("TWITCH_CLIENT_SECRET", FAKE_CLIENT_SECRET)
    monkeypatch.setenv("AUTOCLIPPER_CHAT_HASH_SALT", FAKE_SALT)
    get_settings.cache_clear()
    return get_settings()


# --- FFmpeg & FFprobe --------------------------------------------------------------------------


def test_check_ffmpeg_ok_when_both_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_which(monkeypatch, {"ffmpeg", "ffprobe"})

    result = check_ffmpeg()

    assert result.name == "ffmpeg"
    assert result.status == "ok"
    assert result.source == "path"
    assert "PATH" in result.message
    assert result.fix is None
    assert "/fake/bin" not in result.message


@pytest.mark.parametrize(
    ("available", "expected_part", "absent_part"),
    [
        (set(), "ffmpeg and ffprobe", None),
        ({"ffprobe"}, "ffmpeg is not found", "ffprobe"),
        ({"ffmpeg"}, "ffprobe is not found", "ffmpeg"),
    ],
    ids=["both-missing", "ffmpeg-missing", "ffprobe-missing"],
)
def test_check_ffmpeg_fails_when_missing(
    monkeypatch: pytest.MonkeyPatch,
    available: set[str],
    expected_part: str,
    absent_part: str | None,
) -> None:
    _patch_which(monkeypatch, available)

    result = check_ffmpeg()

    assert result.status == "fail"
    assert result.source is None
    assert expected_part in result.message
    if absent_part is not None:
        assert absent_part not in result.message
    assert result.fix is not None
    assert "winget install Gyan.FFmpeg" in result.fix


def test_check_ffmpeg_reports_managed_source_without_local_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from autoclipper.tools.discovery import managed_binary_path

    tools_dir = tmp_path / "tools"
    for tool in ("ffmpeg", "ffprobe"):
        binary = managed_binary_path(tool, tools_dir)
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_bytes(b"bin")
        binary.chmod(0o755)
    _patch_which(monkeypatch, set())
    monkeypatch.setenv("AUTOCLIPPER_TOOLS_DIR", str(tools_dir))
    get_settings.cache_clear()

    result = check_ffmpeg(get_settings())

    assert result.status == "ok"
    assert result.source == "managed"
    assert "managed tools folder" in result.message
    assert str(tools_dir) not in result.model_dump_json()


def test_check_ffmpeg_reports_mixed_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from autoclipper.tools.discovery import managed_binary_path

    tools_dir = tmp_path / "tools"
    managed_ffmpeg = managed_binary_path("ffmpeg", tools_dir)
    managed_ffmpeg.parent.mkdir(parents=True, exist_ok=True)
    managed_ffmpeg.write_bytes(b"bin")
    managed_ffmpeg.chmod(0o755)
    _patch_which(monkeypatch, {"ffprobe"})
    monkeypatch.setenv("AUTOCLIPPER_TOOLS_DIR", str(tools_dir))
    get_settings.cache_clear()

    result = check_ffmpeg(get_settings())

    assert result.status == "ok"
    assert result.source == "mixed"
    assert "managed tools folder" in result.message
    assert "PATH" in result.message
    assert str(tools_dir) not in result.model_dump_json()
    assert "/fake/bin" not in result.model_dump_json()


def test_check_ffmpeg_reports_invalid_configured_path_without_leaking_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing_path = tmp_path / "secret-dir" / "missing-ffmpeg"
    monkeypatch.setenv("AUTOCLIPPER_FFMPEG_PATH", str(missing_path))
    _patch_which(monkeypatch, {"ffmpeg", "ffprobe"})
    get_settings.cache_clear()

    result = check_ffmpeg(get_settings())

    assert result.status == "fail"
    assert result.source == "config"
    assert "AUTOCLIPPER_FFMPEG_PATH" in result.message
    assert result.fix is not None
    assert "AUTOCLIPPER_FFMPEG_PATH" in result.fix
    assert "secret-dir" not in result.model_dump_json()


# --- TwitchDownloaderCLI -----------------------------------------------------------------------


def test_check_twitch_downloader_ok_when_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_which(monkeypatch, {TOOL_NAME})

    result = check_twitch_downloader()

    assert result.name == "twitch_downloader"
    assert result.status == "ok"
    assert result.source == "path"
    assert "PATH" in result.message
    assert result.fix is None
    assert "/fake/bin" not in result.message


def test_check_twitch_downloader_reports_config_source_without_local_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    custom = tmp_path / "private-bin" / "TwitchDownloaderCLI.exe"
    custom.parent.mkdir(parents=True)
    custom.write_bytes(b"bin")
    custom.chmod(0o755)
    monkeypatch.setenv("AUTOCLIPPER_TWITCH_DOWNLOADER_PATH", str(custom))
    _patch_which(monkeypatch, set())
    get_settings.cache_clear()

    result = check_twitch_downloader(get_settings())

    assert result.status == "ok"
    assert result.source == "config"
    assert "AUTOCLIPPER_TWITCH_DOWNLOADER_PATH" in result.message
    assert "private-bin" not in result.model_dump_json()


def test_check_twitch_downloader_warns_on_invalid_config_path_without_leaking_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = tmp_path / "private-bin" / "missing-td"
    monkeypatch.setenv("AUTOCLIPPER_TWITCH_DOWNLOADER_PATH", str(missing))
    _patch_which(monkeypatch, {TOOL_NAME})
    get_settings.cache_clear()

    result = check_twitch_downloader(get_settings())

    assert result.status == "warn"
    assert result.source == "config"
    assert "AUTOCLIPPER_TWITCH_DOWNLOADER_PATH" in result.message
    assert "private-bin" not in result.model_dump_json()


def test_check_twitch_downloader_warns_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_which(monkeypatch, set())

    result = check_twitch_downloader()

    assert result.status == "warn"
    assert result.source is None
    assert TOOL_NAME in result.message
    assert "--video" in result.message
    assert result.fix is not None
    assert PINNED_TOOL_VERSION in result.fix


# --- Chat hash salt ----------------------------------------------------------------------------


def test_check_chat_hash_salt_ok_and_hides_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _full_settings(monkeypatch)

    result = check_chat_hash_salt(settings)

    assert result.name == "chat_hash_salt"
    assert result.status == "ok"
    assert result.fix is None
    assert FAKE_SALT not in result.model_dump_json()


@pytest.mark.parametrize("job_runner", ["pipeline", "mock"])
def test_check_chat_hash_salt_missing_warns_for_both_runners(job_runner: str) -> None:
    settings = get_settings()

    result = check_chat_hash_salt(
        settings,
        job_runner=job_runner,  # type: ignore[arg-type]
    )

    assert result.status == "warn"
    assert "AUTOCLIPPER_CHAT_HASH_SALT" in result.message
    assert result.fix is not None
    assert "AUTOCLIPPER_CHAT_HASH_SALT" in result.fix
    assert "secrets.token_hex(32)" in result.fix


def test_check_chat_hash_salt_treats_blank_secret_as_missing() -> None:
    settings = Settings.model_construct(chat_hash_salt=SecretStr("   "))

    result = check_chat_hash_salt(settings, job_runner="pipeline")

    assert result.status == "warn"


# --- Captions optional extra -------------------------------------------------------------------


def test_check_captions_ok_without_loading_model(monkeypatch: pytest.MonkeyPatch) -> None:
    loaded: list[bool] = []
    _patch_captions(monkeypatch, available=True, model_loaded=loaded)

    result = check_captions()

    assert result.name == "captions"
    assert result.status == "ok"
    assert result.fix is None
    assert loaded == []


def test_check_captions_warns_when_not_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_captions(monkeypatch, available=False)

    result = check_captions()

    assert result.status == "warn"
    assert "faster-whisper" in result.message
    assert result.fix == "Install the captions extra with: uv sync --extra captions"


def test_check_captions_warns_when_native_import_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    real_import = importlib.import_module

    def broken_import(name: str, package: str | None = None) -> object:
        if name == "faster_whisper":
            raise OSError("libctranslate2 failed to load")
        return real_import(name, package)

    monkeypatch.setattr(importlib, "import_module", broken_import)

    result = check_captions()

    assert result.status == "warn"
    assert result.fix == "Install the captions extra with: uv sync --extra captions"


# --- Twitch Helix credentials ------------------------------------------------------------------


def test_check_twitch_credentials_ok_without_leaking_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _full_settings(monkeypatch)

    result = check_twitch_credentials(settings)

    assert result.name == "twitch_credentials"
    assert result.status == "ok"
    assert result.fix is None
    dumped = result.model_dump_json()
    assert FAKE_CLIENT_ID not in dumped
    assert FAKE_CLIENT_SECRET not in dumped


@pytest.mark.parametrize(
    ("present", "missing", "secret_val"),
    [
        ("TWITCH_CLIENT_ID", "TWITCH_CLIENT_SECRET", FAKE_CLIENT_ID),
        ("TWITCH_CLIENT_SECRET", "TWITCH_CLIENT_ID", FAKE_CLIENT_SECRET),
    ],
)
def test_check_twitch_credentials_names_only_missing_variable(
    monkeypatch: pytest.MonkeyPatch, present: str, missing: str, secret_val: str
) -> None:
    monkeypatch.setenv(present, secret_val)
    get_settings.cache_clear()

    result = check_twitch_credentials(get_settings(), job_runner="pipeline")

    assert result.status == "fail"
    assert missing in result.message
    assert present not in result.message
    assert result.fix is not None
    assert missing in result.fix
    assert secret_val not in result.model_dump_json()


@pytest.mark.parametrize(
    ("job_runner", "expected_status"),
    [("pipeline", "fail"), ("mock", "warn")],
)
def test_check_twitch_credentials_missing_both_depends_on_runner(
    job_runner: str, expected_status: str
) -> None:
    result = check_twitch_credentials(
        get_settings(),
        job_runner=job_runner,  # type: ignore[arg-type]
    )

    assert result.status == expected_status
    assert "TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET" in result.message
    assert result.fix is not None
    assert "TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET" in result.fix


def test_check_twitch_credentials_treats_blank_secret_as_missing() -> None:
    settings = Settings.model_construct(
        twitch_client_id=SecretStr("   "),
        twitch_client_secret=SecretStr(FAKE_CLIENT_SECRET),
    )

    result = check_twitch_credentials(settings, job_runner="pipeline")

    assert result.status == "fail"
    assert "TWITCH_CLIENT_ID" in result.message
    assert FAKE_CLIENT_SECRET not in result.model_dump_json()


# --- Report aggregation and formatting ---------------------------------------------------------


def test_run_preflight_all_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_which(monkeypatch, {"ffmpeg", "ffprobe", TOOL_NAME})
    _patch_captions(monkeypatch, available=True)
    settings = _full_settings(monkeypatch)

    report = run_preflight(settings)

    assert report.status == "ok"
    assert [c.name for c in report.checks] == [
        "ffmpeg",
        "twitch_downloader",
        "chat_hash_salt",
        "captions",
        "twitch_credentials",
    ]
    assert all(c.status == "ok" and c.fix is None for c in report.checks)
    summary = format_preflight_summary(report)
    assert summary.isascii()
    assert "Fix:" not in summary
    for secret in (FAKE_CLIENT_ID, FAKE_CLIENT_SECRET, FAKE_SALT):
        assert secret not in summary
        assert secret not in report.model_dump_json()


def test_run_preflight_warn_only(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_which(monkeypatch, {"ffmpeg", "ffprobe"})
    _patch_captions(monkeypatch, available=False)
    settings = _full_settings(monkeypatch)

    report = run_preflight(settings)

    assert report.status == "warn"
    by_name = {c.name: c.status for c in report.checks}
    assert by_name == {
        "ffmpeg": "ok",
        "twitch_downloader": "warn",
        "chat_hash_salt": "ok",
        "captions": "warn",
        "twitch_credentials": "ok",
    }
    summary = format_preflight_summary(report)
    assert "[WARN]" in summary
    assert "[FAIL]" not in summary
    assert "Fix:" in summary


def test_run_preflight_fail_overrides_warn(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_which(monkeypatch, {"ffmpeg", "ffprobe"})
    _patch_captions(monkeypatch, available=False)

    report = run_preflight(get_settings(), job_runner="pipeline")

    assert report.status == "fail"
    summary = format_preflight_summary(report)
    assert "[FAIL]" in summary
    assert "[WARN]" in summary


# --- CLI: doctor -------------------------------------------------------------------------------


def test_doctor_exits_0_when_all_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_which(monkeypatch, {"ffmpeg", "ffprobe", TOOL_NAME})
    _patch_captions(monkeypatch, available=True)
    _full_settings(monkeypatch)

    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == ExitCode.SUCCESS, result.output
    assert "Preflight checks:" in result.output
    assert "[FAIL]" not in result.output
    assert "[WARN]" not in result.output


def test_doctor_exits_0_on_warnings_only(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_which(monkeypatch, {"ffmpeg", "ffprobe"})
    _patch_captions(monkeypatch, available=False)
    _full_settings(monkeypatch)

    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == ExitCode.SUCCESS, result.output
    assert "[WARN]" in result.output
    assert "uv sync --extra captions" in result.output


def test_doctor_exits_1_when_any_check_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_which(monkeypatch, set())
    _patch_captions(monkeypatch, available=False)

    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == ExitCode.USER_ERROR, result.output
    assert "[FAIL]" in result.output
    assert "TWITCH_CLIENT_ID" in result.output
    assert "AUTOCLIPPER_CHAT_HASH_SALT" in result.output


def test_doctor_mock_mode_exits_0_without_twitch_creds(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_which(monkeypatch, {"ffmpeg", "ffprobe"})
    _patch_captions(monkeypatch, available=False)

    result = runner.invoke(app, ["doctor", "--jobs", "mock"])

    assert result.exit_code == ExitCode.SUCCESS, result.output
    assert "[WARN]" in result.output
    assert "[FAIL]" not in result.output


def test_doctor_json_prints_valid_preflight_report(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_which(monkeypatch, {"ffmpeg", "ffprobe", TOOL_NAME})
    _patch_captions(monkeypatch, available=False)
    _full_settings(monkeypatch)

    result = runner.invoke(app, ["doctor", "--json"])

    assert result.exit_code == ExitCode.SUCCESS, result.output
    parsed = PreflightReport.model_validate(json.loads(result.output))
    assert parsed.status == "warn"
    assert len(parsed.checks) == 5
    assert "/fake/bin" not in result.output


def test_doctor_text_shows_resolved_tool_locations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from autoclipper.tools.discovery import managed_binary_path

    tools_dir = tmp_path / "tools"
    managed_ffmpeg = managed_binary_path("ffmpeg", tools_dir)
    managed_ffmpeg.parent.mkdir(parents=True, exist_ok=True)
    managed_ffmpeg.write_bytes(b"bin")
    managed_ffmpeg.chmod(0o755)
    _patch_which(monkeypatch, {"ffprobe", TOOL_NAME})
    _patch_captions(monkeypatch, available=True)
    monkeypatch.setenv("AUTOCLIPPER_TOOLS_DIR", str(tools_dir))
    _full_settings(monkeypatch)

    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == ExitCode.SUCCESS, result.output
    assert "Tool locations" in result.output
    assert "[managed]" in result.output
    assert str(managed_ffmpeg) in result.output
    assert "[path]" in result.output
