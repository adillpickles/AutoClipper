// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { ClipExport } from "@/components/clip-card";
import { StreamTimeline } from "@/components/stream-timeline";
import type { Candidate, Export } from "@/lib/api/types";

const OVERLAPPING_CANDIDATES: Candidate[] = [
  {
    candidate_id: "c001",
    rank: 1,
    score: 3.4,
    start_s: 118,
    end_s: 148,
    source: "both",
    n_viewer_clips: 3,
    top_signals: ["chat_msg_rate_z", "audio_rms_max_z"],
  },
  {
    candidate_id: "c002",
    rank: 2,
    score: 2.9,
    start_s: 130,
    end_s: 165,
    source: "discovered",
    n_viewer_clips: 0,
    top_signals: ["audio_rms_max_z"],
  },
  {
    candidate_id: "c003",
    rank: 3,
    score: 1.5,
    start_s: 260,
    end_s: 290,
    source: "viewer_clips",
    n_viewer_clips: 1,
    top_signals: [],
  },
];

function readyClip(candidate: Candidate): ClipExport {
  const exp: Export = {
    export_id: `exp_${candidate.candidate_id}`,
    job_id: "job_1",
    candidate_id: candidate.candidate_id,
    video_id: "9000000001",
    start_s: candidate.start_s,
    end_s: candidate.end_s,
    layout: "center_crop",
    captions: true,
    state: "done",
    progress: null,
    error: null,
    download_url: `/mock-media/${candidate.candidate_id}.mp4`,
    created_at: "2026-11-20T12:00:00Z",
    started_at: "2026-11-20T12:00:01Z",
    updated_at: "2026-11-20T12:00:02Z",
    finished_at: "2026-11-20T12:00:02Z",
  };
  return { kind: "export", export: exp, url: exp.download_url };
}

