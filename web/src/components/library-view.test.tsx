// @vitest-environment jsdom

import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  within,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { LibraryView } from "@/components/library-view";
import { ApiRequestError, POLL_INTERVAL_MS } from "@/lib/api/client";
import type { JobListResponse, JobSummary } from "@/lib/api/types";

const state = vi.hoisted(() => ({
  listJobs: vi.fn(),
  deleteJob: vi.fn(),
}));

vi.mock("@/lib/api/client", async () => {
  const actual =
    await vi.importActual<typeof import("@/lib/api/client")>(
      "@/lib/api/client",
    );
  return {
    ...actual,
    getApiClient: () => ({
      listJobs: state.listJobs,
      deleteJob: state.deleteJob,
    }),
  };
});

function makeSummary(overrides: Partial<JobSummary>): JobSummary {
  return {
    job_id: "job_1",
    video_id: "9000000001",
    video_path: null,
    state: "done",
    progress: null,
    title: "Synthetic stream 1",
    broadcaster_id: "9100000001",
    broadcaster_display_name: "user_001",
    duration_s: 360,
    thumbnail_url: null,
    candidate_count: 3,
    result_available: true,
    export_count: 2,
    missing_export_count: 0,
    highlights_count: 1,
    missing_highlights_count: 0,
    exports: [],
    highlights: [],
    warning_count: 0,
    error: null,
    created_at: "2026-11-26T15:00:00Z",
    started_at: "2026-11-26T15:00:01Z",
    updated_at: "2026-11-26T15:02:00Z",
    finished_at: "2026-11-26T15:02:00Z",
    ...overrides,
  };
}

function page(
  items: JobSummary[],
  { total = items.length, hasMore = false } = {},
): JobListResponse {
  return {
    items,
    total,
    limit: 20,
    offset: 0,
    has_more: hasMore,
  };
}

