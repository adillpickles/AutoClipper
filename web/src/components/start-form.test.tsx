// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { StartForm } from "@/components/start-form";

const state = vi.hoisted(() => ({
  push: vi.fn(),
  createJob: vi.fn(),
  mockMode: true,
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: state.push }),
}));

vi.mock("@/lib/api/client", async () => {
  const actual =
    await vi.importActual<typeof import("@/lib/api/client")>(
      "@/lib/api/client",
    );
  return {
    ...actual,
    isMockMode: () => state.mockMode,
    getApiClient: () => ({ createJob: state.createJob }),
  };
});

describe("StartForm", () => {
  afterEach(() => {
    cleanup();
    state.mockMode = true;
    vi.clearAllMocks();
  });

  it("fills the VOD field from an example and names the parsed ID", () => {
    render(<StartForm />);

    fireEvent.click(screen.getByRole("button", { name: "Full link" }));

    const input = screen.getByLabelText("Twitch VOD link") as HTMLInputElement;
    expect(input.value).toBe("https://www.twitch.tv/videos/2345678901");
    expect(screen.getByText(/Ready to analyze VOD 2345678901/)).toBeTruthy();
  });

  it("explains a channel link and does not start a job", () => {
    render(<StartForm />);

    fireEvent.change(screen.getByLabelText("Twitch VOD link"), {
      target: { value: "https://www.twitch.tv/somechannel" },
    });
    fireEvent.click(
      screen.getByRole("button", { name: "Find the best moments" }),
    );

    expect(screen.getByRole("alert").textContent).toMatch(/channel page/);
    expect(
      screen
        .getByRole("link", { name: /Open somechannel in Channels/ })
        .getAttribute("href"),
    ).toBe("/channels?channel=somechannel");
    expect(state.createJob).not.toHaveBeenCalled();
  });

  it("links to the channel browser from the form header", () => {
    render(<StartForm />);
    const link = screen.getByRole("link", { name: /Browse a channel's VODs/ });
    expect(link.getAttribute("href")).toBe("/channels");
  });

  it("rejects a relative file path and accepts an absolute example", () => {
    render(<StartForm />);

    fireEvent.change(screen.getByLabelText("Twitch VOD link"), {
      target: { value: "2345678901" },
    });
    fireEvent.change(screen.getByLabelText(/VOD file on this computer/), {
      target: { value: "vod.mp4" },
    });
    fireEvent.click(
      screen.getByRole("button", { name: "Find the best moments" }),
    );

    expect(screen.getByRole("alert").textContent).toMatch(/full path/);
    expect(state.createJob).not.toHaveBeenCalled();

    const path = screen.getByLabelText(
      /VOD file on this computer/,
    ) as HTMLInputElement;
    expect(path.value).toBe("vod.mp4");
    expect(
      screen.getByText("/Users/you/Videos/vod.mp4", { selector: "code" }),
    ).toBeTruthy();
    expect(
      screen.getByText("C:\\Videos\\vod.mp4", { selector: "code" }),
    ).toBeTruthy();
  });

  it("refuses to analyze an unchanged example when a real backend is configured", () => {
    state.mockMode = false;
    render(<StartForm />);

    fireEvent.click(screen.getByRole("button", { name: "VOD ID" }));
    expect(
      screen.getByText(/Replace 2345678901 with your own VOD/),
    ).toBeTruthy();

    fireEvent.click(
      screen.getByRole("button", { name: "Find the best moments" }),
    );
    expect(screen.getByRole("alert").textContent).toMatch(
      /example of the format/,
    );
    expect(state.createJob).not.toHaveBeenCalled();
  });

  it("allows manually entered VOD IDs matching the sample when a real backend is configured", async () => {
    state.mockMode = false;
    state.createJob.mockResolvedValue({ job: { job_id: "job_123" } });
    render(<StartForm />);

    fireEvent.change(screen.getByLabelText("Twitch VOD link"), {
      target: { value: "2345678901" },
    });
    expect(screen.getByText(/Ready to analyze VOD 2345678901/)).toBeTruthy();

    fireEvent.click(
      screen.getByRole("button", { name: "Find the best moments" }),
    );
    expect(state.createJob).toHaveBeenCalledWith({
      vod: "2345678901",
      video_path: null,
    });
  });
});
