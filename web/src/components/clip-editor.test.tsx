// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ClipEditor } from "@/components/clip-editor";
import type { AnalysisResult, CandidateMedia, Export } from "@/lib/api/types";

const params = vi.hoisted(() => ({ jobId: "job_1", candidateId: "c001" }));

const api = vi.hoisted(() => ({
  getResult: vi.fn(),
  getCandidateMedia: vi.fn(),
  createExport: vi.fn(),
  getExport: vi.fn(),
  resolveUrl: vi.fn((url: string) => url),
}));

vi.mock("next/navigation", () => ({
  useParams: () => params,
}));

vi.mock("@/lib/api/client", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  POLL_INTERVAL_MS: 30,
  getApiClient: () => api,
}));

const RESULT: AnalysisResult = {
  schema_version: "1.0",
  generated_at: "2026-11-20T12:00:00Z",
  vod: {
    video_id: "9000000001",
    title: "Synthetic stream 1",
    broadcaster_id: "9100000001",
    broadcaster_display_name: "user_001",
    duration_s: 360,
    created_at: "2026-11-12T18:00:00Z",
  },
  scorer: { kind: "mock", name: "mock", run_id: null },
  n_viewer_clips_total: 4,
  warnings: [],
  candidates: [
    {
      candidate_id: "c001",
      rank: 1,
      score: 3.42,
      start_s: 118,
      end_s: 148,
      source: "both",
      n_viewer_clips: 3,
      top_signals: ["chat_msg_rate_z", "audio_rms_max_z"],
    },
  ],
};

const AVAILABLE_MEDIA: CandidateMedia = {
  job_id: "job_1",
  candidate_id: "c001",
  video_id: "9000000001",
  preview: {
    status: "available",
    kind: "video_file",
    url: "/mock-media/preview.mp4",
    media_start_s: 88,
    media_end_s: 178,
    reason: null,
    message: null,
  },
  export: {
    status: "available",
    reason: null,
    message: null,
    min_start_s: 88,
    max_end_s: 178,
    max_duration_s: 90,
    layouts: ["center_crop", "configured_region"],
  },
};

function makeExport(
  state: Export["state"],
  overrides: Partial<Export> = {},
): Export {
  return {
    export_id: "exp_1",
    job_id: "job_1",
    candidate_id: "c001",
    video_id: "9000000001",
    start_s: 113,
    end_s: 149,
    layout: "configured_region",
    captions: false,
    state,
    progress:
      state === "running"
        ? {
            stage: "rendering",
            stage_index: 3,
            stage_count: 3,
            stage_fraction: 0.5,
            message: "Rendering clip",
          }
        : null,
    error: null,
    download_url: state === "done" ? "/mock-media/clip.mp4" : null,
    created_at: "2026-11-20T12:01:00Z",
    started_at: state === "queued" ? null : "2026-11-20T12:01:01Z",
    updated_at: "2026-11-20T12:01:02Z",
    finished_at:
      state === "done" || state === "failed" ? "2026-11-20T12:01:05Z" : null,
    ...overrides,
  };
}

describe("ClipEditor", () => {
  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it("nudges start/end, resets, toggles layout and captions, and re-renders to done", async () => {
    api.getResult.mockResolvedValue(RESULT);
    api.getCandidateMedia.mockResolvedValue(AVAILABLE_MEDIA);
    api.createExport.mockResolvedValue({
      export: makeExport("queued"),
      created: true,
    });
    api.getExport
      .mockResolvedValueOnce(makeExport("running"))
      .mockResolvedValue(makeExport("done"));

    render(<ClipEditor />);

    const startInput = (await screen.findByLabelText(
      "Start (seconds)",
    )) as HTMLInputElement;
    const endInput = screen.getByLabelText("End (seconds)") as HTMLInputElement;
    expect(startInput.value).toBe("118");
    expect(endInput.value).toBe("148");

    fireEvent.click(screen.getByLabelText("Move start 5 seconds earlier"));
    fireEvent.click(screen.getByLabelText("Move end 1 second later"));
    expect(startInput.value).toBe("113");
    expect(endInput.value).toBe("149");

    // Reset restores the candidate's original bounds, then trim again for re-render.
    fireEvent.click(screen.getByRole("button", { name: "Reset" }));
    expect(startInput.value).toBe("118");
    expect(endInput.value).toBe("148");

    fireEvent.click(screen.getByLabelText("Move start 5 seconds earlier"));
    fireEvent.click(screen.getByLabelText("Move end 1 second later"));
    fireEvent.click(screen.getByLabelText("Configured region (9:16)"));
    fireEvent.click(screen.getByLabelText("Burn in captions"));

    fireEvent.click(screen.getByRole("button", { name: "Re-render clip" }));

    const download = await screen.findByRole("link", { name: "Download clip" });
    expect(download.getAttribute("href")).toBe("/mock-media/clip.mp4");
    expect(api.createExport).toHaveBeenCalledWith({
      job_id: "job_1",
      candidate_id: "c001",
      start_s: 113,
      end_s: 149,
      layout: "configured_region",
      captions: false,
    });
  });

  it("shows the video unavailable state with contract reasons and disables export", async () => {
    for (const [reason, label] of [
      ["source_unavailable", "Source unavailable"],
      ["local_file_missing", "Local file missing"],
      ["fetch_failed", "Download failed"],
    ] as const) {
      api.getResult.mockResolvedValue(RESULT);
      api.getCandidateMedia.mockResolvedValue({
        job_id: "job_1",
        candidate_id: "c001",
        video_id: "9000000007",
        preview: {
          status: "unavailable",
          kind: null,
          url: null,
          media_start_s: null,
          media_end_s: null,
          reason,
          message: `Preview unavailable for ${reason}.`,
        },
        export: {
          status: "unavailable",
          reason,
          message: `Export unavailable for ${reason}.`,
          min_start_s: null,
          max_end_s: null,
          max_duration_s: null,
          layouts: [],
        },
      });

      render(<ClipEditor />);

      expect(await screen.findByText("Video unavailable")).toBeTruthy();
      expect(screen.getByText(label)).toBeTruthy();
      expect(screen.getByText(`Reason: ${reason}`)).toBeTruthy();
      expect(
        screen.getByText(`Preview unavailable for ${reason}.`),
      ).toBeTruthy();
      expect(
        screen.getByText(`Export unavailable for ${reason}.`),
      ).toBeTruthy();
      const renderButton = screen.getByRole("button", {
        name: "Re-render clip",
      }) as HTMLButtonElement;
      expect(renderButton.disabled).toBe(true);

      cleanup();
      vi.clearAllMocks();
    }
  });

  it("polls candidate media while preparing until it becomes available", async () => {
    api.getResult.mockResolvedValue(RESULT);
    api.getCandidateMedia
      .mockResolvedValueOnce({
        ...AVAILABLE_MEDIA,
        preview: {
          status: "preparing",
          kind: null,
          url: null,
          media_start_s: null,
          media_end_s: null,
          reason: null,
          message: "Fetching the video for this moment…",
        },
      })
      .mockResolvedValue(AVAILABLE_MEDIA);

    render(<ClipEditor />);

    expect(
      await screen.findByText("Fetching the video for this moment…"),
    ).toBeTruthy();
    expect(await screen.findByLabelText("Preview of clip 1")).toBeTruthy();
    expect(api.getCandidateMedia.mock.calls.length).toBeGreaterThanOrEqual(2);
  });
});