describe("StreamTimeline", () => {
  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it("renders overlapping markers in separate lanes and opens the clicked clip card", () => {
    const onSelect = vi.fn();
    const { rerender } = render(
      <StreamTimeline
        jobId="job_1"
        videoId="9000000001"
        durationS={360}
        candidates={OVERLAPPING_CANDIDATES}
        selectedId="c001"
        onSelect={onSelect}
        clips={{}}
        onRetry={vi.fn()}
      />,
    );

    const marker1 = screen.getByRole("button", { name: /^Rank #1/ });
    const marker2 = screen.getByRole("button", { name: /^Rank #2/ });
    const marker3 = screen.getByRole("button", { name: /^Rank #3/ });

    // Overlapping c001 and c002 must live on different rows; disjoint c003 reuses row 0.
    expect(marker1.getAttribute("data-lane")).toBe("0");
    expect(marker2.getAttribute("data-lane")).toBe("1");
    expect(marker3.getAttribute("data-lane")).toBe("0");
    expect(marker1.getAttribute("aria-pressed")).toBe("true");
    expect(
      screen.getByText(/Overlapping moments are arranged into 2 rows/),
    ).toBeTruthy();

    // Clicking marker #2 notifies the parent; rerendering with c002 opens its clip card.
    fireEvent.click(marker2);
    expect(onSelect).toHaveBeenCalledWith(OVERLAPPING_CANDIDATES[1]);

    rerender(
      <StreamTimeline
        jobId="job_1"
        videoId="9000000001"
        durationS={360}
        candidates={OVERLAPPING_CANDIDATES}
        selectedId="c002"
        onSelect={onSelect}
        clips={{}}
        onRetry={vi.fn()}
      />,
    );

    expect(marker2.getAttribute("aria-pressed")).toBe("true");
    expect(
      screen.getByRole("link", { name: /Edit clip 2/ }).getAttribute("href"),
    ).toBe("/jobs/job_1/results/c002");
  });

  it("renders a true-time anchor inside an enlarged hit area for short clips and labels candidate activity", () => {
    const sixHours = 21600;
    const lateClip: Candidate = {
      candidate_id: "c_late",
      rank: 1,
      score: 4.1,
      start_s: 21570,
      end_s: 21600,
      source: "both",
      n_viewer_clips: 2,
      top_signals: ["chat_msg_rate_z"],
    };

    render(
      <StreamTimeline
        jobId="job_6h"
        videoId="9000000001"
        durationS={sixHours}
        candidates={[lateClip]}
        selectedId="c_late"
        onSelect={vi.fn()}
        clips={{}}
        onRetry={vi.fn()}
      />,
    );

    // Activity strip is explicitly labeled as candidate activity.
    expect(screen.getByText("Candidate activity")).toBeTruthy();
    expect(
      screen.getByRole("img", { name: /Candidate activity/i }),
    ).toBeTruthy();

    // The clickable button uses the enlarged hit area, while its inner anchor sits at exact start_s.
    const button = screen.getByRole("button", { name: /^Rank #1/ });
    expect(button.getAttribute("data-short")).toBe("true");
    expect(button.getAttribute("data-anchor-left-pct")).toBe("99.86");
    expect(button.style.left).toBe("90%");
    expect(button.style.width).toBe("10%");

    const anchor = button.querySelector(
      '[data-timeline-anchor="true"]',
    ) as HTMLElement | null;
    expect(anchor).toBeTruthy();
    // Anchor left inside the [90%, 100%] hit box is exactly ((99.8611 - 90) / 10) * 100 = 98.611...%
    const renderedLeftPct = Number.parseFloat(anchor?.style.left ?? "NaN");
    expect(renderedLeftPct).toBeCloseTo(
      (((21570 / sixHours) * 100 - 90) / 10) * 100,
      3,
    );
  });

  it("resets video error state when switching from a failed preview to another clip", () => {
    const clips: Record<string, ClipExport> = {
      c001: readyClip(OVERLAPPING_CANDIDATES[0]),
      c002: readyClip(OVERLAPPING_CANDIDATES[1]),
    };

    const { rerender } = render(
      <StreamTimeline
        jobId="job_1"
        videoId="9000000001"
        durationS={360}
        candidates={OVERLAPPING_CANDIDATES}
        selectedId="c001"
        onSelect={vi.fn()}
        clips={clips}
        onRetry={vi.fn()}
      />,
    );

    fireEvent.error(screen.getByLabelText(/^Clip 1,/));
    expect(screen.getByText(/ready but can't be played here/i)).toBeTruthy();

    rerender(
      <StreamTimeline
        jobId="job_1"
        videoId="9000000001"
        durationS={360}
        candidates={OVERLAPPING_CANDIDATES}
        selectedId="c002"
        onSelect={vi.fn()}
        clips={clips}
        onRetry={vi.fn()}
      />,
    );

    expect(screen.queryByText(/ready but can't be played here/i)).toBeNull();
    expect(screen.getByLabelText(/^Clip 2,/).getAttribute("src")).toBe(
      "/mock-media/c002.mp4",
    );
  });

  it("moves focus with Arrow keys, Home, and End without auto-selecting, and ignores modified keys", () => {
    const onSelect = vi.fn();
    render(
      <StreamTimeline
        jobId="job_1"
        videoId="9000000001"
        durationS={360}
        candidates={OVERLAPPING_CANDIDATES}
        selectedId="c001"
        onSelect={onSelect}
        clips={{}}
        onRetry={vi.fn()}
      />,
    );

    const marker1 = screen.getByRole("button", { name: /^Rank #1/ });
    const marker2 = screen.getByRole("button", { name: /^Rank #2/ });
    const marker3 = screen.getByRole("button", { name: /^Rank #3/ });

    marker1.focus();
    fireEvent.keyDown(marker1, { key: "ArrowRight" });
    expect(document.activeElement).toBe(marker2);
    expect(onSelect).not.toHaveBeenCalled();

    fireEvent.keyDown(marker2, { key: "End", ctrlKey: true });
    expect(document.activeElement).toBe(marker2);

    fireEvent.keyDown(marker2, { key: "End" });
    expect(document.activeElement).toBe(marker3);

    fireEvent.keyDown(marker3, { key: "Home" });
    expect(document.activeElement).toBe(marker1);
    expect(onSelect).not.toHaveBeenCalled();

    fireEvent.click(marker3);
    expect(onSelect).toHaveBeenCalledWith(OVERLAPPING_CANDIDATES[2]);
  });

  it("renders an empty state when there are no candidates", () => {
    render(
      <StreamTimeline
        jobId="job_1"
        videoId="9000000006"
        durationS={360}
        candidates={[]}
        selectedId={null}
        onSelect={vi.fn()}
        clips={{}}
        onRetry={vi.fn()}
      />,
    );

    expect(
      screen.getByRole("heading", {
        level: 3,
        name: "No moments on this timeline",
      }),
    ).toBeTruthy();
  });
});
