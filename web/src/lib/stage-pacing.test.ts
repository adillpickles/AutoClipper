import { describe, expect, it } from "vitest";

import type { Job } from "@/lib/api/types";
import {
  CLOCK_TOLERANCE_MS,
  REPLAY_WINDOW_MS,
  shouldPace,
  targetStep,
} from "@/lib/stage-pacing";

function job(overrides: Partial<Job>): Job {
  return {
    job_id: "job_1",
    video_id: "9000000001",
    video_path: null,
    state: "running",
    progress: null,
    warnings: [],
    error: null,
    created_at: "2026-11-20T12:00:00Z",
    started_at: null,
    updated_at: "2026-11-20T12:00:00Z",
    finished_at: null,
    ...overrides,
  };
}

const progress = (index: number) => ({
  stage: "x",
  stage_index: index,
  stage_count: 6,
  stage_fraction: null,
  message: "x",
});

describe("targetStep", () => {
  it("maps each state to a step of the six-stage list", () => {
    expect(targetStep(job({ state: "queued" }), 6)).toBe(0);
    expect(targetStep(job({ progress: progress(4) }), 6)).toBe(4);
    expect(targetStep(job({ state: "running" }), 6)).toBe(0);
    expect(targetStep(job({ state: "failed", progress: progress(1) }), 6)).toBe(
      1,
    );
    expect(targetStep(job({ state: "done" }), 6)).toBe(7);
  });
});

describe("shouldPace", () => {
  const finished = "2026-11-20T12:00:10Z";
  const finishedMs = Date.parse(finished);

  it("paces jobs still in progress", () => {
    expect(shouldPace(job({ state: "queued" }), 0)).toBe(true);
    expect(shouldPace(job({ state: "running" }), 0)).toBe(true);
  });

  it("replays only a job that just finished", () => {
    const done = job({ state: "done", finished_at: finished });
    expect(shouldPace(done, finishedMs + 1000)).toBe(true);
    expect(shouldPace(done, finishedMs + REPLAY_WINDOW_MS)).toBe(false);
  });

  it("doesn't trust a finish time far in the future (clock skew)", () => {
    const done = job({ state: "done", finished_at: finished });
    // Whole-second timestamps can round a little ahead of the browser clock.
    expect(shouldPace(done, finishedMs - CLOCK_TOLERANCE_MS)).toBe(true);
    // A browser clock minutes behind the server would otherwise replay old jobs.
    expect(shouldPace(done, finishedMs - CLOCK_TOLERANCE_MS - 1)).toBe(false);
    expect(shouldPace(done, finishedMs - 10 * 60_000)).toBe(false);
  });

  it("never paces a failure", () => {
    expect(
      shouldPace(job({ state: "failed", finished_at: finished }), finishedMs),
    ).toBe(false);
  });
});
