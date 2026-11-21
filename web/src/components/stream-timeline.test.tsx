// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { StreamTimeline } from "@/components/stream-timeline";
import type { Candidate } from "@/lib/api/types";

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
});
