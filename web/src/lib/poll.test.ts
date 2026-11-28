// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { sameClipExport } from "@/lib/clip-export";
import type { Export } from "@/lib/api/types";
import { HIDDEN_POLL_INTERVAL_MS, schedulePoll, waitForPoll } from "@/lib/poll";

function setVisibility(state: "visible" | "hidden") {
  Object.defineProperty(document, "visibilityState", {
    configurable: true,
    get: () => state,
  });
  document.dispatchEvent(new Event("visibilitychange"));
}

describe("schedulePoll", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    setVisibility("visible");
  });
  afterEach(() => {
    vi.useRealTimers();
    setVisibility("visible");
  });

  it("waits the given delay while the page is visible", () => {
    const callback = vi.fn();
    schedulePoll(callback, 2000);
    vi.advanceTimersByTime(1999);
    expect(callback).not.toHaveBeenCalled();
    vi.advanceTimersByTime(1);
    expect(callback).toHaveBeenCalledTimes(1);
  });

  it("slows to the hidden interval in a background tab", () => {
    setVisibility("hidden");
    const callback = vi.fn();
    schedulePoll(callback, 2000);
    vi.advanceTimersByTime(HIDDEN_POLL_INTERVAL_MS - 1);
    expect(callback).not.toHaveBeenCalled();
    vi.advanceTimersByTime(1);
    expect(callback).toHaveBeenCalledTimes(1);
  });

  it("keeps a longer backoff while hidden", () => {
    setVisibility("hidden");
    const callback = vi.fn();
    schedulePoll(callback, HIDDEN_POLL_INTERVAL_MS + 5000);
    vi.advanceTimersByTime(HIDDEN_POLL_INTERVAL_MS + 4999);
    expect(callback).not.toHaveBeenCalled();
    vi.advanceTimersByTime(1);
    expect(callback).toHaveBeenCalledTimes(1);
  });

  it("polls at once when a hidden page is shown again, and only once", () => {
    setVisibility("hidden");
    const callback = vi.fn();
    schedulePoll(callback, 2000);
    vi.advanceTimersByTime(3000);
    expect(callback).not.toHaveBeenCalled();
    setVisibility("visible");
    expect(callback).toHaveBeenCalledTimes(1);
    vi.advanceTimersByTime(HIDDEN_POLL_INTERVAL_MS);
    setVisibility("hidden");
    setVisibility("visible");
    expect(callback).toHaveBeenCalledTimes(1);
  });

  it("never runs once cancelled", () => {
    setVisibility("hidden");
    const callback = vi.fn();
    const cancel = schedulePoll(callback, 2000);
    cancel();
    setVisibility("visible");
    vi.advanceTimersByTime(HIDDEN_POLL_INTERVAL_MS);
    expect(callback).not.toHaveBeenCalled();
  });

  it("waitForPoll resolves after the delay and hands out its cancel function", async () => {
    const cancels: (() => void)[] = [];
    const done = vi.fn();
    void waitForPoll(500, (cancel) => cancels.push(cancel)).then(done);
    expect(cancels).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(500);
    expect(done).toHaveBeenCalledTimes(1);
  });
});

describe("sameClipExport", () => {
  const exp = (overrides: Partial<Export> = {}): Export => ({
    export_id: "exp_1",
    job_id: "job_1",
    candidate_id: "c001",
    video_id: "1",
    start_s: 0,
    end_s: 20,
    layout: "center_crop",
    captions: true,
    state: "running",
    progress: null,
    error: null,
    download_url: null,
    created_at: "2026-11-20T12:01:00Z",
    started_at: null,
    updated_at: "2026-11-20T12:01:00Z",
    finished_at: null,
    ...overrides,
  });

  it("treats a fresh copy of the same export as unchanged", () => {
    expect(
      sameClipExport(
        { kind: "export", export: exp(), url: null },
        { kind: "export", export: exp(), url: null },
      ),
    ).toBe(true);
  });

  it("sees a changed state, progress or url", () => {
    const current = { kind: "export" as const, export: exp(), url: null };
    expect(
      sameClipExport(current, {
        kind: "export",
        export: exp({ state: "done" }),
        url: null,
      }),
    ).toBe(false);
    expect(
      sameClipExport(current, { kind: "export", export: exp(), url: "/a.mp4" }),
    ).toBe(false);
    expect(sameClipExport(undefined, { kind: "pending" })).toBe(false);
    expect(
      sameClipExport(
        { kind: "error", message: "a" },
        { kind: "error", message: "b" },
      ),
    ).toBe(false);
    expect(
      sameClipExport({ kind: "pending" }, { kind: "error", message: "a" }),
    ).toBe(false);
  });
});
