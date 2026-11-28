// @vitest-environment jsdom

import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ResultsScreen } from "@/components/results-screen";
import type {
  AnalysisResult,
  Candidate,
  CandidateMedia,
  CreateExportRequest,
  Export,
} from "@/lib/api/types";

// Export polls must not re-render the clip cards whose export didn't change (#241).

const renders = vi.hoisted(() => new Map<string, number>());

vi.mock("@/components/clip-card", async (importOriginal) => {
  const original =
    await importOriginal<typeof import("@/components/clip-card")>();
  return {
    ...original,
    ClipCard: (props: Parameters<typeof original.ClipCard>[0]) => {
      const id = props.candidate.candidate_id;
      renders.set(id, (renders.get(id) ?? 0) + 1);
      return original.ClipCard(props);
    },
  };
});

const api = vi.hoisted(() => ({
  getHealth: vi.fn(),
  getResult: vi.fn(),
  getCandidateMedia: vi.fn(),
  createExport: vi.fn(),
  getExport: vi.fn(),
  resolveUrl: vi.fn((url: string) => url),
  reanalyzeJob: vi.fn(),
}));

vi.mock("next/navigation", () => ({
  useParams: () => ({ jobId: "job_1" }),
  useRouter: () => ({ push: vi.fn() }),
}));

vi.mock("@/lib/api/client", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  POLL_INTERVAL_MS: 10,
  getApiClient: () => api,
}));

const CANDIDATES: Candidate[] = Array.from({ length: 10 }, (_, i) => ({
  candidate_id: `c${String(i + 1).padStart(3, "0")}`,
  rank: i + 1,
  score: 10 - i,
  start_s: 30 * i,
  end_s: 30 * i + 20,
  source: "discovered",
  n_viewer_clips: 0,
  top_signals: ["chat_msg_rate_z"],
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
  n_viewer_clips_total: 0,
  warnings: [],
  candidates: CANDIDATES,
};

function media(candidateId: string): CandidateMedia {
  return {
    job_id: "job_1",
    candidate_id: candidateId,
    video_id: "9000000001",
    preview: {
      status: "unavailable",
      kind: null,
      url: null,
      media_start_s: null,
      media_end_s: null,
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
}

function makeExport(req: CreateExportRequest, state: Export["state"]): Export {
  return {
    export_id: `exp_${req.candidate_id}`,
    job_id: req.job_id,
    candidate_id: req.candidate_id,
    video_id: "9000000001",
    start_s: req.start_s,
    end_s: req.end_s,
    layout: req.layout,
    captions: req.captions,
    state,
    progress:
      state === "running"
        ? {
            stage: "rendering",
            stage_index: 2,
            stage_count: 3,
            stage_fraction: 0.5,
            message: "Rendering the clip",
          }
        : null,
    error: null,
    download_url: state === "done" ? "/mock-media/clip.mp4" : null,
    created_at: "2026-11-20T12:01:00Z",
    started_at: "2026-11-20T12:01:01Z",
    updated_at: "2026-11-20T12:01:02Z",
    finished_at: state === "done" ? "2026-11-20T12:01:05Z" : null,
  };
}

describe("ResultsScreen export polling", () => {
  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
    renders.clear();
    window.localStorage.clear();
  });

  it("re-renders only the clip card whose export changed on a poll tick", async () => {
    api.getHealth.mockResolvedValue({
      status: "ok",
      schema_version: "1.1",
      job_runner: "pipeline",
      captions_available: true,
    });
    api.getResult.mockResolvedValue(RESULT);
    api.getCandidateMedia.mockImplementation(async (_job: string, id: string) =>
      media(id),
    );
    const requests = new Map<string, CreateExportRequest>();
    api.createExport.mockImplementation(async (req: CreateExportRequest) => {
      requests.set(`exp_${req.candidate_id}`, req);
      // The last clip keeps rendering; the others are done at once.
      const state = req.candidate_id === "c010" ? "running" : "done";
      return { export: makeExport(req, state), created: true };
    });
    // The running export answers each poll with a fresh but identical object.
    api.getExport.mockImplementation(async (id: string) =>
      makeExport(requests.get(id)!, "running"),
    );

    render(<ResultsScreen />);
    await screen.findByText(/Rendering clips: 9 of 10 done/, undefined, {
      timeout: 5000,
    });
    await act(() => new Promise((resolve) => setTimeout(resolve, 50)));

    const pollsBefore = api.getExport.mock.calls.length;
    const before = new Map(renders);
    // Wait for several more polls of the running export (by count, not time, so a busy test
    // machine can't make this pass without polling).
    await act(() =>
      vi.waitFor(
        () =>
          expect(api.getExport.mock.calls.length - pollsBefore).toBeGreaterThan(
            4,
          ),
        { timeout: 5000 },
      ),
    );
    // Let the last poll's answer land before counting.
    await act(() => new Promise((resolve) => setTimeout(resolve, 30)));

    const extra = (id: string) =>
      (renders.get(id) ?? 0) - (before.get(id) ?? 0);
    // Neither the finished cards nor the unchanged running one re-render.
    for (const candidate of CANDIDATES) {
      expect(extra(candidate.candidate_id)).toBe(0);
    }
  });
});
