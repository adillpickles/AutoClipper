// @vitest-environment jsdom

// The dialog's polling must survive the results screen's rerenders (every finished clip
// re-renders the parent, and an inline onClose used to cancel the poll loop), and must stop
// when the dialog is actually closed.

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { HighlightsDialog } from "@/components/highlights-dialog";
import type { Candidate } from "@/lib/api/types";

const candidates: Candidate[] = [
  {
    candidate_id: "c001",
    rank: 1,
    score: 3.4,
    start_s: 118,
    end_s: 148,
    source: "both",
    n_viewer_clips: 3,
    top_signals: [],
  },
  {
    candidate_id: "c002",
    rank: 2,
    score: 2.8,
    start_s: 251.5,
    end_s: 276.5,
    source: "discovered",
    n_viewer_clips: 0,
    top_signals: [],
  },
];

const api = vi.hoisted(() => ({
  createHighlights: vi.fn(),
  getHighlights: vi.fn(),
  resolveUrl: vi.fn((url: string) => url),
}));

vi.mock("@/lib/api/client", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  POLL_INTERVAL_MS: 40, // fast polls; the logic under test is the same
  getApiClient: () => api,
}));

const video = (state: string, extra: Record<string, unknown> = {}) =>
  ({
    highlights_id: "hl_1",
    job_id: "job_1",
    video_id: "9000000001",
    segments: [],
    order: "chronological",
    captions: false,
    state,
    progress: null,
    error: null,
    download_url: null,
    duration_s: null,
    created_at: "2026-11-04T12:00:00Z",
    started_at: null,
    updated_at: "2026-11-04T12:00:00Z",
    finished_at: null,
    ...extra,
  }) as const;

const dialog = (onClose: () => void) => (
  <HighlightsDialog
    jobId="job_1"
    videoId="9000000001"
    vodDurationS={360}
    candidates={candidates}
    onClose={onClose}
  />
);

describe("HighlightsDialog", () => {
  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it("keeps polling through its parent's rerenders and finishes", async () => {
    api.createHighlights.mockResolvedValue({
      highlights: video("queued"),
      created: true,
    });
    api.getHighlights.mockResolvedValueOnce(video("running")).mockResolvedValue(
      video("done", {
        download_url: "/mock-media/highlights.mp4",
        duration_s: 77,
      }),
    );

    const { rerender } = render(dialog(() => undefined));
    fireEvent.click(
      screen.getByRole("button", { name: "Make highlights video" }),
    );
    await screen.findByText(/Making the highlights video/);

    // Every finished clip re-renders the results screen with a fresh onClose: the polling
    // must survive these rerenders (#72's review round).
    rerender(dialog(() => undefined));
    rerender(dialog(() => undefined));

    expect(
      await screen.findByText(/The highlights video is ready/),
    ).toBeTruthy();
    expect(api.getHighlights).toHaveBeenCalled();
    const download = screen.getByRole("link", { name: "Download" });
    expect(download.getAttribute("download")).toBe(
      "autoclipper_9000000001_highlights.mp4",
    );
    expect(download.getAttribute("href")).toBe("/mock-media/highlights.mp4");
  });

  it("stops polling once it is closed", async () => {
    api.createHighlights.mockResolvedValue({
      highlights: video("queued"),
      created: true,
    });
    api.getHighlights.mockResolvedValue(video("running"));

    const { rerender, unmount } = render(dialog(() => undefined));
    fireEvent.click(
      screen.getByRole("button", { name: "Make highlights video" }),
    );
    await screen.findByText(/Making the highlights video/);
    rerender(dialog(() => undefined));
    await screen.findByText(/Making the highlights video/);
    unmount();

    const calls = api.getHighlights.mock.calls.length;
    await new Promise((resolve) => setTimeout(resolve, 150));
    expect(api.getHighlights.mock.calls.length).toBe(calls); // no further polls
  });
});
