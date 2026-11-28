// @vitest-environment jsdom

// The progress screen's stage pacing (#188): every stage stays on screen for at least
// MIN_STAGE_MS, real progress shows once the stepper catches up, and reduced motion is honored.

import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { JobProgress } from "@/components/job-progress";
import exampleResult from "@/lib/api/fixtures/result-example.json";
import type { AnalysisResult, Job, Progress } from "@/lib/api/types";
import { MIN_STAGE_MS } from "@/lib/stage-pacing";

const state = vi.hoisted(() => ({
  getJob: vi.fn(),
  getResult: vi.fn(),
}));

vi.mock("next/navigation", () => ({
  useParams: () => ({ jobId: "job_1" }),
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

const NOW = Date.parse("2026-11-20T12:00:10Z");

const STAGES = [
  "resolving_vod",
  "fetching_clips",
  "downloading_chat",
  "downloading_audio",
  "computing_features",
  "ranking",
];

function stage(index: number, fraction: number | null = null): Progress {
  return {
    stage: STAGES[index - 1],
    stage_index: index,
    stage_count: 6,
    stage_fraction: fraction,
    message: "server message",
  };
}

function makeJob(overrides: Partial<Job> = {}): Job {
  return {
    job_id: "job_1",
    video_id: "9000000001",
    video_path: null,
    state: "running",
    progress: stage(1),
    warnings: [],
    error: null,
    created_at: "2026-11-20T12:00:00Z",
    started_at: "2026-11-20T12:00:01Z",
    updated_at: "2026-11-20T12:00:05Z",
    finished_at: null,
    ...overrides,
  };
}

const doneJob = (finishedAt: string) =>
  makeJob({
    state: "done",
    progress: null,
    finished_at: finishedAt,
  });

/** Let pending promises (the poll) settle. */
async function flush() {
  await act(async () => {
    await Promise.resolve();
  });
}

/**
 * Move the clock forward in small steps, each in its own `act`, so React commits and runs its
 * effects (which schedule the next stage) between steps, as a browser would.
 */
async function advance(ms: number) {
  for (let left = ms; left > 0; left -= 50) {
    await act(async () => {
      await vi.advanceTimersByTimeAsync(Math.min(50, left));
    });
  }
}

function heading() {
  return screen.getByRole("heading", { level: 1 }).textContent;
}

function number(label: string) {
  const term = screen.getByText(label, { selector: "dt" });
  return term.nextElementSibling?.textContent ?? null;
}

function mockReducedMotion(reduce: boolean) {
  window.matchMedia = vi.fn().mockImplementation((query: string) => ({
    matches: reduce && query.includes("reduce"),
    media: query,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  })) as unknown as typeof window.matchMedia;
}

describe("JobProgress stage pacing", () => {
  beforeEach(() => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "Date"] });
    vi.setSystemTime(NOW);
    state.getResult.mockResolvedValue(exampleResult as AnalysisResult);
  });

  afterEach(() => {
    cleanup();
    vi.useRealTimers();
    vi.clearAllMocks();
    // @ts-expect-error -- jsdom has no matchMedia; tests that need it install a fake.
    delete window.matchMedia;
  });

  it("walks a job that finished before the first poll through every stage", async () => {
    state.getJob.mockResolvedValue(doneJob("2026-11-20T12:00:09Z"));
    render(<JobProgress />);
    await flush();

    expect(heading()).toBe("Step 1 of 6: Looking up the VOD");
    expect(screen.queryByRole("link", { name: "See the clips" })).toBeNull();

    // Each stage stays for the minimum: not a millisecond less.
    await advance(MIN_STAGE_MS - 1);
    expect(heading()).toBe("Step 1 of 6: Looking up the VOD");
    await advance(1);
    expect(heading()).toBe("Step 2 of 6: Collecting viewer clips");

    await advance(MIN_STAGE_MS);
    expect(heading()).toBe("Step 3 of 6: Reading chat");
    // Chat isn't counted until its stage is done.
    expect(number("Chat messages read")).toContain("Not counted yet");

    await advance(MIN_STAGE_MS);
    expect(heading()).toBe("Step 4 of 6: Listening to the audio");
    expect(number("Chat messages read")).toBe("2,418");
    expect(number("Windows scored")).toContain("Not counted yet");

    await advance(MIN_STAGE_MS * 2);
    expect(heading()).toBe("Step 6 of 6: Scoring and picking the top moments");
    expect(screen.queryByRole("link", { name: "See the clips" })).toBeNull();

    await advance(MIN_STAGE_MS);
    expect(heading()).toBe("Analysis finished");
    expect(number("Windows scored")).toBe("36");
    expect(number("Moments found")).toBe("3");
    expect(
      screen.getByRole("link", { name: "See the clips" }).getAttribute("href"),
    ).toBe("/jobs/job_1/results");
    expect(screen.getByRole("status").textContent).toBe("Found 3 moments.");
  });

  it("announces each stage change through a polite live region", async () => {
    state.getJob.mockResolvedValue(doneJob("2026-11-20T12:00:09Z"));
    render(<JobProgress />);
    await flush();

    const live = screen.getByRole("heading", { level: 1 });
    expect(live.getAttribute("aria-live")).toBe("polite");
    await advance(MIN_STAGE_MS);
    expect(live.textContent).toBe("Step 2 of 6: Collecting viewer clips");
  });

  it("lets the user skip the replay of a finished job", async () => {
    state.getJob.mockResolvedValue(doneJob("2026-11-20T12:00:09Z"));
    render(<JobProgress />);
    await flush();

    fireEvent.click(
      screen.getByRole("button", { name: "Skip to the results" }),
    );
    expect(heading()).toBe("Analysis finished");
    expect(screen.getByRole("link", { name: "See the clips" })).toBeTruthy();
  });

  it("shows an old finished job at once", async () => {
    state.getJob.mockResolvedValue(doneJob("2026-11-20T11:00:00Z"));
    render(<JobProgress />);
    await flush();

    expect(heading()).toBe("Analysis finished");
    expect(number("Chat messages read")).toBe("2,418");
    expect(screen.getByRole("link", { name: "See the clips" })).toBeTruthy();
  });

  it("shows a failure at once, at the stage where it stopped", async () => {
    state.getJob.mockResolvedValue(
      makeJob({
        state: "failed",
        progress: stage(4),
        error: {
          code: "no_usable_signals",
          message: "Nothing to rank.",
          details: null,
        },
        finished_at: "2026-11-20T12:00:09Z",
      }),
    );
    render(<JobProgress />);
    await flush();

    expect(heading()).toBe("Analysis stopped");
    const steps = within(
      screen.getByRole("list", { name: "Stopped at step 4 of 6" }),
    ).getAllByRole("listitem");
    expect(steps[3].textContent).toContain("(stopped here)");
    expect(steps[2].textContent).toContain("(done)");
    expect(screen.queryByTestId("stage-visual")).toBeNull();
  });

  it("shows real progress once the stepper reaches the server's stage", async () => {
    state.getJob.mockResolvedValue(makeJob({ progress: stage(3, 0.5) }));
    render(<JobProgress />);
    await flush();

    // Catching up: the measured fraction belongs to stage 3, not the stage on screen.
    expect(heading()).toBe("Step 1 of 6: Looking up the VOD");
    expect(screen.queryByRole("progressbar")).toBeNull();
    await advance(MIN_STAGE_MS);
    expect(screen.queryByRole("progressbar")).toBeNull();
    await advance(MIN_STAGE_MS);
    expect(heading()).toBe("Step 3 of 6: Reading chat");
    expect(screen.getByRole("progressbar").getAttribute("aria-valuenow")).toBe(
      "50",
    );
  });

  it("doesn't hold back a server that is slower than the minimum", async () => {
    state.getJob
      .mockResolvedValueOnce(makeJob({ progress: stage(1) }))
      .mockResolvedValue(makeJob({ progress: stage(2, 0.25) }));
    render(<JobProgress />);
    await flush();
    expect(heading()).toBe("Step 1 of 6: Looking up the VOD");

    // The next poll (2 s later) reports stage 2; stage 1 has had its minimum, so it shows at once.
    await advance(2100);
    expect(heading()).toBe("Step 2 of 6: Collecting viewer clips");
    expect(screen.getByRole("progressbar").getAttribute("aria-valuenow")).toBe(
      "25",
    );
    // No numbers while running: they come with the result.
    expect(screen.queryByText("Chat messages read")).toBeNull();
  });

  it("animates the current stage when motion is allowed", async () => {
    mockReducedMotion(false);
    state.getJob.mockResolvedValue(makeJob({ progress: stage(4) }));
    render(<JobProgress />);
    await flush();

    const visual = screen.getByTestId("stage-visual");
    expect(visual.getAttribute("data-animated")).toBe("true");
    expect(visual.getAttribute("aria-hidden")).toBe("true");
    expect(visual.querySelector('[class*="stage-anim-"]')).not.toBeNull();
  });

  it("keeps the stage visual still when the user prefers reduced motion", async () => {
    mockReducedMotion(true);
    state.getJob.mockResolvedValue(doneJob("2026-11-20T12:00:09Z"));
    render(<JobProgress />);
    await flush();

    for (let i = 1; i <= 6; i++) {
      const visual = screen.getByTestId("stage-visual");
      expect(visual.getAttribute("data-animated")).toBe("false");
      expect(visual.querySelector('[class*="stage-anim-"]')).toBeNull();
      await advance(MIN_STAGE_MS);
    }
    // The stages still each get their moment on screen.
    expect(heading()).toBe("Analysis finished");
  });
});
