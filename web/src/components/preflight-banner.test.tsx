// @vitest-environment jsdom

import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { PreflightBanner } from "@/components/preflight-banner";
import type {
  PreflightReport,
  ToolSetupRequest,
  ToolSetupStatus,
} from "@/lib/api/types";

const IDLE_SETUP: ToolSetupStatus = {
  state: "idle",
  items: [],
  error: null,
  started_at: null,
  updated_at: "2026-11-26T00:00:00Z",
  finished_at: null,
};

const state = vi.hoisted(() => ({
  mockMode: false,
  getPreflight: vi.fn<() => Promise<PreflightReport>>(),
  getToolSetup: vi.fn<() => Promise<ToolSetupStatus>>(),
  startToolSetup: vi.fn<
    (request?: ToolSetupRequest) => Promise<{
      setup: ToolSetupStatus;
      started: boolean;
    }>
  >(),
}));

vi.mock("@/lib/api/client", () => ({
  isMockMode: () => state.mockMode,
  getApiClient: () => ({
    getPreflight: state.getPreflight,
    getToolSetup: state.getToolSetup,
    startToolSetup: state.startToolSetup,
  }),
}));

describe("PreflightBanner", () => {
  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
    vi.useRealTimers();
    state.mockMode = false;
  });

  it("stays hidden and skips the request in browser mock mode", () => {
    state.mockMode = true;
    const { container } = render(<PreflightBanner />);

    expect(container.firstChild).toBeNull();
    expect(state.getPreflight).not.toHaveBeenCalled();
  });

  it("mounts an empty polite region before warnings arrive, then fills it", async () => {
    let resolveReport: (report: PreflightReport) => void = () => {};
    state.getPreflight.mockReturnValue(
      new Promise((resolve) => {
        resolveReport = resolve;
      }),
    );

    render(<PreflightBanner />);

    const region = screen.getByRole("status");
    expect(region.getAttribute("aria-live")).toBe("polite");
    expect(region.textContent).toBe("");

    resolveReport({
      status: "warn",
      checks: [
        {
          name: "captions",
          label: "Captions extra",
          status: "warn",
          message: "faster-whisper is not installed.",
          fix: "uv sync --extra captions",
        },
      ],
    });

    expect(
      await screen.findByText("Optional setup items missing"),
    ).toBeTruthy();
    expect(screen.getByRole("status")).toBe(region);
  });

  it("stays visually empty when all preflight checks pass", async () => {
    state.getPreflight.mockResolvedValue({
      status: "ok",
      checks: [
        {
          name: "ffmpeg",
          label: "FFmpeg & FFprobe",
          status: "ok",
          message: "FFmpeg and FFprobe are available on PATH.",
          fix: null,
        },
      ],
    });

    render(<PreflightBanner />);
    await vi.waitFor(() => expect(state.getPreflight).toHaveBeenCalledTimes(1));

    const region = screen.getByRole("status");
    expect(region.textContent).toBe("");
    expect(region.className).toContain("sr-only");
    expect(screen.queryByText("FFmpeg & FFprobe")).toBeNull();
  });

  it("stays visually empty when the preflight request fails", async () => {
    state.getPreflight.mockRejectedValue(new Error("offline"));

    render(<PreflightBanner />);
    await vi.waitFor(() => expect(state.getPreflight).toHaveBeenCalledTimes(1));

    expect(screen.getByRole("status").textContent).toBe("");
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("renders a polite status banner for warnings and hides passing checks", async () => {
    state.getPreflight.mockResolvedValue({
      status: "warn",
      checks: [
        {
          name: "ffmpeg",
          label: "FFmpeg & FFprobe",
          status: "ok",
          message: "FFmpeg and FFprobe are available on PATH.",
          fix: null,
        },
        {
          name: "captions",
          label: "Captions extra",
          status: "warn",
          message: "faster-whisper is not installed.",
          fix: "Install the captions extra with: uv sync --extra captions",
        },
      ],
    });

    render(<PreflightBanner />);

    const banner = await screen.findByRole("status");
    expect(banner.getAttribute("aria-live")).toBe("polite");
    expect(screen.getByText("Optional setup items missing")).toBeTruthy();
    expect(screen.getByText("Captions extra")).toBeTruthy();
    expect(screen.getByText("faster-whisper is not installed.")).toBeTruthy();
    expect(
      screen.getByText(
        "Fix: Install the captions extra with: uv sync --extra captions",
      ),
    ).toBeTruthy();
    expect(screen.queryByText("FFmpeg & FFprobe")).toBeNull();
  });

  it("renders an alert banner when any check fails and lists failures first", async () => {
    state.getPreflight.mockResolvedValue({
      status: "fail",
      checks: [
        {
          name: "captions",
          label: "Captions extra",
          status: "warn",
          message: "faster-whisper is not installed.",
          fix: "uv sync --extra captions",
        },
        {
          name: "twitch_credentials",
          label: "Twitch API credentials",
          status: "fail",
          message: "TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET are not set.",
          fix: "Set TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET in .env.",
        },
      ],
    });

    render(<PreflightBanner />);

    const banner = await screen.findByRole("alert");
    expect(banner.hasAttribute("aria-live")).toBe(false);
    expect(screen.getByText("Setup issues need attention")).toBeTruthy();
    const items = screen.getAllByRole("listitem");
    expect(items).toHaveLength(2);
    expect(items[0].textContent).toContain("Twitch API credentials");
    expect(items[1].textContent).toContain("Captions extra");
  });

  it("can be dismissed via the dismiss button and via Escape", async () => {
    const warningReport: PreflightReport = {
      status: "warn",
      checks: [
        {
          name: "twitch_downloader",
          label: "TwitchDownloaderCLI",
          status: "warn",
          message: "TwitchDownloaderCLI is not on PATH.",
          fix: "Add TwitchDownloaderCLI 1.56.5 to PATH.",
        },
      ],
    };
    state.getPreflight.mockResolvedValue(warningReport);

    render(
      <>
        <PreflightBanner />
        <input name="vod" />
      </>,
    );
    const button = await screen.findByRole("button", {
      name: "Dismiss setup notice",
    });
    expect(button.getAttribute("type")).toBe("button");

    fireEvent.click(button);
    expect(screen.queryByRole("status")).toBeNull();
    expect(document.activeElement).toBe(screen.getByRole("textbox"));

    cleanup();
    render(
      <>
        <PreflightBanner />
        <input name="vod" />
      </>,
    );
    const secondButton = await screen.findByRole("button", {
      name: "Dismiss setup notice",
    });
    fireEvent.keyDown(secondButton, { key: "Escape" });
    expect(screen.queryByRole("status")).toBeNull();
    expect(document.activeElement).toBe(screen.getByRole("textbox"));
  });

  it("offers one-click tool download, polls progress, and refreshes preflight when done", async () => {
    state.getToolSetup.mockResolvedValueOnce(IDLE_SETUP);
    state.getPreflight
      .mockResolvedValueOnce({
        status: "fail",
        checks: [
          {
            name: "ffmpeg",
            label: "FFmpeg & FFprobe",
            status: "fail",
            message: "ffmpeg and ffprobe are not found.",
            fix: "Run `uv run autoclipper setup-tools --tool ffmpeg`.",
            source: null,
          },
          {
            name: "twitch_downloader",
            label: "TwitchDownloaderCLI",
            status: "warn",
            message: "TwitchDownloaderCLI is not found.",
            fix: "Run `uv run autoclipper setup-tools --tool twitch-downloader`.",
            source: null,
          },
        ],
      })
      .mockResolvedValueOnce({
        status: "ok",
        checks: [
          {
            name: "ffmpeg",
            label: "FFmpeg & FFprobe",
            status: "ok",
            message: "FFmpeg and FFprobe are available (managed tools folder).",
            fix: null,
            source: "managed",
          },
          {
            name: "twitch_downloader",
            label: "TwitchDownloaderCLI",
            status: "ok",
            message: "TwitchDownloaderCLI is available (managed tools folder).",
            fix: null,
            source: "managed",
          },
        ],
      });

    const runningStatus: ToolSetupStatus = {
      state: "running",
      items: [
        {
          tool: "ffmpeg",
          label: "FFmpeg & FFprobe",
          stage: "downloading",
          message: "Downloading ffmpeg-9.0.2-essentials_build.zip...",
          bytes_downloaded: 50 * 1024 * 1024,
          bytes_total: 100 * 1024 * 1024,
          fraction: 0.5,
          error: null,
        },
      ],
      error: null,
      started_at: "2026-11-26T00:00:00Z",
      updated_at: "2026-11-26T00:00:01Z",
      finished_at: null,
    };
    const doneStatus: ToolSetupStatus = {
      state: "done",
      items: [
        {
          tool: "ffmpeg",
          label: "FFmpeg & FFprobe",
          stage: "done",
          message: "Installed FFmpeg & FFprobe 9.0.2.",
          bytes_downloaded: 100 * 1024 * 1024,
          bytes_total: 100 * 1024 * 1024,
          fraction: 1.0,
          error: null,
        },
      ],
      error: null,
      started_at: "2026-11-26T00:00:00Z",
      updated_at: "2026-11-26T00:00:02Z",
      finished_at: "2026-11-26T00:00:02Z",
    };

    state.startToolSetup.mockResolvedValue({
      setup: runningStatus,
      started: true,
    });
    state.getToolSetup.mockResolvedValue(doneStatus);

    render(<PreflightBanner />);

    const downloadBtn = await screen.findByRole("button", {
      name: "Download missing tools",
    });
    fireEvent.click(downloadBtn);

    expect(state.startToolSetup).toHaveBeenCalledWith({
      tools: ["ffmpeg", "twitch_downloader"],
    });
    expect(
      await screen.findByRole("progressbar", {
        name: "FFmpeg & FFprobe setup progress",
      }),
    ).toBeTruthy();
    expect(screen.getByText(/50%/)).toBeTruthy();

    expect(
      await screen.findByText("Managed tools installed", undefined, {
        timeout: 3000,
      }),
    ).toBeTruthy();
    expect(state.getPreflight).toHaveBeenCalledTimes(2);
  });

  it("shows an actionable error and retry button when download fails", async () => {
    state.getToolSetup.mockResolvedValueOnce(IDLE_SETUP);
    state.getPreflight.mockResolvedValue({
      status: "fail",
      checks: [
        {
          name: "ffmpeg",
          label: "FFmpeg & FFprobe",
          status: "fail",
          message: "ffmpeg is not found.",
          fix: "Run `uv run autoclipper setup-tools --tool ffmpeg`.",
          source: null,
        },
      ],
    });
    state.startToolSetup.mockRejectedValueOnce(
      new Error("Unsupported platform for pinned builds."),
    );

    render(<PreflightBanner />);

    const downloadBtn = await screen.findByRole("button", {
      name: "Download FFmpeg & FFprobe",
    });
    fireEvent.click(downloadBtn);

    expect(
      await screen.findByText("Unsupported platform for pinned builds."),
    ).toBeTruthy();
    expect(screen.getByRole("button", { name: "Retry download" })).toBeTruthy();
    expect(
      screen.getByText(
        "Fix: Run `uv run autoclipper setup-tools --tool ffmpeg`.",
      ),
    ).toBeTruthy();
  });

  it("hides the automatic download button when only an explicit config path is broken", async () => {
    state.getToolSetup.mockResolvedValueOnce(IDLE_SETUP);
    state.getPreflight.mockResolvedValue({
      status: "fail",
      checks: [
        {
          name: "ffmpeg",
          label: "FFmpeg & FFprobe",
          status: "fail",
          message: "AUTOCLIPPER_FFMPEG_PATH does not exist.",
          fix: "Update or unset AUTOCLIPPER_FFMPEG_PATH.",
          source: "config",
        },
      ],
    });

    render(<PreflightBanner />);

    await screen.findByText("Setup issues need attention");
    expect(screen.queryByRole("button", { name: /Download/i })).toBeNull();
  });

  it("keeps deferred preflight refresh alive after polling stops", async () => {
    state.getToolSetup.mockResolvedValueOnce(IDLE_SETUP);

    let resolveRefreshedPreflight: (report: PreflightReport) => void = () => {};
    state.getPreflight
      .mockResolvedValueOnce({
        status: "fail",
        checks: [
          {
            name: "ffmpeg",
            label: "FFmpeg & FFprobe",
            status: "fail",
            message: "ffmpeg is not found.",
            fix: "Run `uv run autoclipper setup-tools --tool ffmpeg`.",
            source: null,
          },
        ],
      })
      .mockReturnValueOnce(
        new Promise((resolve) => {
          resolveRefreshedPreflight = resolve;
        }),
      );

    state.startToolSetup.mockResolvedValue({
      setup: {
        state: "running",
        items: [
          {
            tool: "ffmpeg",
            label: "FFmpeg & FFprobe",
            stage: "downloading",
            message: "Downloading...",
            bytes_downloaded: 10,
            bytes_total: 100,
            fraction: 0.1,
            error: null,
          },
        ],
        error: null,
        started_at: "2026-11-26T00:00:00Z",
        updated_at: "2026-11-26T00:00:01Z",
        finished_at: null,
      },
      started: true,
    });
    state.getToolSetup.mockResolvedValue({
      state: "done",
      items: [
        {
          tool: "ffmpeg",
          label: "FFmpeg & FFprobe",
          stage: "done",
          message: "Installed FFmpeg & FFprobe 9.0.2.",
          bytes_downloaded: 100,
          bytes_total: 100,
          fraction: 1.0,
          error: null,
        },
      ],
      error: null,
      started_at: "2026-11-26T00:00:00Z",
      updated_at: "2026-11-26T00:00:02Z",
      finished_at: "2026-11-26T00:00:02Z",
    });

    render(<PreflightBanner />);
    const btn = await screen.findByRole("button", {
      name: "Download FFmpeg & FFprobe",
    });
    fireEvent.click(btn);

    // Wait until polling transitions to done and kicks off the deferred preflight refresh.
    await vi.waitFor(
      () => expect(state.getPreflight).toHaveBeenCalledTimes(2),
      {
        timeout: 3000,
      },
    );

    // Resolving the deferred preflight AFTER the polling effect cleaned up must still apply.
    resolveRefreshedPreflight({
      status: "ok",
      checks: [
        {
          name: "ffmpeg",
          label: "FFmpeg & FFprobe",
          status: "ok",
          message: "FFmpeg and FFprobe are available (managed tools folder).",
          fix: null,
          source: "managed",
        },
      ],
    });

    expect(await screen.findByText("Managed tools installed")).toBeTruthy();
  });

  it("ignores a late initial setup GET once the user starts a download", async () => {
    let resolveInitialSetup: (status: ToolSetupStatus) => void = () => {};
    state.getToolSetup.mockReturnValueOnce(
      new Promise((resolve) => {
        resolveInitialSetup = resolve;
      }),
    );
    state.getPreflight.mockResolvedValue({
      status: "fail",
      checks: [
        {
          name: "ffmpeg",
          label: "FFmpeg & FFprobe",
          status: "fail",
          message: "ffmpeg is not found.",
          fix: "Run `uv run autoclipper setup-tools --tool ffmpeg`.",
          source: null,
        },
      ],
    });
    state.startToolSetup.mockResolvedValue({
      setup: {
        state: "running",
        items: [
          {
            tool: "ffmpeg",
            label: "FFmpeg & FFprobe",
            stage: "downloading",
            message: "Downloading...",
            bytes_downloaded: 25,
            bytes_total: 100,
            fraction: 0.25,
            error: null,
          },
        ],
        error: null,
        started_at: "2026-11-26T00:00:05Z",
        updated_at: "2026-11-26T00:00:06Z",
        finished_at: null,
      },
      started: true,
    });

    render(<PreflightBanner />);
    const btn = await screen.findByRole("button", {
      name: "Download FFmpeg & FFprobe",
    });
    fireEvent.click(btn);
    expect(await screen.findByText(/25%/)).toBeTruthy();

    // Late stale response from an earlier failed run must not overwrite active progress.
    resolveInitialSetup({
      state: "failed",
      items: [],
      error: { code: "download_failed", message: "Old error", details: null },
      started_at: "2026-11-26T00:00:00Z",
      updated_at: "2026-11-26T00:00:01Z",
      finished_at: "2026-11-26T00:00:01Z",
    });

    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(screen.queryByText("Old error")).toBeNull();
    expect(screen.getByText(/25%/)).toBeTruthy();
  });

  it("backs off and shows a retry action when polling loses the backend", async () => {
    vi.useFakeTimers();
    state.getToolSetup.mockResolvedValueOnce(IDLE_SETUP);
    state.getPreflight.mockResolvedValue({
      status: "fail",
      checks: [
        {
          name: "ffmpeg",
          label: "FFmpeg & FFprobe",
          status: "fail",
          message: "ffmpeg is not found.",
          fix: "Run `uv run autoclipper setup-tools --tool ffmpeg`.",
          source: null,
        },
      ],
    });
    state.startToolSetup.mockResolvedValue({
      setup: {
        state: "running",
        items: [
          {
            tool: "ffmpeg",
            label: "FFmpeg & FFprobe",
            stage: "downloading",
            message: "Downloading...",
            bytes_downloaded: 10,
            bytes_total: 100,
            fraction: 0.1,
            error: null,
          },
        ],
        error: null,
        started_at: "2026-11-26T00:00:00Z",
        updated_at: "2026-11-26T00:00:00Z",
        finished_at: null,
      },
      started: true,
    });
    state.getToolSetup.mockRejectedValue(new Error("network down"));

    render(<PreflightBanner />);
    await act(async () => {
      await Promise.resolve();
    });
    const btn = screen.getByRole("button", {
      name: "Download FFmpeg & FFprobe",
    });
    await act(async () => {
      fireEvent.click(btn);
      await Promise.resolve();
    });

    // Advance through 4 failed polls with exponential backoff (1s + 1s + 2s + 4s).
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000);
    });

    expect(
      screen.getByText(/Lost connection to the AutoClipper server/i),
    ).toBeTruthy();
    expect(
      screen.getByRole("button", { name: "Check setup status" }),
    ).toBeTruthy();
  });
});
