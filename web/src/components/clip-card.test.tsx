// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ClipCard } from "@/components/clip-card";
import type { Candidate } from "@/lib/api/types";

const RICH_CANDIDATE: Candidate = {
  candidate_id: "c001",
  rank: 1,
  score: 3.42,
  start_s: 118,
  end_s: 148,
  source: "both",
  n_viewer_clips: 3,
  top_signals: ["chat_msg_rate_z", "audio_rms_max_z"],
  title: "Loud audio & laughing chat",
  reason:
    "Chat jumped to 6× its usual rate with lots of laugh reactions; loudest moment of the stream; 3 viewer clips",
  signal_details: [
    {
      kind: "chat_rate",
      label: "Chat rate",
      detail: "6× usual stream rate (z=3.4)",
    },
    {
      kind: "audio_loudness",
      label: "Audio loudness",
      detail: "Loudest moment of the stream (z=2.6)",
    },
    {
      kind: "viewer_clips",
      label: "Viewer clips",
      detail: "3 viewer clips overlap this moment",
    },
  ],
};

describe("ClipCard explanations", () => {
  afterEach(() => {
    cleanup();
  });

  it("shows the clip title, one-line reason, and toggles the Why breakdown", () => {
    render(
      <ul>
        <ClipCard
          jobId="job_1"
          candidate={RICH_CANDIDATE}
          scoreFraction={1}
          clip={{ kind: "pending" }}
          videoId="9000000001"
          onRetry={vi.fn()}
        />
      </ul>,
    );

    expect(
      screen.getByRole("heading", {
        level: 3,
        name: "Loud audio & laughing chat",
      }),
    ).toBeTruthy();
    expect(
      screen.getByText(
        "Chat jumped to 6× its usual rate with lots of laugh reactions; loudest moment of the stream; 3 viewer clips",
      ),
    ).toBeTruthy();

    const whyButton = screen.getByRole("button", { name: /Why/i });
    expect(whyButton.getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByText("6× usual stream rate (z=3.4)")).toBeNull();

    fireEvent.click(whyButton);
    expect(whyButton.getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByText("Chat rate")).toBeTruthy();
    expect(screen.getByText("6× usual stream rate (z=3.4)")).toBeTruthy();
    expect(
      screen.getByText("Loudest moment of the stream (z=2.6)"),
    ).toBeTruthy();
    expect(screen.getByText("3 viewer clips overlap this moment")).toBeTruthy();

    fireEvent.click(whyButton);
    expect(whyButton.getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByText("6× usual stream rate (z=3.4)")).toBeNull();
  });

  it("falls back cleanly for legacy 1.0 candidates without explanation fields", () => {
    const legacy: Candidate = {
      candidate_id: "c002",
      rank: 2,
      score: 2.1,
      start_s: 40,
      end_s: 70,
      source: "discovered",
      n_viewer_clips: 0,
      top_signals: ["audio_rms_max_z"],
    };

    render(
      <ul>
        <ClipCard
          jobId="job_1"
          candidate={legacy}
          scoreFraction={0.6}
          clip={{ kind: "pending" }}
          videoId="9000000001"
          onRetry={vi.fn()}
        />
      </ul>,
    );

    expect(
      screen.getByRole("heading", { level: 3, name: "Loud audio spike" }),
    ).toBeTruthy();
    expect(screen.getByText("The streamer got loud")).toBeTruthy();
    const whyButton = screen.getByRole("button", { name: /Why/i });
    fireEvent.click(whyButton);
    expect(screen.getByText("Audio loudness")).toBeTruthy();
  });

  it("omits the Why toggle when there are no signals or viewer clips", () => {
    const quiet: Candidate = {
      candidate_id: "c003",
      rank: 3,
      score: 0.2,
      start_s: 10,
      end_s: 35,
      source: "discovered",
      n_viewer_clips: 0,
      top_signals: [],
    };

    render(
      <ul>
        <ClipCard
          jobId="job_1"
          candidate={quiet}
          scoreFraction={0.1}
          clip={{ kind: "pending" }}
          videoId="9000000001"
          onRetry={vi.fn()}
        />
      </ul>,
    );

    expect(screen.getByText("Steady activity throughout")).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Why/i })).toBeNull();
  });

  it("uses the clean server export filename on the download link", () => {
    render(
      <ul>
        <ClipCard
          jobId="job_1"
          candidate={RICH_CANDIDATE}
          scoreFraction={1}
          clip={{
            kind: "export",
            url: "/api/v1/exports/exp_1/file",
            export: {
              export_id: "exp_1",
              job_id: "job_1",
              candidate_id: "c001",
              video_id: "9000000001",
              start_s: 118,
              end_s: 148,
              layout: "center_crop",
              captions: true,
              state: "done",
              progress: null,
              error: null,
              download_url: "/api/v1/exports/exp_1/file",
              filename:
                "user_001_2026-10-12_Loud-audio-laughing-chat_00-01-58.mp4",
              created_at: "2026-10-19T15:05:00Z",
              started_at: "2026-10-19T15:05:01Z",
              updated_at: "2026-10-19T15:05:48Z",
              finished_at: "2026-10-19T15:05:48Z",
            },
          }}
          videoId="9000000001"
          onRetry={vi.fn()}
        />
      </ul>,
    );

    const link = screen.getByRole("link", { name: /Download/i });
    expect(link.getAttribute("download")).toBe(
      "user_001_2026-10-12_Loud-audio-laughing-chat_00-01-58.mp4",
    );
  });
});
