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

// jsdom can't decode video: the rendered clip's poster frame is stubbed.
const poster = vi.hoisted(() => ({
  clipPoster: vi.fn(() => Promise.resolve("data:image/jpeg;base64,CCCC")),
}));

vi.mock("@/lib/clip-poster", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  clipPoster: poster.clipPoster,
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

/**
 * The trim controls stay locked until candidate media (the export limits) has loaded; the
 * "Allowed range" line replaces the loading status at that point. Wait for it before editing.
 */
async function waitForMediaLimits(range: RegExp = /^Allowed range:/) {
  await screen.findByText(range);
}

/** Export availability for "unavailable": the contract sends null limits and no layouts. */
const UNAVAILABLE_EXPORT: CandidateMedia["export"] = {
  status: "unavailable",
  reason: "fetch_failed",
  message:
    "Export isn't available: downloading the video failed. Trying again later may work.",
  min_start_s: null,
  max_end_s: null,
  max_duration_s: null,
  layouts: [],
};

/** Both time inputs and the eight nudge buttons. */
function trimControls(): (HTMLInputElement | HTMLButtonElement)[] {
  return [
    screen.getByLabelText("Start (seconds)") as HTMLInputElement,
    screen.getByLabelText("End (seconds)") as HTMLInputElement,
    ...[
      "Move start 5 seconds earlier",
      "Move start 1 second earlier",
      "Move start 1 second later",
      "Move start 5 seconds later",
      "Move end 5 seconds earlier",
      "Move end 1 second earlier",
      "Move end 1 second later",
      "Move end 5 seconds later",
    ].map((label) => screen.getByLabelText(label) as HTMLButtonElement),
  ];
}

describe("ClipEditor", () => {
  beforeEach(() => {
    window.localStorage.clear();
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
    await waitForMediaLimits();

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
    // The rendered clip shows a frame from inside it, not a black box, before it plays.
    const rendered = screen.getByLabelText(
      "Rendered vertical clip",
    ) as HTMLVideoElement;
    expect(rendered.getAttribute("preload")).toBe("none");
    // The poster is asked for from an effect, which can land just after the render.
    await vi.waitFor(() =>
      expect(poster.clipPoster).toHaveBeenCalledWith("/mock-media/clip.mp4", {
        width: undefined,
      }),
    );
    await vi.waitFor(() =>
      expect(rendered.getAttribute("poster")).toBe(
        "data:image/jpeg;base64,CCCC",
      ),
    );
    expect(api.createExport).toHaveBeenCalledWith({
      job_id: "job_1",
      candidate_id: "c001",
      start_s: 113,
      end_s: 149,
      layout: "configured_region",
      captions: false,
    });
  });

  it("says when a stacked render fell back, and shows nothing when it didn't (#218)", async () => {
    const note =
      "No facecam found in this part of the stream, so this clip is a center crop.";
    api.getResult.mockResolvedValue(RESULT);
    api.getCandidateMedia.mockResolvedValue({
      ...AVAILABLE_MEDIA,
      export: {
        ...AVAILABLE_MEDIA.export,
        layouts: ["center_crop", "stacked"],
      },
    });
    api.createExport.mockResolvedValue({
      export: makeExport("queued", { layout: "stacked" }),
      created: true,
    });
    api.getExport.mockResolvedValueOnce(
      makeExport("done", {
        layout: "stacked",
        effective_layout: "center_crop",
        layout_note: note,
      }),
    );

    render(<ClipEditor />);
    await waitForMediaLimits();
    fireEvent.click(screen.getByLabelText("Stacked (facecam + gameplay)"));
    fireEvent.click(screen.getByRole("button", { name: "Re-render clip" }));

    await screen.findByRole("link", { name: "Download clip" });
    expect(screen.getByText(note)).toBeTruthy();

    // Changing the layout drops the finished render, and its note with it.
    fireEvent.click(screen.getByLabelText("Center crop (9:16)"));
    expect(screen.queryByText(note)).toBeNull();

    // A render that used the layout as asked has no note.
    api.createExport.mockResolvedValue({
      export: makeExport("queued", { layout: "center_crop" }),
      created: true,
    });
    api.getExport.mockResolvedValue(
      makeExport("done", { layout: "center_crop", layout_note: null }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Re-render clip" }));
    await screen.findByRole("link", { name: "Download clip" });
    expect(screen.queryByText(/no facecam found/i)).toBeNull();
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
    // Wait for candidate media bounds (min_start_s = 88, max_end_s = 178) to load.
    await screen.findByLabelText("Center crop (9:16)");

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
    // Export is unavailable with no retry to offer, so trimming is locked without one (#180).
    expect(
      screen.getByText(
        "Trimming is unavailable while this clip can't be exported.",
      ),
    ).toBeTruthy();
    expect(screen.queryByText(/Retry to check again/)).toBeNull();
    expect(
      (screen.getByLabelText("Start (seconds)") as HTMLInputElement).disabled,
    ).toBe(true);
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

  it("shows error and Retry preview if polling fails after an initial preparing response", async () => {
    const { ApiRequestError } = await import("@/lib/api/client");
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
      .mockRejectedValueOnce(
        new ApiRequestError(500, {
          code: "internal_error",
          message: "Media poll failed mid-fetch.",
          details: null,
        }),
      )
      .mockResolvedValue(AVAILABLE_MEDIA);

    render(<ClipEditor />);

    expect(
      await screen.findByText("Media poll failed mid-fetch."),
    ).toBeTruthy();
    expect(
      screen.queryByText("Fetching the video for this moment…"),
    ).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Retry preview" }));
    expect(await screen.findByLabelText("Preview of clip 1")).toBeTruthy();
    expect(api.getCandidateMedia).toHaveBeenCalledTimes(3);
  });

  it("preserves exact fractional probed bounds when nudging and keeps export enabled", async () => {
    const probedMedia: CandidateMedia = {
      ...AVAILABLE_MEDIA,
      preview: {
        ...AVAILABLE_MEDIA.preview,
        media_start_s: 80,
        media_end_s: 159.96,
      },
      export: {
        ...AVAILABLE_MEDIA.export,
        min_start_s: 80,
        max_end_s: 159.96,
      },
    };
    const nearEndResult: AnalysisResult = {
      ...RESULT,
      candidates: [{ ...RESULT.candidates[0], start_s: 120, end_s: 158 }],
    };
    api.getResult.mockResolvedValue(nearEndResult);
    api.getCandidateMedia.mockResolvedValue(probedMedia);
    api.createExport.mockResolvedValue({
      export: makeExport("done", { start_s: 120, end_s: 159.96 }),
      created: true,
    });

    render(<ClipEditor />);

    const endInput = (await screen.findByLabelText(
      "End (seconds)",
    )) as HTMLInputElement;
    await screen.findByLabelText("Center crop (9:16)");

    fireEvent.click(screen.getByLabelText("Move end 5 seconds later"));
    expect(endInput.value).toBe("159.96");
    expect(
      (screen.getByRole("button", { name: "Reset" }) as HTMLButtonElement)
        .disabled,
    ).toBe(false);

    const rerenderButton = screen.getByRole("button", {
      name: "Re-render clip",
    }) as HTMLButtonElement;
    expect(rerenderButton.disabled).toBe(false);

    fireEvent.click(rerenderButton);
    await vi.waitFor(() =>
      expect(api.createExport).toHaveBeenCalledWith(
        expect.objectContaining({ start_s: 120, end_s: 159.96 }),
      ),
    );

    // Even a sub-0.05 s nudge to a fractional media limit enables Reset.
    cleanup();
    window.localStorage.clear();
    const nearLimitResult: AnalysisResult = {
      ...RESULT,
      candidates: [{ ...RESULT.candidates[0], start_s: 120, end_s: 159.94 }],
    };
    api.getResult.mockResolvedValue(nearLimitResult);
    api.getCandidateMedia.mockResolvedValue(probedMedia);
    render(<ClipEditor />);
    const endInput2 = (await screen.findByLabelText(
      "End (seconds)",
    )) as HTMLInputElement;
    // The input appears with the analysis result; the probed 159.96 s limit arrives with
    // candidate media afterwards. Nudge only once that limit is on screen (#171).
    await waitForMediaLimits(/^Allowed range: 1:20–2:39/);
    fireEvent.click(screen.getByLabelText("Move end 1 second later"));
    expect(endInput2.value).toBe("159.96");
    const resetBtn = screen.getByRole("button", {
      name: "Reset",
    }) as HTMLButtonElement;
    expect(resetBtn.disabled).toBe(false);
    fireEvent.click(resetBtn);
    expect(endInput2.value).toBe("159.94");
  });

  it("locks trim edits until candidate media loads, then clamps nudges to the probed limits", async () => {
    const { rememberedTrim } = await import("@/lib/job-state");
    const probedMedia: CandidateMedia = {
      ...AVAILABLE_MEDIA,
      export: { ...AVAILABLE_MEDIA.export, min_start_s: 80, max_end_s: 159.96 },
    };
    let resolveMedia: ((media: CandidateMedia) => void) | null = null;
    api.getResult.mockResolvedValue({
      ...RESULT,
      candidates: [{ ...RESULT.candidates[0], start_s: 120, end_s: 159.94 }],
    });
    api.getCandidateMedia.mockImplementation(
      () =>
        new Promise<CandidateMedia>((resolve) => {
          resolveMedia = resolve;
        }),
    );

    render(<ClipEditor />);

    const endInput = (await screen.findByLabelText(
      "End (seconds)",
    )) as HTMLInputElement;
    await vi.waitFor(() => expect(resolveMedia).not.toBeNull());

    // While media is pending, every trim control is disabled and points at the reason.
    const status = screen.getByText(
      "Loading media limits… Trimming unlocks when they arrive.",
    );
    const lockedControls = [
      screen.getByLabelText("Start (seconds)"),
      endInput,
      ...[
        "Move start 5 seconds earlier",
        "Move start 1 second earlier",
        "Move start 1 second later",
        "Move start 5 seconds later",
        "Move end 5 seconds earlier",
        "Move end 1 second earlier",
        "Move end 1 second later",
        "Move end 5 seconds later",
      ].map((label) => screen.getByLabelText(label)),
    ] as (HTMLInputElement | HTMLButtonElement)[];
    for (const control of lockedControls) {
      expect(control.disabled).toBe(true);
      expect(control.getAttribute("aria-describedby")).toBe(status.id);
    }
    // The whole-VOD fallback is never presented as the allowed range.
    expect(screen.queryByText(/^Allowed range:/)).toBeNull();

    // A locked nudge neither moves nor saves the trim past the real limit.
    fireEvent.click(screen.getByLabelText("Move end 1 second later"));
    expect(endInput.value).toBe("159.94");
    expect(rememberedTrim("job_1", "c001")).toBeNull();

    resolveMedia!(probedMedia);
    await waitForMediaLimits(/^Allowed range: 1:20–2:39/);
    for (const control of lockedControls) {
      expect(control.disabled).toBe(false);
      expect(control.getAttribute("aria-describedby")).toBeNull();
    }
    fireEvent.click(screen.getByLabelText("Move end 1 second later"));
    expect(endInput.value).toBe("159.96");
  });

  it("explains why trimming is unavailable when the media request fails and unlocks after Retry preview", async () => {
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

    expect(
      await screen.findByText("Temporary server error loading media."),
    ).toBeTruthy();
    expect(
      screen.getByText(
        "Trimming is unavailable because the media limits couldn't be loaded. Use Retry preview to try again.",
      ),
    ).toBeTruthy();
    expect(
      screen.getByText(
        "Trimming and export are paused until the media limits load. Retry to load them.",
      ),
    ).toBeTruthy();
    const startInput = screen.getByLabelText(
      "Start (seconds)",
    ) as HTMLInputElement;
    expect(startInput.disabled).toBe(true);

    fireEvent.click(screen.getByRole("button", { name: "Retry preview" }));
    await waitForMediaLimits(/^Allowed range: 1:28–2:58/);
    expect(startInput.disabled).toBe(false);
    expect(screen.queryByText(/Trimming is unavailable/)).toBeNull();
  });

  it("locks every trim control, playhead buttons included, while export is unavailable (#180)", async () => {
    const { rememberedTrim } = await import("@/lib/job-state");
    api.getResult.mockResolvedValue(RESULT);
    // The preview plays, but export is unavailable, so the contract sends null limits.
    api.getCandidateMedia.mockResolvedValue({
      ...AVAILABLE_MEDIA,
      export: UNAVAILABLE_EXPORT,
    });

    render(<ClipEditor />);

    expect(await screen.findByLabelText("Preview of clip 1")).toBeTruthy();
    const status = await screen.findByText(
      "Trimming is unavailable while this clip can't be exported.",
    );
    // No Retry preview button here, so the status line doesn't mention one.
    expect(screen.queryByRole("button", { name: "Retry preview" })).toBeNull();
    const lockedControls = [
      ...trimControls(),
      ...["Set start to playhead", "Set end to playhead", "Reset"].map(
        (name) => screen.getByRole("button", { name }) as HTMLButtonElement,
      ),
    ];
    for (const control of lockedControls) {
      expect(control.disabled).toBe(true);
      expect(control.getAttribute("aria-describedby")).toBe(status.id);
    }
    // The whole-VOD fallback is never presented as the allowed range.
    expect(screen.queryByText(/^Allowed range:/)).toBeNull();
    // "Play selection" doesn't edit the trim, so it stays usable.
    expect(
      (
        screen.getByRole("button", {
          name: "Play selection",
        }) as HTMLButtonElement
      ).disabled,
    ).toBe(false);

    const endInput = screen.getByLabelText("End (seconds)") as HTMLInputElement;
    fireEvent.click(screen.getByLabelText("Move end 5 seconds later"));
    fireEvent.click(
      screen.getByRole("button", { name: "Set end to playhead" }),
    );
    expect(endInput.value).toBe("148");
    expect(rememberedTrim("job_1", "c001")).toBeNull();
  });

  it("never saves a trim against made-up bounds while export is unavailable and unlocks once it's available (#180)", async () => {
    const { rememberedTrim } = await import("@/lib/job-state");
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
        export: UNAVAILABLE_EXPORT,
      })
      .mockResolvedValue({
        ...AVAILABLE_MEDIA,
        export: { ...AVAILABLE_MEDIA.export, max_end_s: 148.5 },
      });

    render(<ClipEditor />);

    const status = await screen.findByText(
      "Trimming is unavailable while this clip can't be exported. Use Retry preview to check again.",
    );
    expect(
      screen.getByText(
        "Trimming and export are paused while this clip can't be exported. Retry to check again.",
      ),
    ).toBeTruthy();
    for (const control of trimControls()) {
      expect(control.disabled).toBe(true);
      expect(control.getAttribute("aria-describedby")).toBe(status.id);
    }

    // The #180 repro: before the fix this nudge clamped only to the VOD's length (360 s) and
    // saved 149 s, past the real 148.5 s limit that arrives after the retry.
    const endInput = screen.getByLabelText("End (seconds)") as HTMLInputElement;
    fireEvent.click(screen.getByLabelText("Move end 1 second later"));
    expect(endInput.value).toBe("148");
    expect(rememberedTrim("job_1", "c001")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Retry preview" }));
    await waitForMediaLimits(/^Allowed range: 1:28–2:28/);
    expect(screen.queryByText(/Trimming is unavailable/)).toBeNull();
    for (const control of trimControls()) {
      expect(control.disabled).toBe(false);
      expect(control.getAttribute("aria-describedby")).toBeNull();
    }
    fireEvent.click(screen.getByLabelText("Move end 1 second later"));
    expect(endInput.value).toBe("148.5");
    expect(rememberedTrim("job_1", "c001")).toEqual({
      start_s: 118,
      end_s: 148.5,
    });
  });

  it("enforces the 0.5 s minimum for manual entries and short initial candidates", async () => {
    const shortResult: AnalysisResult = {
      ...RESULT,
      candidates: [{ ...RESULT.candidates[0], start_s: 120, end_s: 120.1 }],
    };
    api.getResult.mockResolvedValue(shortResult);
    api.getCandidateMedia.mockResolvedValue(AVAILABLE_MEDIA);

    render(<ClipEditor />);

    expect(await screen.findByText(/at least 0\.5 seconds/)).toBeTruthy();
    const rerenderButton = screen.getByRole("button", {
      name: "Re-render clip",
    }) as HTMLButtonElement;
    expect(rerenderButton.disabled).toBe(true);
    await waitForMediaLimits();

    // Manually entering a 0.1 s selection clamps to 0.5 s on blur and enables export.
    const endInput = screen.getByLabelText("End (seconds)") as HTMLInputElement;
    fireEvent.change(endInput, { target: { value: "120.2" } });
    fireEvent.blur(endInput);
    expect(endInput.value).toBe("120.5");
    expect(screen.queryByText(/at least 0\.5 seconds/)).toBeNull();
    expect(rerenderButton.disabled).toBe(false);
  });

  it("persists edited trims in shared job state, restores them on reopen, and clears on reset", async () => {
    const { rememberedTrim } = await import("@/lib/job-state");
    api.getResult.mockResolvedValue(RESULT);
    api.getCandidateMedia.mockResolvedValue(AVAILABLE_MEDIA);

    const { unmount } = render(<ClipEditor />);
    const startInput = (await screen.findByLabelText(
      "Start (seconds)",
    )) as HTMLInputElement;
    await screen.findByLabelText("Center crop (9:16)");

    fireEvent.click(screen.getByLabelText("Move start 5 seconds earlier"));
    expect(startInput.value).toBe("113");
    expect(rememberedTrim("job_1", "c001")).toEqual({
      start_s: 113,
      end_s: 148,
    });

    // Reopening the clip editor restores the saved trim.
    unmount();
    render(<ClipEditor />);
    const reopenedStart = (await screen.findByLabelText(
      "Start (seconds)",
    )) as HTMLInputElement;
    expect(reopenedStart.value).toBe("113");
    await waitForMediaLimits();

    // Resetting restores original candidate bounds and clears the saved trim.
    fireEvent.click(screen.getByRole("button", { name: "Reset" }));
    expect(reopenedStart.value).toBe("118");
    expect(rememberedTrim("job_1", "c001")).toBeNull();
  });

  it("renders the candidate title, reason, and expandable Why signals in the header", async () => {
    const richResult: AnalysisResult = {
      ...RESULT,
      schema_version: "1.1",
      candidates: [
        {
          ...RESULT.candidates[0],
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
              kind: "pause_snap",
              label: "Speech boundaries",
              detail: "Start and end aligned to speech pauses",
            },
          ],
        },
      ],
    };
    api.getResult.mockResolvedValue(richResult);
    api.getCandidateMedia.mockResolvedValue(AVAILABLE_MEDIA);

    render(<ClipEditor />);

    expect(
      await screen.findByRole("heading", {
        level: 1,
        name: "Loud audio & laughing chat",
      }),
    ).toBeTruthy();
    expect(
      screen.getByText(
        "Chat jumped to 6× its usual rate with lots of laugh reactions; loudest moment of the stream; 3 viewer clips",
      ),
    ).toBeTruthy();

    const whyButton = screen.getByRole("button", { name: /Why this moment/i });
    expect(whyButton.getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByText("6× usual stream rate (z=3.4)")).toBeNull();

    fireEvent.click(whyButton);
    expect(whyButton.getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByText("Chat rate")).toBeTruthy();
    expect(screen.getByText("6× usual stream rate (z=3.4)")).toBeTruthy();
    expect(screen.getByText("Speech boundaries")).toBeTruthy();
    expect(
      screen.getByText("Start and end aligned to speech pauses"),
    ).toBeTruthy();
  });
});
