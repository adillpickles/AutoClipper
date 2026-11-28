// @vitest-environment jsdom

// The dialog's polling must survive the results screen's rerenders (every finished clip
// re-renders the parent, and an inline onClose used to cancel the poll loop), and must stop
// when the dialog is actually closed.

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { HighlightsDialog, sequenceFor } from "@/components/highlights-dialog";
import type { Candidate } from "@/lib/api/types";
import { rememberTrim } from "@/lib/job-state";

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
  beforeEach(() => {
    window.localStorage.clear();
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it("keeps polling through its parent's rerenders and finishes", async () => {
    api.createHighlights.mockResolvedValue({
      highlights: video("queued"),
      created: true,
    });
    api.getHighlights
      .mockResolvedValueOnce(
        video("running", {
          progress: {
            stage: "rendering",
            stage_index: 3,
            stage_count: 3,
            stage_fraction: 0.5,
            message: "Rendering the highlights video",
          },
        }),
      )
      .mockResolvedValue(
        video("done", {
          download_url: "/mock-media/highlights.mp4",
          duration_s: 77,
        }),
      );

    const { rerender } = render(dialog(() => undefined));
    fireEvent.click(
      screen.getByRole("button", { name: "Make highlights video" }),
    );
    await screen.findByText(/Waiting in render queue/);

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
      "9000000001_unknown-date_highlights.mp4",
    );
    expect(download.getAttribute("href")).toBe("/mock-media/highlights.mp4");
  });

  it("uses server filename when provided on finished highlights video", async () => {
    api.createHighlights.mockResolvedValue({
      highlights: video("done", {
        download_url: "/mock-media/highlights.mp4",
        filename: "user_001_2026-10-12_highlights.mp4",
        duration_s: 52,
      }),
      created: true,
    });

    render(dialog(() => undefined));
    fireEvent.click(
      screen.getByRole("button", { name: "Make highlights video" }),
    );

    await screen.findByText(/The highlights video is ready/);
    const download = screen.getByRole("link", { name: "Download" });
    expect(download.getAttribute("download")).toBe(
      "user_001_2026-10-12_highlights.mp4",
    );
  });

  it("stops polling once it is closed", async () => {
    api.createHighlights.mockResolvedValue({
      highlights: video("queued"),
      created: true,
    });
    api.getHighlights.mockResolvedValue(
      video("running", {
        progress: {
          stage: "obtaining_media",
          stage_index: 1,
          stage_count: 3,
          stage_fraction: null,
          message: "Getting the video",
        },
      }),
    );

    const { rerender, unmount } = render(dialog(() => undefined));
    fireEvent.click(
      screen.getByRole("button", { name: "Make highlights video" }),
    );
    await screen.findByText(/Waiting in render queue/);
    rerender(dialog(() => undefined));
    await screen.findByText(/Step 1 of 3: Getting the video/);
    unmount();

    const calls = api.getHighlights.mock.calls.length;
    await new Promise((resolve) => setTimeout(resolve, 150));
    expect(api.getHighlights.mock.calls.length).toBe(calls); // no further polls
  });

  it("initializes moments from shared saved trims and submits them", async () => {
    rememberTrim("job_1", "c001", { start_s: 112.5, end_s: 151 });
    api.createHighlights.mockResolvedValue({
      highlights: video("done", {
        download_url: "/mock-media/highlights.mp4",
        duration_s: 63.5,
      }),
      created: true,
    });

    render(dialog(() => undefined));

    const start1 = document.getElementById("start-c001") as HTMLInputElement;
    const end1 = document.getElementById("end-c001") as HTMLInputElement;
    expect(start1.value).toBe("112.5");
    expect(end1.value).toBe("151");
    expect(screen.getByText("Trimmed")).toBeTruthy();

    fireEvent.click(
      screen.getByRole("button", { name: "Make highlights video" }),
    );
    await screen.findByText(/The highlights video is ready/);
    expect(api.createHighlights).toHaveBeenCalledWith({
      job_id: "job_1",
      segments: [
        { candidate_id: "c001", start_s: 112.5, end_s: 151 },
        { candidate_id: "c002", start_s: 251.5, end_s: 276.5 },
      ],
      order: "chronological",
      captions: false,
    });
  });

  it("distinguishes queued from active running stages and shows progress bar when measured", async () => {
    let resolveNextPoll: ((value: unknown) => void) | null = null;
    api.createHighlights.mockResolvedValue({
      highlights: video("queued"),
      created: true,
    });
    api.getHighlights.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveNextPoll = resolve;
        }),
    );

    render(dialog(() => undefined));
    fireEvent.click(
      screen.getByRole("button", { name: "Make highlights video" }),
    );

    // Queued state is distinct from active rendering.
    expect(await screen.findByText("Queued")).toBeTruthy();
    expect(screen.getByText("Waiting in render queue…")).toBeTruthy();
    expect(screen.queryByRole("progressbar")).toBeNull();

    // Wait for first poll to start, then advance to running (rendering with fraction).
    await vi.waitFor(() => expect(resolveNextPoll).not.toBeNull());
    const finishPoll = resolveNextPoll!;
    resolveNextPoll = null;
    finishPoll(
      video("running", {
        progress: {
          stage: "rendering",
          stage_index: 3,
          stage_count: 3,
          stage_fraction: 0.65,
          message: "Rendering the highlights video",
        },
      }),
    );

    expect(await screen.findByText("Rendering")).toBeTruthy();
    expect(
      screen.getByText("Step 3 of 3: Rendering the highlights video"),
    ).toBeTruthy();
    const bar = screen.getByRole("progressbar");
    expect(bar.getAttribute("aria-valuenow")).toBe("65");
  });

  it("ignores saved trims outside current export bounds and picks up late-arriving initialTrims", () => {
    const { rerender } = render(
      <HighlightsDialog
        jobId="job_1"
        videoId="9000000001"
        vodDurationS={360}
        candidates={candidates}
        initialTrims={{ c001: { start_s: 50, end_s: 148 } }}
        exportBounds={{
          c001: {
            status: "available",
            reason: null,
            message: null,
            min_start_s: 88,
            max_end_s: 178,
            max_duration_s: 90,
            layouts: ["center_crop"],
          },
        }}
        onClose={() => undefined}
      />,
    );

    // Out-of-bounds saved trim (start 50 < min_start_s 88) falls back to candidate bounds (118..148).
    const start1 = document.getElementById("start-c001") as HTMLInputElement;
    expect(start1.value).toBe("118");

    // When a remembered export trim arrives after the dialog opened, untouched rows update.
    rerender(
      <HighlightsDialog
        jobId="job_1"
        videoId="9000000001"
        vodDurationS={360}
        candidates={candidates}
        initialTrims={{ c001: { start_s: 112, end_s: 148 } }}
        exportBounds={{
          c001: {
            status: "available",
            reason: null,
            message: null,
            min_start_s: 88,
            max_end_s: 178,
            max_duration_s: 90,
            layouts: ["center_crop"],
          },
        }}
        onClose={() => undefined}
      />,
    );
    expect(start1.value).toBe("112");
  });
});

