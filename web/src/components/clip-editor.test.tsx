// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ClipEditor } from "@/components/clip-editor";
import type { AnalysisResult, CandidateMedia, Export } from "@/lib/api/types";

const params = vi.hoisted(() => ({ jobId: "job_1", candidateId: "c001" }));

const api = vi.hoisted(() => ({
  getHealth: vi.fn(),
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

const HEALTH_WITH_CAPTIONS = {
  status: "ok" as const,
  schema_version: "1.1",
  job_runner: "mock" as const,
  captions_available: true,
};

describe("ClipEditor", () => {
  beforeEach(() => {
    api.getHealth.mockResolvedValue(HEALTH_WITH_CAPTIONS);
  });

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
    fireEvent.click(await screen.findByLabelText("Configured region (9:16)"));
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
      api.getHealth.mockResolvedValue(HEALTH_WITH_CAPTIONS);
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

  it("clamps typed trim inputs on blur instead of each keystroke and clears stale renders", async () => {
    api.getResult.mockResolvedValue(RESULT);
    api.getCandidateMedia.mockResolvedValue(AVAILABLE_MEDIA);
    api.createExport.mockResolvedValue({
      export: makeExport("done"),
      created: true,
    });

    render(<ClipEditor />);

    const startInput = (await screen.findByLabelText(
      "Start (seconds)",
    )) as HTMLInputElement;
    const endInput = screen.getByLabelText("End (seconds)") as HTMLInputElement;

    // Typing an intermediate out-of-range prefix ("1", "12") must not clamp mid-keystroke.
    fireEvent.change(startInput, { target: { value: "1" } });
    expect(startInput.value).toBe("1");
    fireEvent.change(startInput, { target: { value: "12" } });
    expect(startInput.value).toBe("12");
    fireEvent.change(startInput, { target: { value: "50" } });
    expect(startInput.value).toBe("50");

    // Blurring clamps 50 up to min_start_s (88).
    fireEvent.blur(startInput);
    expect(startInput.value).toBe("88");

    // Typing an end beyond start + max_duration_s (88 + 90 = 178) clamps on blur.
    fireEvent.change(endInput, { target: { value: "250" } });
    expect(endInput.value).toBe("250");
    fireEvent.blur(endInput);
    expect(endInput.value).toBe("178");

    // Render once so the finished clip appears; blurring an untouched field keeps it,
    // while changing the trim clears it.
    fireEvent.click(screen.getByRole("button", { name: "Re-render clip" }));
    expect(
      await screen.findByRole("link", { name: "Download clip" }),
    ).toBeTruthy();

    fireEvent.blur(startInput);
    expect(screen.getByRole("link", { name: "Download clip" })).toBeTruthy();

    fireEvent.click(screen.getByLabelText("Move end 5 seconds earlier"));
    expect(screen.queryByRole("link", { name: "Download clip" })).toBeNull();
  });

  it("discards an in-flight render when the trim changes and preserves exact pause-snapped times", async () => {
    const exactResult: AnalysisResult = {
      ...RESULT,
      candidates: [
        { ...RESULT.candidates[0], start_s: 118.349, end_s: 148.625 },
      ],
    };
    let resolvePoll: ((value: Export) => void) | null = null;
    api.getResult.mockResolvedValue(exactResult);
    api.getCandidateMedia.mockResolvedValue(AVAILABLE_MEDIA);
    api.createExport.mockResolvedValue({
      export: makeExport("running"),
      created: true,
    });
    api.getExport.mockImplementation(
      () =>
        new Promise<Export>((resolve) => {
          resolvePoll = resolve;
        }),
    );

    render(<ClipEditor />);

    await screen.findByLabelText("Center crop (9:16)");
    fireEvent.click(screen.getByRole("button", { name: "Re-render clip" }));

    // Re-rendering without edits must submit the exact pause-snapped timestamps unrounded.
    await vi.waitFor(() =>
      expect(api.createExport).toHaveBeenCalledWith({
        job_id: "job_1",
        candidate_id: "c001",
        start_s: 118.349,
        end_s: 148.625,
        layout: "center_crop",
        captions: true,
      }),
    );

    // Wait until polling is in flight, then change the trim before the old poll resolves.
    await vi.waitFor(() => expect(resolvePoll).not.toBeNull());
    fireEvent.click(screen.getByLabelText("Move start 1 second earlier"));
    resolvePoll!(makeExport("done"));
    await new Promise((r) => setTimeout(r, 40));

    // The obsolete in-flight render must never surface a Download link under the new trim.
    expect(screen.queryByRole("link", { name: "Download clip" })).toBeNull();
  });

  it("disables captions and submits captions: false when health reports captions_available false", async () => {
    api.getHealth.mockResolvedValue({
      status: "ok",
      schema_version: "1.1",
      job_runner: "pipeline",
      captions_available: false,
    });
    api.getResult.mockResolvedValue(RESULT);
    api.getCandidateMedia.mockResolvedValue(AVAILABLE_MEDIA);
    api.createExport.mockResolvedValue({
      export: makeExport("done", { captions: false }),
      created: true,
    });

    render(<ClipEditor />);

    const checkbox = (await screen.findByLabelText(
      "Burn in captions",
    )) as HTMLInputElement;
    await vi.waitFor(() => {
      expect(checkbox.disabled).toBe(true);
      expect(checkbox.checked).toBe(false);
    });
    expect(screen.getByText(/uv sync --extra captions/)).toBeTruthy();

    await screen.findByLabelText("Center crop (9:16)");
    const rerenderButton = screen.getByRole("button", {
      name: "Re-render clip",
    }) as HTMLButtonElement;
    await vi.waitFor(() => expect(rerenderButton.disabled).toBe(false));
    fireEvent.click(rerenderButton);
    await vi.waitFor(() =>
      expect(api.createExport).toHaveBeenCalledWith(
        expect.objectContaining({ captions: false }),
      ),
    );
  });

  it("keeps captions off while /health is still pending so manual exports do not send captions: true prematurely", async () => {
    api.getHealth.mockReturnValue(new Promise(() => undefined));
    api.getResult.mockResolvedValue(RESULT);
    api.getCandidateMedia.mockResolvedValue(AVAILABLE_MEDIA);
    api.createExport.mockResolvedValue({
      export: makeExport("done", { captions: false }),
      created: true,
    });

    render(<ClipEditor />);

    const checkbox = (await screen.findByLabelText(
      "Burn in captions",
    )) as HTMLInputElement;
    expect(checkbox.disabled).toBe(true);
    expect(checkbox.checked).toBe(false);

    await screen.findByLabelText("Center crop (9:16)");
    fireEvent.click(screen.getByRole("button", { name: "Re-render clip" }));
    await vi.waitFor(() =>
      expect(api.createExport).toHaveBeenCalledWith(
        expect.objectContaining({ captions: false }),
      ),
    );
  });

  it("offers an explicit Retry preview button for fetch_failed and preserves edits on retry", async () => {
    api.getResult.mockResolvedValue(RESULT);
    api.getCandidateMedia
      .mockResolvedValueOnce({
        ...AVAILABLE_MEDIA,
        preview: {
          status: "unavailable",
          kind: null,
          url: null,
          media_start_s: null,
          media_end_s: null,
          reason: "fetch_failed",
          message:
            "Preview isn't available: downloading the video failed. Trying again later may work.",
        },
      })
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

    expect(await screen.findByText("Download failed")).toBeTruthy();
    const retryButton = screen.getByRole("button", { name: "Retry preview" });
    expect(retryButton).toBeTruthy();

    // Trim before retrying to verify retry keeps the user's edits intact.
    fireEvent.click(screen.getByLabelText("Move start 5 seconds earlier"));
    const startInput = screen.getByLabelText(
      "Start (seconds)",
    ) as HTMLInputElement;
    expect(startInput.value).toBe("113");

    fireEvent.click(retryButton);

    expect(await screen.findByLabelText("Preview of clip 1")).toBeTruthy();
    expect(startInput.value).toBe("113");
    expect(api.getCandidateMedia).toHaveBeenCalledTimes(3);
  });

  it("does not show Retry preview for permanent source_unavailable and links to local-file analysis", async () => {
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
        reason: "source_unavailable",
        message:
          "Preview isn't available: Twitch doesn't offer this VOD's video for download.",
      },
      export: {
        status: "unavailable",
        reason: "source_unavailable",
        message:
          "Export isn't available: Twitch doesn't offer this VOD's video for download.",
        min_start_s: null,
        max_end_s: null,
        max_duration_s: null,
        layouts: [],
      },
    });

    render(<ClipEditor />);

    expect(await screen.findByText("Source unavailable")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Retry preview" })).toBeNull();
    const localLink = screen.getByRole("link", {
      name: "Analyze with a local file",
    });
    expect(localLink.getAttribute("href")).toBe("/");
  });

  it("recovers from media request errors and video playback errors via Retry preview", async () => {
    const { ApiRequestError } = await import("@/lib/api/client");
    api.getResult.mockResolvedValue(RESULT);
    api.getCandidateMedia
      .mockRejectedValueOnce(
        new ApiRequestError(500, {
          code: "internal_error",
          message: "Temporary server error loading media.",
          details: null,
        }),
      )
      .mockResolvedValue(AVAILABLE_MEDIA);

    render(<ClipEditor />);

    // Must stop spinning and offer Retry preview when initial media fetch fails.
    expect(
      await screen.findByText("Temporary server error loading media."),
    ).toBeTruthy();
    expect(screen.queryByText("Loading video preview…")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Retry preview" }));
    const video = await screen.findByLabelText("Preview of clip 1");
    expect(video).toBeTruthy();

    // If the browser fails to play the video element, Retry preview re-fetches and recovers.
    fireEvent.error(video);
    expect(await screen.findByText("Preview playback failed")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Retry preview" }));
    expect(await screen.findByLabelText("Preview of clip 1")).toBeTruthy();
    expect(api.getCandidateMedia).toHaveBeenCalledTimes(3);
  });
});
