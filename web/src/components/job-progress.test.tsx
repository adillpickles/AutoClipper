// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { JobProgress } from "@/components/job-progress";
import { ApiRequestError } from "@/lib/api/errors";
import emptyResult from "@/lib/api/fixtures/result-empty.json";
import exampleResult from "@/lib/api/fixtures/result-example.json";
import type { AnalysisResult, Job } from "@/lib/api/types";

const state = vi.hoisted(() => ({
  jobId: "job_1",
  getJob: vi.fn(),
  getResult: vi.fn(),
  router: { replace: vi.fn() },
}));

vi.mock("next/navigation", () => ({
  useParams: () => ({ jobId: state.jobId }),
  useRouter: () => state.router,
}));

vi.mock("@/lib/api/client", async () => {
  const actual =
    await vi.importActual<typeof import("@/lib/api/client")>(
      "@/lib/api/client",
    );
  return {
    ...actual,
    getApiClient: () => ({
      getJob: state.getJob,
      getResult: state.getResult,
    }),
  };
});

function makeJob(overrides: Partial<Job> = {}): Job {
  return {
    job_id: "job_1",
    video_id: "9000000001",
    video_path: null,
    state: "running",
    progress: {
      stage: "downloading_audio",
      stage_index: 4,
      stage_count: 6,
      stage_fraction: 0.5,
      message: "Downloading audio",
    },
    warnings: [],
    error: null,
    created_at: "2026-11-20T12:00:00Z",
    started_at: "2026-11-20T12:00:01Z",
    updated_at: "2026-11-20T12:00:05Z",
    finished_at: null,
    ...overrides,
  };
}

function makeEmptyResult(): AnalysisResult {
  return emptyResult as AnalysisResult;
}

describe("JobProgress", () => {
  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it("shows a loading skeleton while fetching the job", () => {
    state.getJob.mockReturnValue(new Promise(() => {}));
    const { container } = render(<JobProgress />);

    const status = screen.getByRole("status");
    expect(status.getAttribute("aria-busy")).toBeNull();
    expect(container.querySelector('[aria-busy="true"]')).toBeTruthy();
    expect(
      screen.getByRole("heading", {
        level: 1,
        name: "Loading analysis status…",
      }),
    ).toBeTruthy();
  });

  it("shows recovery actions on a 500 error and retries on click", async () => {
    state.getJob
      .mockRejectedValueOnce(
        new ApiRequestError(500, {
          code: "internal_error",
          message: "Backend crashed",
          details: null,
        }),
      )
      .mockResolvedValueOnce(makeJob());

    render(<JobProgress />);

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("Backend crashed");
    expect(
      screen
        .getByRole("link", { name: "Start another VOD" })
        .getAttribute("href"),
    ).toBe("/");

    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    // First seen mid-run, the stepper starts from the first stage (#188).
    expect(await screen.findByText(/Step 1 of 6/)).toBeTruthy();
    expect(state.getJob).toHaveBeenCalledTimes(2);
  });

  it("shows a recovery link when the job no longer exists (404)", async () => {
    state.getJob.mockRejectedValue(
      new ApiRequestError(404, {
        code: "job_not_found",
        message: "That analysis wasn't found.",
        details: null,
      }),
    );

    render(<JobProgress />);

    expect(
      await screen.findByRole("heading", {
        level: 1,
        name: "This analysis no longer exists",
      }),
    ).toBeTruthy();
    expect(
      screen
        .getByRole("link", { name: "Start a new analysis" })
        .getAttribute("href"),
    ).toBe("/");
  });

  it("marks the active stage with aria-current='step'", async () => {
    state.getJob.mockResolvedValue(makeJob());
    render(<JobProgress />);

    await screen.findByText(/Step 1 of 6/);
    const active = document.querySelector('li[aria-current="step"]');
    expect(active?.textContent).toContain("Looking up the VOD");
  });

  it("renders EmptyState with recovery links when analysis finishes with zero moments", async () => {
    state.getJob.mockResolvedValue(
      makeJob({
        video_id: "9000000006",
        state: "done",
        progress: null,
        finished_at: "2026-11-20T12:01:00Z",
      }),
    );
    state.getResult.mockResolvedValue(makeEmptyResult());

    render(<JobProgress />);

    expect(
      await screen.findByRole("heading", {
        level: 2,
        name: "No moments stood out",
      }),
    ).toBeTruthy();
    expect(
      screen
        .getByRole("link", { name: "Try another VOD" })
        .getAttribute("href"),
    ).toBe("/");
    expect(
      screen
        .getByRole("link", { name: "See the results" })
        .getAttribute("href"),
    ).toBe("/jobs/job_1/results");
  });

  it("hides the numbers row for results from before the counts existed (#217)", async () => {
    const old = { ...(exampleResult as AnalysisResult) };
    delete old.n_chat_messages;
    delete old.n_windows;
    state.getJob.mockResolvedValue(
      makeJob({
        state: "done",
        progress: null,
        finished_at: "2026-11-20T12:01:00Z",
      }),
    );
    state.getResult.mockResolvedValue(old);

    render(<JobProgress />);

    expect(
      await screen.findByRole("link", { name: "See the clips" }),
    ).toBeTruthy();
    expect(screen.queryByLabelText("Analysis numbers")).toBeNull();
    expect(screen.queryByText("–")).toBeNull();
  });

  it("says how many moments were scored and that the top 10 were picked (#217)", async () => {
    const base = exampleResult as AnalysisResult;
    const many: AnalysisResult = {
      ...base,
      candidates: Array.from({ length: 82 }, (_, i) => ({
        ...base.candidates[0],
        candidate_id: `c${i}`,
        rank: i + 1,
      })),
    };
    state.getJob.mockResolvedValue(
      makeJob({
        state: "done",
        progress: null,
        finished_at: "2026-11-20T12:01:00Z",
      }),
    );
    state.getResult.mockResolvedValue(many);

    render(<JobProgress />);

    expect(
      await screen.findByText("Scored 82 moments · picked the top 10."),
    ).toBeTruthy();
    const moments = screen.getByText("Moments scored", { selector: "dt" });
    expect(moments.nextElementSibling?.textContent).toBe("82");
  });
});
