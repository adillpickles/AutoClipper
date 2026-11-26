// @vitest-environment jsdom

import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ChannelBrowser } from "@/components/channel-browser";
import { ApiRequestError, POLL_INTERVAL_MS } from "@/lib/api/client";
import type { ChannelVodsResponse, Job } from "@/lib/api/types";

const state = vi.hoisted(() => ({
  replace: vi.fn(),
  searchParams: new URLSearchParams(),
  getChannelVods: vi.fn(),
  createJob: vi.fn(),
  getJob: vi.fn(),
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: state.replace }),
  useSearchParams: () => state.searchParams,
}));

vi.mock("@/lib/api/client", async () => {
  const actual =
    await vi.importActual<typeof import("@/lib/api/client")>(
      "@/lib/api/client",
    );
  return {
    ...actual,
    getApiClient: () => ({
      getChannelVods: state.getChannelVods,
      createJob: state.createJob,
      getJob: state.getJob,
    }),
  };
});

const SAMPLE_RESPONSE: ChannelVodsResponse = {
  channel: {
    broadcaster_id: "9100000001",
    login: "user_001",
    display_name: "User_001",
    profile_image_url: null,
  },
  vods: [
    {
      video_id: "9000000003",
      title: "Synthetic stream 3",
      created_at: "2026-10-13T18:00:00Z",
      duration_s: 300,
      thumbnail_url: "https://example.invalid/thumb-320x180.jpg",
      url: "https://www.twitch.tv/videos/9000000003",
      view_count: 2890,
      analyzed: false,
      last_job_id: null,
    },
    {
      video_id: "9000000001",
      title: "Synthetic stream 1",
      created_at: "2026-10-12T18:00:00Z",
      duration_s: 360,
      thumbnail_url: null,
      url: "https://www.twitch.tv/videos/9000000001",
      view_count: 4820,
      analyzed: false,
      last_job_id: null,
    },
    {
      video_id: "9000000002",
      title: "Synthetic stream 2",
      created_at: "2026-10-12T18:00:00Z",
      duration_s: 360,
      thumbnail_url: null,
      url: "https://www.twitch.tv/videos/9000000002",
      view_count: 3410,
      analyzed: true,
      last_job_id: "job_existing",
    },
  ],
  truncated: false,
};

function makeJob(overrides: Partial<Job>): Job {
  return {
    job_id: "job_1",
    video_id: "9000000003",
    video_path: null,
    state: "queued",
    progress: null,
    warnings: [],
    error: null,
    created_at: "2026-10-19T15:00:00Z",
    started_at: null,
    updated_at: "2026-10-19T15:00:00Z",
    finished_at: null,
    ...overrides,
  };
}

