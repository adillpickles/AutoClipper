// Paces the progress screen's stepper (#188). A cached VOD can pass all six analysis stages
// between two polls, so the screen would jump from "Waiting" to "Finished". Instead the stepper
// walks forward one stage at a time and keeps each stage on screen for at least
// `MIN_STAGE_MS`. Real progress is never held back longer than that: once a stage has been
// shown for the minimum, the next real stage appears as soon as the server reports it.

import { useCallback, useEffect, useRef, useState } from "react";

import type { Job } from "@/lib/api/types";

/** The shortest time one stage stays on screen while the stepper catches up. */
export const MIN_STAGE_MS = 1000;

/**
 * A job first seen `done` is replayed only if it finished this recently, so a cached VOD that
 * finished before the first poll still shows its stages, but reopening an old job doesn't.
 */
export const REPLAY_WINDOW_MS = 15_000;

/**
 * The step the server says the job is at: 0 while queued, the 1-based stage index while
 * running or failed, and `stageCount + 1` once done (every stage finished).
 */
export function targetStep(job: Job, stageCount: number): number {
  switch (job.state) {
    case "queued":
      return 0;
    case "done":
      return stageCount + 1;
    case "running":
    case "failed":
      return job.progress?.stage_index ?? 0;
  }
}

/**
 * Whether the stepper should walk through the stages for a job seen for the first time at
 * `nowMs`: always for a job still in progress, for a `done` job only if it just finished, and
 * never for a failed one (the failure is shown at once).
 */
export function shouldPace(job: Job, nowMs: number): boolean {
  if (job.state === "queued" || job.state === "running") return true;
  if (job.state === "failed" || job.finished_at === null) return false;
  const finished = Date.parse(job.finished_at);
  if (Number.isNaN(finished)) return false;
  // The job carries no server "now" to compare with (a done job's `updated_at` is its
  // `finished_at`), so the age uses the browser clock. A finish time in the future means the
  // clocks disagree; beyond a little rounding, the age is unknown, so the job isn't replayed.
  const age = nowMs - finished;
  return age >= -CLOCK_TOLERANCE_MS && age < REPLAY_WINDOW_MS;
}

/** How far in the future `finished_at` may be and still count as "just now" (rounding). */
export const CLOCK_TOLERANCE_MS = 2000;

export interface PacedStep {
  /** The step to show: never ahead of `target`. */
  shown: number;
  /** Whether the shown step is still behind the server's. */
  catchingUp: boolean;
  /** Jump straight to the server's step. */
  skip: () => void;
}

/**
 * The step to show for a server step `target`. While `pace` is true it advances one step at a
 * time, each stage staying on screen for at least `minMs`; leaving the "queued" step 0 is
 * immediate. With `pace` false (or after `skip`) it follows `target` exactly.
 */
export function usePacedStep(
  target: number,
  { pace, minMs = MIN_STAGE_MS }: { pace: boolean; minMs?: number },
): PacedStep {
  const [shown, setShown] = useState(() =>
    pace ? Math.min(target, 1) : target,
  );
  const [skipped, setSkipped] = useState(false);
  // When the shown step appeared; null until the effect first records it.
  const shownSince = useRef<number | null>(null);
  const paced = pace && !skipped;

  useEffect(() => {
    if (!paced) return;
    shownSince.current ??= Date.now();
    if (shown >= target) return;
    const wait =
      shown < 1 ? 0 : Math.max(0, shownSince.current + minMs - Date.now());
    const timer = setTimeout(() => {
      shownSince.current = Date.now();
      setShown((s) => s + 1);
    }, wait);
    return () => clearTimeout(timer);
  }, [paced, shown, target, minMs]);

  const skip = useCallback(() => setSkipped(true), []);
  const visible = paced ? Math.min(shown, target) : target;
  return { shown: visible, catchingUp: visible < target, skip };
}