describe("LibraryView", () => {
  afterEach(() => {
    vi.useRealTimers();
    cleanup();
    vi.clearAllMocks();
  });

  it("shows a loading skeleton, then renders the empty state when there are no jobs", async () => {
    let resolveList: ((res: JobListResponse) => void) | undefined;
    state.listJobs.mockImplementation(
      () =>
        new Promise<JobListResponse>((resolve) => {
          resolveList = resolve;
        }),
    );

    render(<LibraryView />);
    expect(screen.getByLabelText("Loading library…")).toBeTruthy();

    await act(async () => {
      resolveList?.(page([]));
    });

    expect(screen.getByText("No analyses yet")).toBeTruthy();
    const analyzeLinks = screen.getAllByRole("link", { name: "Analyze a VOD" });
    expect(analyzeLinks.length).toBeGreaterThanOrEqual(2);
    expect(analyzeLinks[0].getAttribute("href")).toBe("/");
    expect(
      screen
        .getByRole("link", { name: "Browse a channel" })
        .getAttribute("href"),
    ).toBe("/channels");
  });

  it("shows an error state with retry when loading fails", async () => {
    state.listJobs.mockRejectedValueOnce(new TypeError("Network error"));

    render(<LibraryView />);
    expect(await screen.findByText("Couldn't load the library")).toBeTruthy();

    state.listJobs.mockResolvedValueOnce(page([makeSummary({})]));
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));

    expect(await screen.findByText("Synthetic stream 1")).toBeTruthy();
  });

  it("renders done, running, local-file fallback, and interrupted jobs with links and counts", async () => {
    state.listJobs.mockResolvedValueOnce(
      page([
        makeSummary({
          job_id: "job_run",
          video_id: "9000000010",
          video_path: "C:\\Videos\\friday_vod.mp4",
          state: "running",
          progress: {
            stage: "downloading_audio",
            stage_index: 4,
            stage_count: 6,
            stage_fraction: 0.5,
            message: "Downloading audio",
          },
          title: null,
          broadcaster_id: null,
          broadcaster_display_name: null,
          duration_s: null,
          candidate_count: null,
          result_available: false,
          export_count: 0,
          highlights_count: 0,
          finished_at: null,
        }),
        makeSummary({
          job_id: "job_done",
          video_id: "9000000001",
          state: "done",
          warning_count: 1,
        }),
        makeSummary({
          job_id: "job_int",
          video_id: "9000000009",
          state: "failed",
          title: null,
          broadcaster_id: null,
          broadcaster_display_name: null,
          duration_s: null,
          candidate_count: null,
          result_available: false,
          export_count: 0,
          highlights_count: 0,
          error: {
            code: "interrupted",
            message:
              "The server stopped while this analysis was running. Analyze the VOD again.",
            details: null,
          },
        }),
      ]),
    );

    render(<LibraryView />);

    expect(await screen.findByText("friday_vod.mp4")).toBeTruthy();
    expect(screen.getByText("Step 4 of 6")).toBeTruthy();
    expect(screen.getByText("Downloading audio")).toBeTruthy();
    expect(screen.getByText("50%")).toBeTruthy();
    expect(
      screen.getByRole("link", { name: "View progress" }).getAttribute("href"),
    ).toBe("/jobs/job_run");

    const list = screen.getByRole("list", { name: "Analysis jobs" });
    expect(screen.getByText("Synthetic stream 1")).toBeTruthy();
    expect(within(list).getByText("1 warning")).toBeTruthy();
    expect(
      within(list).getByText(
        (_, el) => el?.tagName === "SPAN" && el.textContent === "3 moments",
      ),
    ).toBeTruthy();
    expect(
      within(list).getByText(
        (_, el) =>
          el?.tagName === "SPAN" && el.textContent === "2 exported clips",
      ),
    ).toBeTruthy();
    expect(
      within(list).getByText(
        (_, el) =>
          el?.tagName === "SPAN" && el.textContent === "1 highlights video",
      ),
    ).toBeTruthy();
    expect(
      screen.getByRole("link", { name: "Open results" }).getAttribute("href"),
    ).toBe("/jobs/job_done/results");

    expect(screen.getByText("Interrupted")).toBeTruthy();
    expect(
      screen.getByText(
        "The server stopped while this analysis was running. Analyze the VOD again.",
      ),
    ).toBeTruthy();
    expect(
      screen.getByRole("link", { name: "View details" }).getAttribute("href"),
    ).toBe("/jobs/job_int");
  });

  it("polls only while jobs are running and stops once all jobs finish", async () => {
    vi.useFakeTimers();
    state.listJobs
      .mockResolvedValueOnce(
        page([
          makeSummary({
            job_id: "job_run",
            state: "running",
            progress: {
              stage: "fetching_clips",
              stage_index: 2,
              stage_count: 6,
              stage_fraction: null,
              message: "Fetching viewer clips",
            },
            title: null,
            broadcaster_display_name: null,
            candidate_count: null,
            result_available: false,
            export_count: 0,
            highlights_count: 0,
          }),
        ]),
      )
      .mockResolvedValueOnce(
        page([
          makeSummary({
            job_id: "job_run",
            state: "done",
          }),
        ]),
      );

    render(<LibraryView />);
    await act(async () => {
      await Promise.resolve();
    });
    expect(screen.getByText("Step 2 of 6")).toBeTruthy();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS);
    });
    const list = screen.getByRole("list", { name: "Analysis jobs" });
    expect(within(list).getByText("Done")).toBeTruthy();
    expect(state.listJobs).toHaveBeenCalledTimes(2);

    // No further polling once nothing is queued or running.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS * 5);
    });
    expect(state.listJobs).toHaveBeenCalledTimes(2);
  });

  it("filters by query and status, paginates with Load more, and does not poll when idle", async () => {
    state.listJobs
      .mockResolvedValueOnce(
        page([makeSummary({ job_id: "job_1", title: "Stream 1" })], {
          total: 2,
          hasMore: true,
        }),
      )
      .mockResolvedValueOnce(
        page([makeSummary({ job_id: "job_0", title: "Stream 0" })], {
          total: 2,
          hasMore: false,
        }),
      )
      .mockResolvedValueOnce(page([]));

    render(<LibraryView />);
    expect(await screen.findByText("Stream 1")).toBeTruthy();

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Load more" }));
    });
    expect(state.listJobs).toHaveBeenNthCalledWith(2, {
      limit: 20,
      offset: 1,
      q: undefined,
      state: undefined,
    });
    expect(screen.getByText("Stream 0")).toBeTruthy();

    fireEvent.change(
      screen.getByLabelText("Search by VOD title, channel, or VOD ID"),
      { target: { value: "missing" } },
    );
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Search" }));
    });

    expect(state.listJobs).toHaveBeenNthCalledWith(3, {
      limit: 20,
      offset: 0,
      q: "missing",
      state: undefined,
    });
    expect(screen.getByText("No matching analyses")).toBeTruthy();
  });

  it("confirms deletion, explains what is removed on disk, and supports delete_exports", async () => {
    state.listJobs.mockResolvedValueOnce(
      page([
        makeSummary({
          job_id: "job_del",
          video_path: "/videos/my_stream.mp4",
          export_count: 2,
          highlights_count: 1,
        }),
      ]),
    );
    state.deleteJob.mockResolvedValueOnce({
      job_id: "job_del",
      deleted: true,
      deleted_files: 3,
    });

    render(<LibraryView />);
    expect(await screen.findByText("Synthetic stream 1")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    const confirmRegion = screen.getByRole("region", {
      name: "Confirm deleting Synthetic stream 1",
    });
    expect(confirmRegion.textContent).toContain(
      "saved analysis result JSON on disk",
    );
    expect(confirmRegion.textContent).toContain(
      "Your original local video file (my_stream.mp4) is never deleted.",
    );
    const checkbox = within(confirmRegion).getByRole("checkbox");
    expect((checkbox as HTMLInputElement).checked).toBe(false);
    fireEvent.click(checkbox);

    await act(async () => {
      fireEvent.click(
        within(confirmRegion).getByRole("button", { name: "Confirm delete" }),
      );
    });

    expect(state.deleteJob).toHaveBeenCalledWith("job_del", {
      deleteExports: true,
    });
    expect(screen.getByText("No analyses yet")).toBeTruthy();
  });

  it("surfaces delete errors inline without removing the job", async () => {
    state.listJobs.mockResolvedValueOnce(
      page([
        makeSummary({
          job_id: "job_err",
          export_count: 0,
          highlights_count: 0,
        }),
      ]),
    );
    state.deleteJob.mockRejectedValueOnce(
      new ApiRequestError(500, {
        code: "delete_failed",
        message: "Couldn't remove result file.",
        details: null,
      }),
    );

    render(<LibraryView />);
    expect(await screen.findByText("Synthetic stream 1")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    const confirmRegion = screen.getByRole("region", {
      name: "Confirm deleting Synthetic stream 1",
    });
    await act(async () => {
      fireEvent.click(
        within(confirmRegion).getByRole("button", { name: "Confirm delete" }),
      );
    });

    expect(
      within(confirmRegion).getByText("Couldn't remove result file."),
    ).toBeTruthy();
    expect(screen.getByText("Synthetic stream 1")).toBeTruthy();
  });

  it("refreshes the active filter when filters change while deletion is pending", async () => {
    let resolveDelete:
      | ((val: {
          job_id: string;
          deleted: true;
          deleted_files: number;
        }) => void)
      | undefined;
    state.listJobs
      .mockResolvedValueOnce(
        page([
          makeSummary({
            job_id: "job_done",
            title: "Completed stream",
            state: "done",
          }),
        ]),
      )
      .mockResolvedValueOnce(
        page([
          makeSummary({
            job_id: "job_fail",
            title: "Failed stream",
            state: "failed",
          }),
        ]),
      )
      .mockResolvedValueOnce(
        page([
          makeSummary({
            job_id: "job_fail",
            title: "Failed stream",
            state: "failed",
          }),
        ]),
      );
    state.deleteJob.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveDelete = resolve;
        }),
    );

    render(<LibraryView />);
    expect(await screen.findByText("Completed stream")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    fireEvent.click(screen.getByRole("button", { name: "Confirm delete" }));

    // Switch filter to Failed while deleteJob is still pending.
    fireEvent.click(screen.getByRole("button", { name: "Failed" }));
    expect(await screen.findByText("Failed stream")).toBeTruthy();

    await act(async () => {
      resolveDelete?.({ job_id: "job_done", deleted: true, deleted_files: 0 });
    });

    expect(await screen.findByText("Failed stream")).toBeTruthy();
    expect(screen.queryByText("Completed stream")).toBeNull();
  });

  it("polls active rows beyond 100 loaded items and re-fetches offset pages invalidated by a poll", async () => {
    vi.useFakeTimers();
    const first100 = Array.from({ length: 100 }, (_, i) =>
      makeSummary({ job_id: `job_${i}`, title: `Stream ${i}` }),
    );
    const tailActive = makeSummary({
      job_id: "job_100",
      title: "Stream 100",
      state: "done",
      exports: [
        {
          render_id: "exp_active",
          kind: "export",
          candidate_id: "c001",
          state: "running",
          file_available: false,
          download_url: null,
          duration_s: 30,
          created_at: "2026-11-26T15:01:00Z",
          finished_at: null,
        },
      ],
    });
    const tailDone = makeSummary({
      ...tailActive,
      exports: [
        {
          ...tailActive.exports[0],
          state: "done",
          file_available: true,
          download_url: "/api/v1/exports/exp_active/file",
          finished_at: "2026-11-26T15:02:00Z",
        },
      ],
    });

    state.listJobs
      .mockResolvedValueOnce({
        items: first100,
        total: 101,
        limit: 20,
        offset: 0,
        has_more: true,
      })
      .mockResolvedValueOnce({
        items: [tailActive],
        total: 101,
        limit: 20,
        offset: 100,
        has_more: false,
      })
      .mockResolvedValueOnce({
        items: first100,
        total: 101,
        limit: 100,
        offset: 0,
        has_more: true,
      })
      .mockResolvedValueOnce({
        items: [tailDone],
        total: 101,
        limit: 1,
        offset: 100,
        has_more: false,
      });

    render(<LibraryView />);
    await act(async () => {
      await Promise.resolve();
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Load more" }));
    });
    expect(screen.getByText("Stream 100")).toBeTruthy();

    // Advance one poll interval: both pages (0..100 and 100..101) refresh, enabling Delete on job_100.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS);
    });
    expect(state.listJobs).toHaveBeenNthCalledWith(3, {
      limit: 100,
      offset: 0,
      q: undefined,
      state: undefined,
    });
    expect(state.listJobs).toHaveBeenNthCalledWith(4, {
      limit: 1,
      offset: 100,
      q: undefined,
      state: undefined,
    });
    const deleteButtons = screen.getAllByRole("button", { name: "Delete" });
    expect((deleteButtons[100] as HTMLButtonElement).disabled).toBe(false);
  });
});