describe("ChannelBrowser", () => {
  beforeEach(() => {
    window.sessionStorage.clear();
    state.searchParams = new URLSearchParams();
  });

  afterEach(() => {
    vi.useRealTimers();
    cleanup();
    vi.clearAllMocks();
  });

  it("shows the empty initial state before a channel is searched", () => {
    render(<ChannelBrowser />);
    expect(screen.getByText("No channel loaded yet")).toBeTruthy();
    expect(state.getChannelVods).not.toHaveBeenCalled();
  });

  it("validates a VOD link locally and offers a link back to single-VOD analysis", () => {
    render(<ChannelBrowser />);

    fireEvent.change(screen.getByLabelText("Twitch channel link or login"), {
      target: { value: "https://www.twitch.tv/videos/9000000001" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Load VODs" }));

    expect(screen.getByRole("alert").textContent).toMatch(/VOD link/);
    expect(
      screen
        .getByRole("link", { name: /Analyze a single VOD on the home page/ })
        .getAttribute("href"),
    ).toBe("/");
    expect(state.getChannelVods).not.toHaveBeenCalled();
  });

  it("shows loading state, renders VOD cards with metadata and already-analyzed badge, and bulk-queues selected VODs to completion", async () => {
    vi.useFakeTimers();
    let resolveLookup: ((value: ChannelVodsResponse) => void) | undefined;
    state.getChannelVods.mockImplementation(
      () =>
        new Promise<ChannelVodsResponse>((resolve) => {
          resolveLookup = resolve;
        }),
    );

    render(<ChannelBrowser />);

    fireEvent.click(screen.getByRole("button", { name: "Channel login" }));
    fireEvent.click(screen.getByRole("button", { name: "Load VODs" }));

    expect(
      screen.getByLabelText(/Loading past broadcasts for user_001/),
    ).toBeTruthy();

    await act(async () => {
      resolveLookup?.(SAMPLE_RESPONSE);
    });

    expect(screen.getByText("User_001")).toBeTruthy();
    expect(screen.getByText("Synthetic stream 3")).toBeTruthy();
    expect(screen.getByText("Already analyzed")).toBeTruthy();
    expect(screen.getByText("Oct 13, 2026 · 2,890 views")).toBeTruthy();
    expect(
      screen.getByRole("link", { name: /View results/ }).getAttribute("href"),
    ).toBe("/jobs/job_existing/results");

    // Select unanalyzed picks the 2 unanalyzed VODs, skipping 9000000002.
    fireEvent.click(screen.getByRole("button", { name: "Select unanalyzed" }));
    expect(screen.getByText("2 selected")).toBeTruthy();

    state.createJob
      .mockResolvedValueOnce({
        job: makeJob({
          job_id: "job_a",
          video_id: "9000000003",
          state: "queued",
        }),
        created: true,
      })
      .mockResolvedValueOnce({
        job: makeJob({
          job_id: "job_b",
          video_id: "9000000001",
          state: "queued",
        }),
        created: true,
      });

    await act(async () => {
      fireEvent.click(
        screen.getByRole("button", { name: "Analyze all selected (2)" }),
      );
    });

    expect(state.createJob).toHaveBeenCalledTimes(2);
    expect(state.createJob).toHaveBeenNthCalledWith(1, {
      vod: "https://www.twitch.tv/videos/9000000003",
      video_path: null,
    });
    expect(state.createJob).toHaveBeenNthCalledWith(2, {
      vod: "https://www.twitch.tv/videos/9000000001",
      video_path: null,
    });

    const queue = screen.getByRole("region", { name: "Analysis queue" });
    expect(within(queue).getByText(/2 VODs · 2 queued/)).toBeTruthy();

    // First poll: job_a is running with measured progress, job_b is still queued.
    state.getJob
      .mockResolvedValueOnce(
        makeJob({
          job_id: "job_a",
          video_id: "9000000003",
          state: "running",
          progress: {
            stage: "downloading_audio",
            stage_index: 4,
            stage_count: 6,
            stage_fraction: 0.5,
            message: "Downloading audio",
          },
        }),
      )
      .mockResolvedValueOnce(
        makeJob({
          job_id: "job_b",
          video_id: "9000000001",
          state: "queued",
        }),
      );

    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS);
    });

    expect(within(queue).getByText("Step 4 of 6")).toBeTruthy();
    expect(within(queue).getByText("50%")).toBeTruthy();

    // Second poll: job_a finishes done, job_b fails.
    state.getJob
      .mockResolvedValueOnce(
        makeJob({
          job_id: "job_a",
          video_id: "9000000003",
          state: "done",
          finished_at: "2026-10-19T15:02:00Z",
        }),
      )
      .mockResolvedValueOnce(
        makeJob({
          job_id: "job_b",
          video_id: "9000000001",
          state: "failed",
          error: {
            code: "vod_unavailable",
            message: "This VOD isn't available on Twitch.",
            details: null,
          },
          finished_at: "2026-10-19T15:02:05Z",
        }),
      );

    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS);
    });

    expect(within(queue).getByText(/1 done · 1 failed/)).toBeTruthy();
    expect(
      within(queue)
        .getByRole("link", { name: "View results" })
        .getAttribute("href"),
    ).toBe("/jobs/job_a/results");
    expect(
      within(queue).getByText("This VOD isn't available on Twitch."),
    ).toBeTruthy();

    // Clear finished removes terminal jobs.
    fireEvent.click(
      within(queue).getByRole("button", { name: "Clear finished" }),
    );
    expect(screen.queryByRole("region", { name: "Analysis queue" })).toBeNull();
  });

  it("renders the no-credentials state with setup guidance and retry", async () => {
    state.getChannelVods.mockRejectedValueOnce(
      new ApiRequestError(503, {
        code: "twitch_credentials_missing",
        message: "Twitch API credentials aren't configured on this server.",
        details: { missing: ["TWITCH_CLIENT_ID", "TWITCH_CLIENT_SECRET"] },
      }),
    );

    render(<ChannelBrowser />);
    fireEvent.change(screen.getByLabelText("Twitch channel link or login"), {
      target: { value: "no_credentials" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Load VODs" }));

    expect(
      await screen.findByText("Twitch API credentials needed"),
    ).toBeTruthy();
    expect(
      screen.getByText(/TWITCH_CLIENT_ID, TWITCH_CLIENT_SECRET/),
    ).toBeTruthy();

    state.getChannelVods.mockResolvedValueOnce(SAMPLE_RESPONSE);
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByText("User_001")).toBeTruthy();
  });

  it("renders unknown-channel, empty-channel, and upstream error states", async () => {
    state.getChannelVods.mockRejectedValueOnce(
      new ApiRequestError(404, {
        code: "channel_not_found",
        message: "No Twitch channel with that name was found.",
        details: null,
      }),
    );

    render(<ChannelBrowser />);
    const input = screen.getByLabelText("Twitch channel link or login");
    fireEvent.change(input, { target: { value: "unknown_channel" } });
    fireEvent.click(screen.getByRole("button", { name: "Load VODs" }));
    expect(await screen.findByText("Channel not found")).toBeTruthy();

    state.getChannelVods.mockResolvedValueOnce({
      channel: {
        broadcaster_id: "9100000099",
        login: "empty_channel",
        display_name: "Empty_Channel",
        profile_image_url: null,
      },
      vods: [],
      truncated: false,
    });
    fireEvent.change(input, { target: { value: "empty_channel" } });
    fireEvent.click(screen.getByRole("button", { name: "Load VODs" }));
    expect(
      await screen.findByText("No past broadcasts found for Empty_Channel"),
    ).toBeTruthy();

    state.getChannelVods.mockRejectedValueOnce(
      new ApiRequestError(502, {
        code: "upstream_unavailable",
        message: "Twitch isn't responding right now.",
        details: null,
      }),
    );
    fireEvent.change(input, { target: { value: "twitch_error" } });
    fireEvent.click(screen.getByRole("button", { name: "Load VODs" }));
    expect(await screen.findByText("Couldn't load channel VODs")).toBeTruthy();
    expect(screen.getByText("Twitch isn't responding right now.")).toBeTruthy();
  });

  it("auto-loads a channel when navigated to with ?channel= in the URL", async () => {
    state.searchParams = new URLSearchParams("channel=user_001");
    state.getChannelVods.mockResolvedValueOnce(SAMPLE_RESPONSE);

    render(<ChannelBrowser />);

    expect(await screen.findByText("User_001")).toBeTruthy();
    expect(state.getChannelVods).toHaveBeenCalledWith("user_001");
  });

  it("performs only one lookup when submitting a URL updates search params", async () => {
    state.getChannelVods.mockResolvedValue(SAMPLE_RESPONSE);
    state.replace.mockImplementation((url: string) => {
      state.searchParams = new URL(url, "http://localhost").searchParams;
    });

    const { rerender } = render(<ChannelBrowser />);
    fireEvent.click(screen.getByRole("button", { name: "Channel link" }));
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Load VODs" }));
    });
    rerender(<ChannelBrowser />);

    expect(state.getChannelVods).toHaveBeenCalledTimes(1);
  });

  it("persists accepted jobs immediately and keeps failed VODs selected for targeted retry", async () => {
    state.getChannelVods.mockResolvedValueOnce(SAMPLE_RESPONSE);
    render(<ChannelBrowser />);

    fireEvent.click(screen.getByRole("button", { name: "Channel login" }));
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Load VODs" }));
    });

    fireEvent.click(screen.getByRole("button", { name: "Select unanalyzed" }));
    let rejectSecond: ((err: Error) => void) | undefined;
    state.createJob
      .mockResolvedValueOnce({
        job: makeJob({
          job_id: "job_ok",
          video_id: "9000000003",
          state: "queued",
        }),
        created: true,
      })
      .mockImplementationOnce(
        () =>
          new Promise((_, reject) => {
            rejectSecond = reject;
          }),
      );

    await act(async () => {
      fireEvent.click(
        screen.getByRole("button", { name: "Analyze all selected (2)" }),
      );
      await Promise.resolve();
    });

    // First job is already persisted in sessionStorage before the second POST settles.
    expect(
      window.sessionStorage.getItem("autoclipper.channelQueue.v1"),
    ).toContain("job_ok");

    await act(async () => {
      rejectSecond?.(new Error("temporary failure"));
      await Promise.resolve();
    });

    expect(screen.getByRole("alert").textContent).toMatch(/Synthetic stream 1/);
    // Only the failed VOD remains selected so retrying doesn't re-run the accepted one.
    expect(screen.getByText("1 selected")).toBeTruthy();
    expect(
      (screen.getByLabelText("Select Synthetic stream 1") as HTMLInputElement)
        .checked,
    ).toBe(true);
    expect(
      (screen.getByLabelText("Select Synthetic stream 3") as HTMLInputElement)
        .checked,
    ).toBe(false);
  });

  it("revalidates restored done jobs, prefers server last_job_id, and displays all warnings while running", async () => {
    window.sessionStorage.setItem(
      "autoclipper.channelQueue.v1",
      JSON.stringify([
        {
          jobId: "job_stale",
          videoId: "9000000002",
          title: "Synthetic stream 2",
          channelDisplayName: "User_001",
          durationS: 360,
          job: makeJob({
            job_id: "job_stale",
            video_id: "9000000002",
            state: "done",
          }),
          missing: false,
          pollError: null,
        },
        {
          jobId: "job_running",
          videoId: "9000000003",
          title: "Synthetic stream 3",
          channelDisplayName: "User_001",
          durationS: 300,
          job: makeJob({
            job_id: "job_running",
            video_id: "9000000003",
            state: "running",
            progress: {
              stage: "downloading_audio",
              stage_index: 4,
              stage_count: 6,
              stage_fraction: null,
              message: "Downloading audio",
            },
            warnings: [
              { code: "chat_missing", message: "Chat replay missing." },
              {
                code: "viewer_clips_missing",
                message: "Viewer clips missing.",
              },
            ],
          }),
          missing: false,
          pollError: null,
        },
      ]),
    );
    state.searchParams = new URLSearchParams("channel=user_001");
    state.getChannelVods.mockResolvedValueOnce(SAMPLE_RESPONSE);
    state.getJob.mockRejectedValueOnce(
      new ApiRequestError(404, {
        code: "job_not_found",
        message: "Not found",
        details: null,
      }),
    );

    render(<ChannelBrowser />);

    expect(await screen.findByText("Expired")).toBeTruthy();
    expect(screen.getByText("Chat replay missing.")).toBeTruthy();
    expect(screen.getByText("Viewer clips missing.")).toBeTruthy();
    // Card for 9000000002 links to the backend's current `last_job_id`, not `job_stale`.
    expect(
      screen.getByRole("link", { name: /View results/ }).getAttribute("href"),
    ).toBe("/jobs/job_existing/results");
  });

  it("backs off on poll failures and shows an offline retry banner after a minute", async () => {
    vi.useFakeTimers();
    window.sessionStorage.setItem(
      "autoclipper.channelQueue.v1",
      JSON.stringify([
        {
          jobId: "job_net",
          videoId: "9000000001",
          title: "Synthetic stream 1",
          channelDisplayName: "User_001",
          durationS: 360,
          job: makeJob({
            job_id: "job_net",
            video_id: "9000000001",
            state: "running",
          }),
          missing: false,
          pollError: null,
        },
      ]),
    );
    state.getJob.mockRejectedValue(new TypeError("Failed to fetch"));

    render(<ChannelBrowser />);

    // Advance past the 60 s failure threshold with exponential backoff (2, 4, 8, 10, 10, ...).
    await act(async () => {
      await vi.advanceTimersByTimeAsync(75_000);
    });

    expect(
      screen.getByText(/Can't reach the server to refresh job progress/),
    ).toBeTruthy();

    state.getJob.mockResolvedValueOnce(
      makeJob({
        job_id: "job_net",
        video_id: "9000000001",
        state: "done",
      }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS);
    });

    expect(
      screen.queryByText(/Can't reach the server to refresh job progress/),
    ).toBeNull();
    expect(
      screen.getByRole("link", { name: "View results" }).getAttribute("href"),
    ).toBe("/jobs/job_net/results");
  });
});