describe("sequenceFor", () => {
  const trims = {
    c001: { checked: true, start_s: 118, end_s: 148 },
    c002: { checked: true, start_s: 40, end_s: 62 },
    c003: { checked: false, start_s: 300, end_s: 320 },
  };
  const three: Candidate[] = [
    { ...candidates[0], candidate_id: "c001", rank: 1 },
    { ...candidates[0], candidate_id: "c002", rank: 2 },
    { ...candidates[0], candidate_id: "c003", rank: 3 },
  ];

  it("plays ticked moments in stream order or best first", () => {
    expect(
      sequenceFor(three, trims, "chronological").map((s) => s.candidate_id),
    ).toEqual(["c002", "c001"]);
    expect(sequenceFor(three, trims, "rank").map((s) => s.rank)).toEqual([
      1, 2,
    ]);
  });

  it("never reports a negative length for a half-typed trim", () => {
    const sequence = sequenceFor(
      three,
      { ...trims, c001: { checked: true, start_s: 150, end_s: 148 } },
      "rank",
    );
    expect(sequence[0].duration_s).toBe(0);
  });

  it("shows the play order and keeps the primary action first", () => {
    render(
      <HighlightsDialog
        jobId="job_seq"
        videoId="9000000001"
        vodDurationS={360}
        candidates={candidates}
        onClose={() => undefined}
      />,
    );
    const order = screen.getByRole("list", { name: "Play order" });
    expect(order.children.length).toBe(candidates.length);
    const dialog = screen.getByRole("dialog");
    const firstAction = dialog.querySelector(
      'button:not([aria-label="Close"])',
    );
    expect(firstAction?.textContent).toBe("Make highlights video");
    fireEvent.click(screen.getByLabelText("Best first"));
    expect(order.firstElementChild?.textContent).toContain("#1");
  });
});
