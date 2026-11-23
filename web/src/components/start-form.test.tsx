// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { StartForm } from "@/components/start-form";

const state = vi.hoisted(() => ({
  push: vi.fn(),
  createJob: vi.fn(),
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
    getApiClient: () => ({ createJob: state.createJob }),
  };
});

describe("StartForm", () => {
  afterEach(() => {
    cleanup();
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
    expect(state.createJob).not.toHaveBeenCalled();
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

    fireEvent.click(screen.getByRole("button", { name: "Windows path" }));
    const path = screen.getByLabelText(
      /VOD file on this computer/,
    ) as HTMLInputElement;
    expect(path.value).toBe("C:\\Videos\\vod.mp4");
  });
});
