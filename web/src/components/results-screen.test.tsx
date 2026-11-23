// @vitest-environment jsdom

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ResultsScreen } from "@/components/results-screen";
import type {
  AnalysisResult,
  CandidateMedia,
  CreateExportRequest,
  Export,
} from "@/lib/api/types";

const params = vi.hoisted(() => ({ jobId: "job_1" }));

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
      schema_version: "1.0",
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
      schema_version: "1.0",
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
        "Captioning failed on the server, so clips were rendered without captions.",
      ),
    ).toBeTruthy();
    expect(api.createExport).toHaveBeenCalledTimes(2);
    expect(api.createExport.mock.calls[0][0]).toMatchObject({ captions: true });
    expect(api.createExport.mock.calls[1][0]).toMatchObject({
      captions: false,
    });
  });
});
