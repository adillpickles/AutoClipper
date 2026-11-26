"""API tests for ``GET`` and ``POST /api/v1/tools/setup`` (`autoclipper.api.tools`)."""

import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from autoclipper.api.app import API_PREFIX, create_app
from autoclipper.api.service import MockService, SampleMedia
from autoclipper.api.tools import ToolSetupManager
from autoclipper.config import Settings
from autoclipper.schemas import ToolSetupStatus
from autoclipper.tools.installer import (
    ChecksumMismatchError,
    InstallProgress,
    ProgressCallback,
    ToolDownloadError,
    ToolInstallOutcome,
)
from autoclipper.tools.manifest import ToolGroup, UnsupportedPlatformError


def _dummy_service(tmp_path: Path) -> MockService:
    dummy_file = tmp_path / "sample.mp4"
    dummy_file.write_bytes(b"fake")
    samples = SampleMedia(dummy_file)
    return MockService(samples=samples, output_dir=tmp_path / "artifacts", step_s=0.0)


def _wait_for_terminal(client: TestClient, timeout_s: float = 5.0) -> dict[str, object]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        body = client.get(f"{API_PREFIX}/tools/setup").json()
        if body["state"] in {"done", "failed"}:
            return body
        time.sleep(0.02)
    raise AssertionError("tool setup did not finish in time")


def test_get_tools_setup_starts_idle_with_no_store(tmp_path: Path) -> None:
    manager = ToolSetupManager()
    with TestClient(create_app(_dummy_service(tmp_path), tool_setup=manager)) as client:
        response = client.get(f"{API_PREFIX}/tools/setup")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    parsed = ToolSetupStatus.model_validate(response.json())
    assert parsed.state == "idle"
    assert parsed.items == ()
    assert parsed.error is None


def test_post_tools_setup_streams_progress_deduplicates_and_completes(tmp_path: Path) -> None:
    gate = threading.Event()
    calls: list[tuple[ToolGroup, bool]] = []

    def fake_install(
        tool: ToolGroup,
        tools_dir: Path,
        force: bool,
        on_progress: ProgressCallback | None,
    ) -> ToolInstallOutcome:
        calls.append((tool, force))
        if on_progress is not None:
            on_progress(
                InstallProgress(
                    tool=tool,
                    label="FFmpeg & FFprobe" if tool == "ffmpeg" else "TwitchDownloaderCLI",
                    stage="downloading",
                    message=f"Downloading {tool}...",
                    bytes_downloaded=50,
                    bytes_total=100,
                )
            )
        gate.wait(timeout=3.0)
        return ToolInstallOutcome(
            tool=tool,
            label="FFmpeg & FFprobe" if tool == "ffmpeg" else "TwitchDownloaderCLI",
            version="9.0.2" if tool == "ffmpeg" else "1.56.5",
            status="installed",
            source_name="Fake",
            license="GPL-3.0" if tool == "ffmpeg" else "MIT",
            binaries=(tools_dir / "bin" / "secret-local-path-binary",),
        )

    manager = ToolSetupManager(
        missing_tools_fn=lambda _settings: ("ffmpeg", "twitch_downloader"),
        install_one_fn=fake_install,
    )
    with TestClient(create_app(_dummy_service(tmp_path), tool_setup=manager)) as client:
        first = client.post(f"{API_PREFIX}/tools/setup")
        assert first.status_code == 202
        assert first.headers["cache-control"] == "no-store"
        assert first.json()["state"] == "running"

        # Second POST while running returns 200 without starting a duplicate run.
        second = client.post(f"{API_PREFIX}/tools/setup")
        assert second.status_code == 200
        assert second.json()["state"] == "running"

        gate.set()
        done = _wait_for_terminal(client)

    assert done["state"] == "done"
    assert done["error"] is None
    assert calls == [("ffmpeg", False), ("twitch_downloader", False)]
    # Local filesystem paths never leak over HTTP.
    assert "secret-local-path-binary" not in str(done)
    assert str(tmp_path) not in str(done)


def test_post_tools_setup_returns_200_done_when_nothing_missing(tmp_path: Path) -> None:
    called = False

    def should_not_run(
        tool: ToolGroup,
        tools_dir: Path,
        force: bool,
        on_progress: ProgressCallback | None,
    ) -> ToolInstallOutcome:
        nonlocal called
        called = True
        raise AssertionError("should not be called")

    manager = ToolSetupManager(
        missing_tools_fn=lambda _settings: (),
        install_one_fn=should_not_run,
    )
    with TestClient(create_app(_dummy_service(tmp_path), tool_setup=manager)) as client:
        resp = client.post(f"{API_PREFIX}/tools/setup")

    assert resp.status_code == 200
    assert resp.json()["state"] == "done"
    assert resp.json()["items"] == []
    assert not called


