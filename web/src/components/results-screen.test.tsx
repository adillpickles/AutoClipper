// @vitest-environment jsdom

import {
  cleanup,
  fireEvent,
  render,
  screen,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ResultsScreen } from "@/components/results-screen";
import type {
  AnalysisResult,
  CandidateMedia,
  CreateExportRequest,
  Export,
} from "@/lib/api/types";
import { clearRememberedTrim, rememberTrim } from "@/lib/job-state";

const params = vi.hoisted(() => ({ jobId: "job_1" }));

const api = vi.hoisted(() => ({
  getHealth: vi.fn(),
  getResult: vi.fn(),
  getCandidateMedia: vi.fn(),
  createExport: vi.fn(),
  getExport: vi.fn(),
  createHighlights: vi.fn(),
  getHighlights: vi.fn(),
  resolveUrl: vi.fn((url: string) => url),
  reanalyzeJob: vi.fn(),
}));

const router = vi.hoisted(() => ({ push: vi.fn() }));

vi.mock("next/navigation", () => ({
  useParams: () => params,
  useRouter: () => router,
}));

vi.mock("@/lib/api/client", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  POLL_INTERVAL_MS: 20,
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

const MEDIA: CandidateMedia = {
  job_id: "job_1",
  candidate_id: "c001",
  video_id: "9000000001",
  preview: {
    status: "available",
    kind: "video_file",
    url: "/mock-media/preview.mp4",
    media_start_s: 0,
    media_end_s: 360,
    reason: null,
    message: null,
  },
  export: {
    status: "available",
    reason: null,
    message: null,
    min_start_s: 0,
    max_end_s: 360,
    max_duration_s: 90,
    layouts: ["center_crop"],
  },
};

function makeExport(
  req: CreateExportRequest,
  overrides: Partial<Export> = {},
): Export {
  return {
    export_id: req.captions ? "exp_captioned" : "exp_plain",
    job_id: req.job_id,
    candidate_id: req.candidate_id,
    video_id: "9000000001",
    start_s: req.start_s,
    end_s: req.end_s,
    layout: req.layout,
    captions: req.captions,
    state: "done",
    progress: null,
    error: null,
    download_url: "/mock-media/clip.mp4",
    created_at: "2026-11-20T12:01:00Z",
    started_at: "2026-11-20T12:01:01Z",
    updated_at: "2026-11-20T12:01:02Z",
    finished_at: "2026-11-20T12:01:05Z",
    ...overrides,
  };
}

describe("ResultsScreen captions capability and fallback", () => {
  beforeEach(() => {
    window.localStorage.clear();
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it("exports without captions and shows a notice when /health reports captions_available false", async () => {
    api.getHealth.mockResolvedValue({
      status: "ok",
      schema_version: "1.1",
      job_runner: "pipeline",
      captions_available: false,
    });
    api.getResult.mockResolvedValue(RESULT);
    api.getCandidateMedia.mockResolvedValue(MEDIA);
    api.createExport.mockImplementation(async (req: CreateExportRequest) => ({
      export: makeExport(req),
      created: true,
    }));

    render(<ResultsScreen />);

    expect(await screen.findByText(/1 of 1 clips ready/)).toBeTruthy();
    expect(screen.getByText(/uv sync --extra captions/)).toBeTruthy();
    expect(api.createExport).toHaveBeenCalledTimes(1);
    expect(api.createExport).toHaveBeenCalledWith(
      expect.objectContaining({ candidate_id: "c001", captions: false }),
    );
  });

  it("exports with captions when /health reports captions_available true", async () => {
    api.getHealth.mockResolvedValue({
      status: "ok",
      schema_version: "1.1",
      job_runner: "mock",
      captions_available: true,
    });
    api.getResult.mockResolvedValue(RESULT);
    api.getCandidateMedia.mockResolvedValue(MEDIA);
    api.createExport.mockImplementation(async (req: CreateExportRequest) => ({
      export: makeExport(req),
      created: true,
    }));

    render(<ResultsScreen />);

    expect(await screen.findByText(/1 of 1 clips ready/)).toBeTruthy();
    expect(api.createExport).toHaveBeenCalledTimes(1);
    expect(api.createExport).toHaveBeenCalledWith(
      expect.objectContaining({ candidate_id: "c001", captions: true }),
    );
  });

  it("doesn't request its clips again when the results page is opened a second time (#186)", async () => {
    api.getHealth.mockResolvedValue({
      status: "ok",
      schema_version: "1.1",
      job_runner: "mock",
      captions_available: true,
    });
    api.getResult.mockResolvedValue(RESULT);
    api.getCandidateMedia.mockResolvedValue(MEDIA);
    api.createExport.mockImplementation(async (req: CreateExportRequest) => ({
      export: makeExport(req),
      created: true,
    }));
    api.getExport.mockImplementation(async (exportId: string) =>
      makeExport(
        {
          job_id: "job_1",
          candidate_id: "c001",
          start_s: 118,
          end_s: 148,
          layout: "center_crop",
          captions: true,
        },
        { export_id: exportId },
      ),
    );

    render(<ResultsScreen />);
    expect(await screen.findByText(/1 of 1 clips ready/)).toBeTruthy();
    cleanup();
    render(<ResultsScreen />);
    expect(await screen.findByText(/1 of 1 clips ready/)).toBeTruthy();

    expect(api.createExport).toHaveBeenCalledTimes(1);
    expect(api.getExport).toHaveBeenCalledWith("exp_captioned");
  });

  it("falls back to uncaptioned export if a captioned export fails with captioning_failed", async () => {
    api.getHealth.mockRejectedValue(new Error("temporary health error"));
    api.getResult.mockResolvedValue(RESULT);
    api.getCandidateMedia.mockResolvedValue(MEDIA);
    api.createExport.mockImplementation(async (req: CreateExportRequest) => {
      if (req.captions) {
        return {
          export: makeExport(req, {
            state: "failed",
            download_url: null,
            error: {
              code: "captioning_failed",
              message: "Captions couldn't be generated for this clip.",
              details: null,
            },
          }),
          created: true,
        };
      }
      return { export: makeExport(req), created: true };
    });

    render(<ResultsScreen />);

    expect(await screen.findByText(/1 of 1 clips ready/)).toBeTruthy();
    expect(
      screen.getByText(
        "Captioning failed on the server for one or more clips, so they were rendered without captions.",
      ),
    ).toBeTruthy();
    expect(api.createExport).toHaveBeenCalledTimes(2);
    expect(api.createExport.mock.calls[0][0]).toMatchObject({ captions: true });
    expect(api.createExport.mock.calls[1][0]).toMatchObject({
      captions: false,
    });
  });

  it("preserves custom trim and layout from a remembered failed captioned export and keeps fallback local to the failed clip", async () => {
    window.localStorage.setItem(
      "autoclipper.exports.job_1",
      JSON.stringify({ c001: "exp_custom_failed" }),
    );
    const twoCandidates: AnalysisResult = {
      ...RESULT,
      candidates: [
        RESULT.candidates[0],
        {
          candidate_id: "c002",
          rank: 2,
          score: 2.8,
          start_s: 200,
          end_s: 230,
          source: "discovered",
          n_viewer_clips: 0,
          top_signals: ["audio_rms_max_z"],
        },
      ],
    };
    api.getHealth.mockResolvedValue({
      status: "ok",
      schema_version: "1.1",
      job_runner: "pipeline",
      captions_available: true,
    });
    api.getResult.mockResolvedValue(twoCandidates);
    api.getExport.mockResolvedValueOnce(
      makeExport(
        {
          job_id: "job_1",
          candidate_id: "c001",
          start_s: 112.5,
          end_s: 151.0,
          layout: "stacked",
          captions: true,
        },
        {
          export_id: "exp_custom_failed",
          state: "failed",
          download_url: null,
          error: {
            code: "captioning_failed",
            message: "Captions couldn't be generated for this clip.",
            details: null,
          },
        },
      ),
    );
    api.getCandidateMedia.mockImplementation(
      async (_jobId: string, candidateId: string) => ({
        ...MEDIA,
        candidate_id: candidateId,
        export: {
          ...MEDIA.export,
          layouts: ["center_crop", "stacked"],
        },
      }),
    );
    api.createExport.mockImplementation(async (req: CreateExportRequest) => ({
      export: makeExport(req),
      created: true,
    }));

    render(<ResultsScreen />);

    expect(await screen.findByText(/2 of 2 clips ready/)).toBeTruthy();
    expect(api.createExport).toHaveBeenCalledWith({
      job_id: "job_1",
      candidate_id: "c001",
      start_s: 112.5,
      end_s: 151.0,
      layout: "stacked",
      captions: false,
    });
    expect(api.createExport).toHaveBeenCalledWith({
      job_id: "job_1",
      candidate_id: "c002",
      start_s: 200,
      end_s: 230,
      layout: "center_crop",
      captions: true,
    });
  });

  it("initializes the highlights dialog from trims saved in the clip editor", async () => {
    rememberTrim("job_1", "c001", { start_s: 113, end_s: 148 });
    api.getHealth.mockResolvedValue({
      status: "ok",
      schema_version: "1.1",
      job_runner: "pipeline",
      captions_available: true,
    });
    api.getResult.mockResolvedValue(RESULT);
    api.getCandidateMedia.mockResolvedValue(MEDIA);
    api.createExport.mockImplementation(async (req: CreateExportRequest) => ({
      export: makeExport(req),
      created: true,
    }));
    api.createHighlights.mockResolvedValue({
      highlights: {
        highlights_id: "hl_1",
        job_id: "job_1",
        video_id: "9000000001",
        segments: [{ candidate_id: "c001", start_s: 113, end_s: 148 }],
        order: "chronological",
        captions: false,
        state: "done",
        progress: null,
        error: null,
        download_url: "/mock-media/highlights.mp4",
        duration_s: 35,
        created_at: "2026-11-20T12:02:00Z",
        started_at: "2026-11-20T12:02:01Z",
        updated_at: "2026-11-20T12:02:03Z",
        finished_at: "2026-11-20T12:02:03Z",
      },
      created: true,
    });

    render(<ResultsScreen />);

    expect(await screen.findByText(/1 of 1 clips ready/)).toBeTruthy();
    fireEvent.click(
      screen.getByRole("button", { name: "Make highlights video" }),
    );
    // The dialog loads in its own chunk (#241).
    await screen.findByRole("dialog");
    const startInput = document.getElementById(
      "start-c001",
    ) as HTMLInputElement;
    expect(startInput.value).toBe("113");

    const dialog = screen.getByRole("dialog");
    fireEvent.click(
      dialog.querySelector('button:not([aria-label="Close"])') as HTMLElement,
    );
    await screen.findByText(/The highlights video is ready/);
    expect(api.createHighlights).toHaveBeenCalledWith(
      expect.objectContaining({
        job_id: "job_1",
        segments: [{ candidate_id: "c001", start_s: 113, end_s: 148 }],
      }),
    );
  });

  it("does not revive an earlier remembered export trim in highlights after the user resets in the editor", async () => {
    window.localStorage.setItem(
      "autoclipper.exports.job_1",
      JSON.stringify({ c001: "exp_old_trim" }),
    );
    clearRememberedTrim("job_1", "c001", { recordReset: true });
    api.getHealth.mockResolvedValue({
      status: "ok",
      schema_version: "1.1",
      job_runner: "pipeline",
      captions_available: true,
    });
    api.getResult.mockResolvedValue(RESULT);
    api.getExport.mockResolvedValue(
      makeExport(
        {
          job_id: "job_1",
          candidate_id: "c001",
          start_s: 110,
          end_s: 148,
          layout: "center_crop",
          captions: true,
        },
        { export_id: "exp_old_trim" },
      ),
    );

    render(<ResultsScreen />);

    expect(await screen.findByText(/1 of 1 clips ready/)).toBeTruthy();
    fireEvent.click(
      screen.getByRole("button", { name: "Make highlights video" }),
    );
    // The dialog loads in its own chunk (#241).
    await screen.findByRole("dialog");
    const startInput = document.getElementById(
      "start-c001",
    ) as HTMLInputElement;
    // Must stay at the original candidate start (118), not revive the old export's 110.
    expect(startInput.value).toBe("118");
  });
});

describe("ResultsScreen with exports queued when the analysis finished (#187)", () => {
  beforeEach(() => {
    window.localStorage.clear();
    api.getHealth.mockResolvedValue({
      status: "ok",
      schema_version: "1.1",
      job_runner: "mock",
      captions_available: true,
    });
    api.getResult.mockResolvedValue(RESULT);
    api.getCandidateMedia.mockResolvedValue(MEDIA);
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it("asks for exactly the export the server queued and shows it rendering", async () => {
    const queued: CreateExportRequest = {
      job_id: "job_1",
      candidate_id: "c001",
      start_s: 118,
      end_s: 148,
      layout: "center_crop",
      captions: true,
    };
    // The server returns its auto-queued render (200, created false), already running.
    api.createExport.mockImplementation(async (req: CreateExportRequest) => ({
      export: makeExport(req, {
        export_id: "exp_auto",
        state: "running",
        progress: {
          stage: "rendering",
          stage_index: 3,
          stage_count: 3,
          stage_fraction: null,
          message: "Rendering the clip",
        },
        download_url: null,
        finished_at: null,
      }),
      created: false,
    }));
    api.getExport.mockImplementation(async () =>
      makeExport(queued, { export_id: "exp_auto" }),
    );

    render(<ResultsScreen />);

    expect(await screen.findByText(/^Rendering the clip$/)).toBeTruthy();
    // Field for field the request the server auto-queued, so it reuses that render.
    expect(api.createExport).toHaveBeenCalledTimes(1);
    expect(api.createExport).toHaveBeenCalledWith(queued);
    expect(await screen.findByText(/1 of 1 clips ready/)).toBeTruthy();
    expect(api.getExport).toHaveBeenCalledWith("exp_auto");
  });
});

describe("ResultsScreen re-analyze", () => {
  beforeEach(() => {
    window.localStorage.clear();
    api.getHealth.mockResolvedValue({
      status: "ok",
      schema_version: "1.1",
      job_runner: "mock",
      captions_available: true,
    });
    api.getResult.mockResolvedValue(RESULT);
    api.getCandidateMedia.mockResolvedValue(MEDIA);
    api.createExport.mockImplementation(async (req: CreateExportRequest) => ({
      export: makeExport(req),
      created: false,
    }));
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it("starts a fresh analysis after a confirm and follows its progress", async () => {
    api.reanalyzeJob.mockResolvedValue({
      job: { job_id: "job_new", state: "queued" },
      created: true,
    });
    render(<ResultsScreen />);
    await screen.findByText(/1 of 1 clips ready/);

    fireEvent.click(screen.getByRole("button", { name: "Re-analyze" }));
    const confirm = screen.getByRole("region", {
      name: "Confirm re-analyzing this VOD",
    });
    expect(api.reanalyzeJob).not.toHaveBeenCalled();
    // Focus moves into the panel, onto its confirm button.
    expect(document.activeElement).toBe(
      within(confirm).getByRole("button", { name: "Re-analyze" }),
    );

    // Cancel leaves everything as it was, with focus back on "Re-analyze".
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(confirm.isConnected).toBe(false);
    expect(api.reanalyzeJob).not.toHaveBeenCalled();
    expect(document.activeElement).toBe(
      screen.getByRole("button", { name: "Re-analyze" }),
    );

    fireEvent.click(screen.getByRole("button", { name: "Re-analyze" }));
    fireEvent.click(
      within(
        screen.getByRole("region", { name: "Confirm re-analyzing this VOD" }),
      ).getByRole("button", { name: "Re-analyze" }),
    );

    await vi.waitFor(() =>
      expect(router.push).toHaveBeenCalledWith("/jobs/job_new"),
    );
    expect(api.reanalyzeJob).toHaveBeenCalledTimes(1);
    expect(api.reanalyzeJob).toHaveBeenCalledWith("job_1");
  });

  it("shows the server's message when the new analysis can't start", async () => {
    const { ApiRequestError } = await import("@/lib/api/errors");
    api.reanalyzeJob.mockRejectedValue(
      new ApiRequestError(422, {
        code: "invalid_video_path",
        message: "That video file can't be opened.",
        details: null,
      }),
    );
    render(<ResultsScreen />);
    await screen.findByText(/1 of 1 clips ready/);

    fireEvent.click(screen.getByRole("button", { name: "Re-analyze" }));
    fireEvent.click(
      within(
        screen.getByRole("region", { name: "Confirm re-analyzing this VOD" }),
      ).getByRole("button", { name: "Re-analyze" }),
    );

    expect((await screen.findByRole("alert")).textContent).toBe(
      "That video file can't be opened.",
    );
    expect(router.push).not.toHaveBeenCalled();
  });
});
