// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { PreflightBanner } from "@/components/preflight-banner";
import type { PreflightReport } from "@/lib/api/types";

const state = vi.hoisted(() => ({
  mockMode: false,
  getPreflight: vi.fn<() => Promise<PreflightReport>>(),
}));

vi.mock("@/lib/api/client", () => ({
  isMockMode: () => state.mockMode,
  getApiClient: () => ({ getPreflight: state.getPreflight }),
}));

describe("PreflightBanner", () => {
  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
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
});