def test_partial_platform_failure_still_installs_supported_tool_and_hides_paths(
    tmp_path: Path,
) -> None:
    def partial_install(
        tool: ToolGroup,
        tools_dir: Path,
        force: bool,
        on_progress: ProgressCallback | None,
    ) -> ToolInstallOutcome:
        del force, on_progress
        if tool == "ffmpeg":
            return ToolInstallOutcome(
                tool="ffmpeg",
                label="FFmpeg & FFprobe",
                version="9.0.2",
                status="installed",
                source_name="BtbN",
                license="GPL-3.0",
                binaries=(tools_dir / "bin" / "ffmpeg.exe",),
            )
        raise UnsupportedPlatformError(
            os_name="windows",
            arch_name="arm64",
            tool="twitch_downloader",
            supported=("windows-x86_64",),
        )

    manager = ToolSetupManager(
        missing_tools_fn=lambda _settings: ("ffmpeg", "twitch_downloader"),
        install_one_fn=partial_install,
    )
    with TestClient(create_app(_dummy_service(tmp_path), tool_setup=manager)) as client:
        resp = client.post(f"{API_PREFIX}/tools/setup")
        assert resp.status_code == 202
        final = _wait_for_terminal(client)

    assert final["state"] == "failed"
    assert final["error"] is not None
    assert final["error"]["code"] == "unsupported_platform"  # type: ignore[index]
    assert "Installed 1 of 2 tool(s)" in final["error"]["message"]  # type: ignore[index]
    by_tool = {item["tool"]: item for item in final["items"]}  # type: ignore[union-attr]
    assert by_tool["ffmpeg"]["stage"] == "done"
    assert by_tool["twitch_downloader"]["stage"] == "failed"
    assert str(tmp_path) not in str(final)


@pytest.mark.parametrize(
    ("exc", "expected_code"),
    [
        (
            ChecksumMismatchError("ffmpeg.zip", expected="a" * 64, actual="b" * 64),
            "checksum_mismatch",
        ),
        (ToolDownloadError("network down /secret/local/path"), "download_failed"),
    ],
)
def test_installer_errors_map_to_sanitized_api_error_codes(
    tmp_path: Path, exc: Exception, expected_code: str
) -> None:
    def fail_install(
        tool: ToolGroup,
        tools_dir: Path,
        force: bool,
        on_progress: ProgressCallback | None,
    ) -> ToolInstallOutcome:
        del tool, tools_dir, force, on_progress
        raise exc

    manager = ToolSetupManager(
        settings_provider=lambda: Settings(),
        install_one_fn=fail_install,
    )
    with TestClient(create_app(_dummy_service(tmp_path), tool_setup=manager)) as client:
        resp = client.post(
            f"{API_PREFIX}/tools/setup",
            json={"tools": ["ffmpeg"], "force": True},
        )
        assert resp.status_code == 202
        final = _wait_for_terminal(client)

    assert final["state"] == "failed"
    assert final["error"]["code"] == expected_code  # type: ignore[index]
    assert "/secret/local/path" not in str(final)


def test_post_tools_setup_rejects_unknown_tool_name(tmp_path: Path) -> None:
    manager = ToolSetupManager()
    with TestClient(create_app(_dummy_service(tmp_path), tool_setup=manager)) as client:
        resp = client.post(f"{API_PREFIX}/tools/setup", json={"tools": ["unknown_tool"]})

    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_request"


def test_post_tools_setup_rejects_unapproved_browser_origin(tmp_path: Path) -> None:
    started = False

    def should_not_run(
        tool: ToolGroup,
        tools_dir: Path,
        force: bool,
        on_progress: ProgressCallback | None,
    ) -> ToolInstallOutcome:
        nonlocal started
        started = True
        raise AssertionError("should not run for disallowed origin")

    manager = ToolSetupManager(
        missing_tools_fn=lambda _s: ("ffmpeg",),
        install_one_fn=should_not_run,
    )
    app = create_app(
        _dummy_service(tmp_path),
        cors_origins=("http://localhost:3000",),
        tool_setup=manager,
    )
    with TestClient(app) as client:
        blocked = client.post(
            f"{API_PREFIX}/tools/setup",
            headers={"Origin": "https://evil.example"},
        )
        assert blocked.status_code == 403
        assert blocked.json()["error"]["code"] == "origin_not_allowed"
        assert not started


def test_close_cancels_remaining_tools_and_aborts_progress(tmp_path: Path) -> None:
    entered = threading.Event()
    resume = threading.Event()
    calls: list[ToolGroup] = []

    def gated_install(
        tool: ToolGroup,
        tools_dir: Path,
        force: bool,
        on_progress: ProgressCallback | None,
    ) -> ToolInstallOutcome:
        del force
        calls.append(tool)
        if tool == "ffmpeg":
            entered.set()
            resume.wait(timeout=3.0)
            if on_progress is not None:
                on_progress(
                    InstallProgress(
                        tool="ffmpeg",
                        label="FFmpeg & FFprobe",
                        stage="downloading",
                        message="Downloading...",
                        bytes_downloaded=10,
                        bytes_total=100,
                    )
                )
        return ToolInstallOutcome(
            tool=tool,
            label=tool,
            version="9.0.2",
            status="installed",
            source_name="Fake",
            license="GPL-3.0",
            binaries=(tools_dir / "bin" / "ffmpeg",),
        )

    manager = ToolSetupManager(
        missing_tools_fn=lambda _s: ("ffmpeg", "twitch_downloader"),
        install_one_fn=gated_install,
    )
    manager.start()
    assert entered.wait(timeout=3.0)
    close_thread = threading.Thread(target=manager.close, kwargs={"timeout_s": 3.0})
    close_thread.start()
    time.sleep(0.05)
    resume.set()
    close_thread.join(timeout=3.0)

    assert calls == ["ffmpeg"]
    final = manager.status()
    assert final.state == "failed"
    assert final.error is not None
    assert final.error.code == "setup_cancelled"
