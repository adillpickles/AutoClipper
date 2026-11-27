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
import type {
  DeleteJobResponse,
  JobListResponse,
  JobSummary,
} from "@/lib/api/types";

const state = vi.hoisted(() => ({
  listJobs: vi.fn(),
  getJobStatuses: vi.fn(),
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
      getJobStatuses: state.getJobStatuses,
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
  { total = items.length, nextCursor = null as string | null, limit = 20 } = {},
): JobListResponse {
  return {
    items,
    total,
    limit,
    offset: 0,
    has_more: nextCursor !== null,
    next_cursor: nextCursor,
  };
}

function history(count: number) {
  return Array.from({ length: count }, (_, index) =>
    makeSummary({ job_id: `job_${index + 1}`, title: `Stream ${index + 1}` }),
  );
}

function finishDelete(jobId: string): DeleteJobResponse {
  return { job_id: jobId, deleted: true, deleted_files: 0 };
}

function openDelete(title: string) {
  const row = screen.getByText(title).closest("li") as HTMLLIElement;
  fireEvent.click(within(row).getByRole("button", { name: "Delete" }));
  return within(
    screen.getByRole("region", { name: `Confirm deleting ${title}` }),
  );
}

async function tickPoll() {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS);
  });
}

describe("LibraryView", () => {
  afterEach(() => {
    vi.useRealTimers();
    cleanup();
    vi.resetAllMocks();
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
    state.listJobs.mockResolvedValueOnce(
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
    );
    state.getJobStatuses.mockResolvedValueOnce([
      makeSummary({ job_id: "job_run", state: "done" }),
    ]);

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
    expect(state.getJobStatuses).toHaveBeenCalledExactlyOnceWith(["job_run"]);

    // No further polling once nothing is queued or running.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS * 5);
    });
    expect(state.getJobStatuses).toHaveBeenCalledTimes(1);
    expect(state.listJobs).toHaveBeenCalledTimes(1);
  });

  it("filters by query and paginates with Load more when idle", async () => {
    const jobs = history(21);
    state.listJobs
      .mockResolvedValueOnce(
        page(jobs.slice(0, 20), {
          total: 21,
          nextCursor: "after-first",
        }),
      )
      .mockResolvedValueOnce(page(jobs.slice(20), { total: 21 }))
      .mockResolvedValueOnce(page([]));

    render(<LibraryView />);
    expect(await screen.findByText("Stream 1")).toBeTruthy();

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Load more" }));
    });
    expect(state.listJobs).toHaveBeenNthCalledWith(2, {
      limit: 20,
      before: "after-first",
      q: undefined,
      state: undefined,
    });
    expect(screen.getByText("Stream 21")).toBeTruthy();

    fireEvent.change(
      screen.getByLabelText("Search by VOD title, channel, or VOD ID"),
      { target: { value: "missing" } },
    );
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Search" }));
    });

    expect(state.listJobs).toHaveBeenNthCalledWith(3, {
      limit: 20,
      before: undefined,
      q: "missing",
      state: undefined,
    });
    expect(screen.getByText("No matching analyses")).toBeTruthy();
    expect(state.getJobStatuses).not.toHaveBeenCalled();
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

  it.each(["poll first", "page first"])(
    "keeps all 40 rows when Load more overlaps a poll (%s)",
    async (order) => {
      vi.useFakeTimers();
      const [finished, ...older] = history(40);
      const running = { ...finished, state: "running" as const };
      const total = 40;
      const nextPage = Promise.withResolvers<JobListResponse>();
      const poll = Promise.withResolvers<JobSummary[]>();
      state.listJobs
        .mockResolvedValueOnce(
          page([running, ...older.slice(0, 19)], {
            total,
            nextCursor: "after-20",
          }),
        )
        .mockReturnValueOnce(nextPage.promise);
      state.getJobStatuses.mockReturnValueOnce(poll.promise);
      state.getJobStatuses.mockResolvedValueOnce([finished]);

      await act(async () => {
        render(<LibraryView />);
      });
      await tickPoll();
      expect(state.getJobStatuses).toHaveBeenCalledExactlyOnceWith([
        running.job_id,
      ]);
      fireEvent.click(screen.getByRole("button", { name: "Load more" }));
      expect(state.listJobs).toHaveBeenLastCalledWith(
        expect.objectContaining({ limit: 20, before: "after-20" }),
      );

      if (order === "poll first") {
        await act(async () => poll.resolve([finished]));
        await act(async () =>
          nextPage.resolve(page(older.slice(19), { total })),
        );
      } else {
        await act(async () =>
          nextPage.resolve(page(older.slice(19), { total })),
        );
        await act(async () => poll.resolve([finished]));
        expect(
          screen.getByRole("link", { name: "View progress" }),
        ).toBeTruthy();
        await tickPoll();
        expect(state.getJobStatuses).toHaveBeenCalledTimes(2);
      }

      expect(screen.getAllByRole("listitem")).toHaveLength(40);
      expect(
        screen.getAllByRole("link", { name: "Open results" }),
      ).toHaveLength(40);
      expect(state.listJobs).toHaveBeenCalledTimes(2);
    },
  );

  it("reconciles a cross-tab deletion when loading the rest of the history", async () => {
    const original = history(25);
    state.listJobs
      .mockResolvedValueOnce(
        page(original.slice(0, 20), { total: 25, nextCursor: "after-20" }),
      )
      .mockResolvedValueOnce(page(original.slice(20), { total: 24 }))
      .mockResolvedValueOnce(page(original.slice(1), { total: 24, limit: 40 }));

    render(<LibraryView />);
    expect(await screen.findByText("Stream 1")).toBeTruthy();
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Load more" }));
    });

    expect(state.listJobs).toHaveBeenNthCalledWith(
      2,
      expect.objectContaining({ before: "after-20", limit: 20 }),
    );
    expect(state.listJobs).toHaveBeenNthCalledWith(
      3,
      expect.objectContaining({ before: undefined, limit: 40 }),
    );
    expect(screen.queryByText("Stream 1")).toBeNull();
    expect(screen.getByText("Stream 21")).toBeTruthy();
    expect(screen.getAllByRole("listitem")).toHaveLength(24);
  });

  it("does not restore a deleted row from an older poll's history refresh", async () => {
    vi.useFakeTimers();
    const [removed, finished, missing] = history(3);
    const running = { ...finished, state: "running" as const };
    const queued = { ...missing, state: "queued" as const };
    const oldHistory = Promise.withResolvers<JobListResponse>();
    state.listJobs
      .mockResolvedValueOnce(page([removed, running, queued]))
      .mockReturnValueOnce(oldHistory.promise)
      .mockResolvedValueOnce(page([finished]));
    state.getJobStatuses
      .mockResolvedValueOnce([running])
      .mockResolvedValueOnce([finished]);
    state.deleteJob.mockResolvedValueOnce(finishDelete(removed.job_id));

    await act(async () => {
      render(<LibraryView />);
    });
    await tickPoll();
    expect(state.listJobs).toHaveBeenCalledTimes(2);
    const confirmation = openDelete("Stream 1");
    await act(async () => {
      fireEvent.click(
        confirmation.getByRole("button", { name: "Confirm delete" }),
      );
    });
    expect(screen.queryByText("Stream 1")).toBeNull();

    await act(async () => oldHistory.resolve(page([removed, finished])));
    expect(screen.queryByText("Stream 1")).toBeNull();
    await tickPoll();

    expect(state.getJobStatuses).toHaveBeenNthCalledWith(2, [
      running.job_id,
      queued.job_id,
    ]);
    expect(screen.getAllByRole("listitem")).toHaveLength(1);
    expect(screen.getByRole("link", { name: "Open results" })).toBeTruthy();
    await tickPoll();
    expect(state.getJobStatuses).toHaveBeenCalledTimes(2);
  });

  it("restarts polling the Running filter after a pending deletion triggers a refresh", async () => {
    vi.useFakeTimers();
    const [removed, finished] = history(2);
    const running = { ...finished, state: "running" as const };
    const deletion = Promise.withResolvers<DeleteJobResponse>();
    const stalePoll = Promise.withResolvers<JobSummary[]>();
    const refresh = Promise.withResolvers<JobListResponse>();
    state.listJobs
      .mockResolvedValueOnce(page([removed, running]))
      .mockResolvedValueOnce(page([running]))
      .mockReturnValueOnce(refresh.promise)
      .mockResolvedValueOnce(page([]));
    state.deleteJob.mockReturnValueOnce(deletion.promise);
    state.getJobStatuses
      .mockReturnValueOnce(stalePoll.promise)
      .mockResolvedValueOnce([finished]);

    await act(async () => {
      render(<LibraryView />);
    });
    const confirmation = openDelete("Stream 1");
    fireEvent.click(
      confirmation.getByRole("button", { name: "Confirm delete" }),
    );
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Running" }));
    });
    await tickPoll();
    expect(state.getJobStatuses).toHaveBeenCalledExactlyOnceWith([
      running.job_id,
    ]);

    await act(async () => deletion.resolve(finishDelete(removed.job_id)));
    expect(state.listJobs).toHaveBeenLastCalledWith(
      expect.objectContaining({ state: "running" }),
    );
    await act(async () => stalePoll.resolve([finished]));
    expect(screen.getByRole("link", { name: "View progress" })).toBeTruthy();
    await act(async () => refresh.resolve(page([running])));
    await tickPoll();

    expect(state.getJobStatuses).toHaveBeenNthCalledWith(2, [running.job_id]);
    expect(screen.getByText("No matching analyses")).toBeTruthy();
    await tickPoll();
    expect(state.getJobStatuses).toHaveBeenCalledTimes(2);
  });

  it("shows the second delete failure after the first overlapping delete succeeds", async () => {
    const first = Promise.withResolvers<DeleteJobResponse>();
    const second = Promise.withResolvers<DeleteJobResponse>();
    state.listJobs.mockResolvedValueOnce(page(history(2)));
    state.deleteJob
      .mockReturnValueOnce(first.promise)
      .mockReturnValueOnce(second.promise);

    render(<LibraryView />);
    expect(await screen.findByText("Stream 1")).toBeTruthy();
    const firstConfirm = openDelete("Stream 1");
    fireEvent.click(
      firstConfirm.getByRole("button", { name: "Confirm delete" }),
    );
    const secondConfirm = openDelete("Stream 2");
    fireEvent.click(secondConfirm.getByRole("checkbox"));
    fireEvent.click(
      secondConfirm.getByRole("button", { name: "Confirm delete" }),
    );
    expect(state.deleteJob).toHaveBeenNthCalledWith(1, "job_1", {
      deleteExports: false,
    });
    expect(state.deleteJob).toHaveBeenNthCalledWith(2, "job_2", {
      deleteExports: true,
    });

    await act(async () => first.resolve(finishDelete("job_1")));
    await act(async () => {
      second.reject(
        new ApiRequestError(500, {
          code: "delete_failed",
          message: "The rendered file is in use.",
          details: null,
        }),
      );
    });

    expect(screen.queryByText("Stream 1")).toBeNull();
    expect(secondConfirm.getByRole("alert").textContent).toBe(
      "The rendered file is in use.",
    );
    expect(
      secondConfirm.getByRole("button", { name: "Confirm delete" }),
    ).toHaveProperty("disabled", false);
  });

  it("polls one batch for active rows beyond 100 loaded jobs", async () => {
    vi.useFakeTimers();
    const jobs = history(102);
    const renderActive = {
      ...jobs[100],
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
    } satisfies JobSummary;
    const analysisActive = { ...jobs[101], state: "running" as const };
    for (let offset = 0; offset < 100; offset += 20) {
      state.listJobs.mockResolvedValueOnce(
        page(jobs.slice(offset, offset + 20), {
          total: 102,
          nextCursor: `after-${offset + 20}`,
        }),
      );
    }
    state.listJobs.mockResolvedValueOnce(
      page([renderActive, analysisActive], { total: 102 }),
    );
    state.getJobStatuses.mockResolvedValueOnce([jobs[100], jobs[101]]);

    await act(async () => {
      render(<LibraryView />);
    });
    for (let offset = 20; offset <= 100; offset += 20) {
      await act(async () => {
        fireEvent.click(screen.getByRole("button", { name: "Load more" }));
      });
      expect(state.listJobs).toHaveBeenLastCalledWith(
        expect.objectContaining({ before: `after-${offset}`, limit: 20 }),
      );
    }
    expect(screen.getByRole("link", { name: "View progress" })).toBeTruthy();
    await tickPoll();

    expect(state.getJobStatuses).toHaveBeenCalledExactlyOnceWith([
      "job_101",
      "job_102",
    ]);
    expect(state.listJobs).toHaveBeenCalledTimes(6);
    expect(screen.getAllByRole("listitem")).toHaveLength(102);
    expect(screen.queryByRole("link", { name: "View progress" })).toBeNull();
    const row = screen.getByText("Stream 101").closest("li") as HTMLLIElement;
    expect(within(row).getByRole("button", { name: "Delete" })).toHaveProperty(
      "disabled",
      false,
    );
  });
});
