// Pure trim-interval helpers for the clip editor (#31, api-contract.md § 5-6).

export interface TrimBounds {
  minStartS: number;
  maxEndS: number;
  maxDurationS: number | null;
}

export interface TrimInterval {
  startS: number;
  endS: number;
}

/** Smallest selection length the nudge controls allow (seconds). */
export const MIN_TRIM_DURATION_S = 0.5;

function roundTenths(value: number): number {
  return Math.round(value * 10) / 10;
}

function ceilTenths(value: number): number {
  return Math.ceil(value * 10 - 1e-9) / 10;
}

function floorTenths(value: number): number {
  return Math.floor(value * 10 + 1e-9) / 10;
}

function clamp(value: number, low: number, high: number): number {
  return Math.min(high, Math.max(low, value));
}

function fitMaxDurationStart(
  startS: number,
  endS: number,
  bounds: TrimBounds,
): number {
  if (bounds.maxDurationS === null || endS - startS <= bounds.maxDurationS) {
    return startS;
  }
  let next = Math.max(bounds.minStartS, ceilTenths(endS - bounds.maxDurationS));
  // Guard against IEEE-754 decimal overshoot such as `128.3 - 38.3 === 90.00000000000001`.
  while (endS - next > bounds.maxDurationS && roundTenths(next + 0.1) < endS) {
    next = roundTenths(next + 0.1);
  }
  return next;
}

function fitMaxDurationEnd(
  startS: number,
  endS: number,
  bounds: TrimBounds,
): number {
  if (bounds.maxDurationS === null || endS - startS <= bounds.maxDurationS) {
    return endS;
  }
  let next = Math.min(
    bounds.maxEndS,
    floorTenths(startS + bounds.maxDurationS),
  );
  // Guard against IEEE-754 decimal overshoot such as `128.3 - 38.3 === 90.00000000000001`.
  while (
    next - startS > bounds.maxDurationS &&
    roundTenths(next - 0.1) > startS
  ) {
    next = roundTenths(next - 0.1);
  }
  return next;
}

/**
 * Clamp a trim interval to `[minStartS, maxEndS]` and `maxDurationS` while keeping
 * `startS < endS`. When `anchor` is `"end"`, `endS` is kept fixed and `startS` is pulled
 * forward to satisfy `maxDurationS`; otherwise `endS` is pulled back.
 */
export function clampTrim(
  startS: number,
  endS: number,
  bounds: TrimBounds,
  anchor: "start" | "end" = "start",
): TrimInterval {
  const span = Math.max(0, bounds.maxEndS - bounds.minStartS);
  const minGap = Math.min(MIN_TRIM_DURATION_S, span / 2);
  const safeStart = Number.isFinite(startS) ? startS : bounds.minStartS;
  const safeEnd = Number.isFinite(endS) ? endS : bounds.maxEndS;
  let nextStart = roundTenths(
    clamp(safeStart, bounds.minStartS, bounds.maxEndS - minGap),
  );
  let nextEnd = roundTenths(
    clamp(safeEnd, bounds.minStartS + minGap, bounds.maxEndS),
  );
  if (nextEnd <= nextStart) {
    nextEnd = roundTenths(Math.min(bounds.maxEndS, nextStart + minGap));
    if (nextEnd <= nextStart) {
      nextStart = roundTenths(Math.max(bounds.minStartS, nextEnd - minGap));
    }
  }
  if (anchor === "end") {
    nextStart = fitMaxDurationStart(nextStart, nextEnd, bounds);
  } else {
    nextEnd = fitMaxDurationEnd(nextStart, nextEnd, bounds);
  }
  return { startS: nextStart, endS: nextEnd };
}

/** Nudge `startS` by `deltaS`, clamped to `[minStartS, endS - MIN_TRIM_DURATION_S]` and `maxDurationS`. */
export function nudgeStart(
  current: TrimInterval,
  deltaS: number,
  bounds: TrimBounds,
): TrimInterval {
  const minStart = fitMaxDurationStart(bounds.minStartS, current.endS, bounds);
  const maxStart = Math.max(minStart, current.endS - MIN_TRIM_DURATION_S);
  const nextStart = fitMaxDurationStart(
    roundTenths(clamp(current.startS + deltaS, minStart, maxStart)),
    current.endS,
    bounds,
  );
  return { startS: nextStart, endS: current.endS };
}

/** Nudge `endS` by `deltaS`, clamped to `[startS + MIN_TRIM_DURATION_S, maxEndS]` and `maxDurationS`. */
export function nudgeEnd(
  current: TrimInterval,
  deltaS: number,
  bounds: TrimBounds,
): TrimInterval {
  const maxEnd = fitMaxDurationEnd(current.startS, bounds.maxEndS, bounds);
  const minEnd = Math.min(maxEnd, current.startS + MIN_TRIM_DURATION_S);
  const nextEnd = fitMaxDurationEnd(
    current.startS,
    roundTenths(clamp(current.endS + deltaS, minEnd, maxEnd)),
    bounds,
  );
  return { startS: current.startS, endS: nextEnd };
}

/** Return a validation message if the trim violates export bounds, or `null` when valid. */
export function trimError(
  interval: TrimInterval,
  bounds: TrimBounds,
): string | null {
  if (!Number.isFinite(interval.startS) || !Number.isFinite(interval.endS)) {
    return "Enter valid start and end times.";
  }
  if (interval.startS < bounds.minStartS || interval.endS > bounds.maxEndS) {
    return `Keep the clip between ${bounds.minStartS.toFixed(1)}s and ${bounds.maxEndS.toFixed(1)}s.`;
  }
  const duration = interval.endS - interval.startS;
  if (duration <= 0) {
    return "End time must be after start time.";
  }
  if (bounds.maxDurationS !== null && duration > bounds.maxDurationS) {
    return `Clips can be at most ${bounds.maxDurationS} seconds long. Shorten the selection to export it.`;
  }
  return null;
}

/** Human-readable label for a contract `MediaUnavailableReason` (open enum, § 5). */
export function unavailableReasonLabel(reason: string | null): string {
  switch (reason) {
    case "source_unavailable":
      return "Source unavailable";
    case "local_file_missing":
      return "Local file missing";
    case "fetch_failed":
      return "Download failed";
    default:
      return "Video unavailable";
  }
}
