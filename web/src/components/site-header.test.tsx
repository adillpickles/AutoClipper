// @vitest-environment jsdom

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { SiteHeader } from "@/components/site-header";

const state = vi.hoisted(() => ({
  mockMode: true,
  pathname: "/",
  getHealth: vi.fn(),
}));

vi.mock("next/navigation", () => ({
  usePathname: () => state.pathname,
}));

vi.mock("@/lib/api/client", () => ({
  isMockMode: () => state.mockMode,
  getApiClient: () => ({ getHealth: state.getHealth }),
}));

describe("SiteHeader", () => {
  afterEach(() => {
    cleanup();
    state.pathname = "/";
    vi.clearAllMocks();
  });

  it("shows the Mock data badge immediately in browser mock mode", () => {
    state.mockMode = true;
    render(<SiteHeader />);
    expect(screen.getByText("Mock data")).toBeTruthy();
    expect(state.getHealth).not.toHaveBeenCalled();
  });

  it("shows the Mock data badge when the backend reports job_runner mock", async () => {
    state.mockMode = false;
    state.getHealth.mockResolvedValue({
      status: "ok",
      schema_version: "1.1",
      job_runner: "mock",
      captions_available: true,
    });

    render(<SiteHeader />);

    expect(await screen.findByText("Mock data")).toBeTruthy();
  });

  it("hides the Mock data badge when the backend reports job_runner pipeline", async () => {
    state.mockMode = false;
    state.getHealth.mockResolvedValue({
      status: "ok",
      schema_version: "1.1",
      job_runner: "pipeline",
      captions_available: false,
    });

    render(<SiteHeader />);
    await vi.waitFor(() => expect(state.getHealth).toHaveBeenCalledTimes(1));

    expect(screen.queryByText("Mock data")).toBeNull();
  });

  it("links to Single VOD and Channels and marks the active route", () => {
    state.pathname = "/channels";
    render(<SiteHeader />);

    const channelLinks = screen.getAllByRole("link", { name: "Channels" });
    expect(channelLinks.length).toBeGreaterThan(0);
    expect(channelLinks[0].getAttribute("href")).toBe("/channels");
    expect(channelLinks[0].getAttribute("aria-current")).toBe("page");
  });
});
