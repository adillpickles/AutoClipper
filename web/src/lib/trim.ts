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

/** Smallest selection length a vertical clip export allows (seconds; matches `autoclipper.export.cut.MIN_CLIP_DURATION_S`). */
export const MIN_TRIM_DURATION_S = 0.5;

/** Float-comparison slack matching `autoclipper.export.cut.DURATION_EPSILON_S` (e.g. `4.1 - 3.6`). */
const DURATION_EPSILON_S = 1e-6;

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

/**
 * Round interior values to tenths while preserving exact `[low, high]` limits whenever a value
 * reaches or exceeds either bound (so e.g. `159.96` never rounds past the bound to `160.0`).
 */
function roundWithin(value: number, low: number, high: number): number {
  if (low >= high) return low;
  if (value <= low) return low;
  if (value >= high) return high;
  return clamp(roundTenths(value), low, high);
}

/** Remove IEEE-754 subtraction noise (e.g. `159.96 - 0.5`) while staying strictly inside `[low, high]`. */
function cleanWithin(value: number, low: number, high: number): number {
  if (low >= high) return low;
  if (value <= low) return low;
  if (value >= high) return high;
  return clamp(Math.round(value * 1e6) / 1e6, low, high);
}

function formatBoundS(value: number): string {
  if (Math.abs(value * 10 - Math.round(value * 10)) < DURATION_EPSILON_S) {
    return value.toFixed(1);
  }
  return String(value);
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
  return clamp(next, bounds.minStartS, bounds.maxEndS);
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
  return clamp(next, bounds.minStartS, bounds.maxEndS);
}

/**
 * Clamp a trim interval to `[minStartS, maxEndS]`, `MIN_TRIM_DURATION_S`, and `maxDurationS`
 * while preserving exact media limits. When `anchor` is `"end"`, `endS` is kept fixed and
 * `startS` is adjusted first; otherwise `endS` is adjusted first.
 */
export function clampTrim(
  startS: number,
  endS: number,
  bounds: TrimBounds,
  anchor: "start" | "end" = "start",
): TrimInterval {
  const span = Math.max(0, bounds.maxEndS - bounds.minStartS);
  const minGap = Math.min(MIN_TRIM_DURATION_S, span);
  const safeStart = Number.isFinite(startS) ? startS : bounds.minStartS;
  const safeEnd = Number.isFinite(endS) ? endS : bounds.maxEndS;
  const maxStartLimit = cleanWithin(
    bounds.maxEndS - minGap,
    bounds.minStartS,
    bounds.maxEndS,
  );
  const minEndLimit = cleanWithin(
    bounds.minStartS + minGap,
    bounds.minStartS,
    bounds.maxEndS,
  );
  let nextStart = roundWithin(safeStart, bounds.minStartS, maxStartLimit);
  let nextEnd = roundWithin(safeEnd, minEndLimit, bounds.maxEndS);
  if (nextEnd - nextStart < minGap - DURATION_EPSILON_S) {
    if (anchor === "end") {
      nextStart = cleanWithin(
        nextEnd - minGap,
        bounds.minStartS,
        bounds.maxEndS,
      );
      if (nextEnd - nextStart < minGap - DURATION_EPSILON_S) {
        nextEnd = cleanWithin(
          nextStart + minGap,
          bounds.minStartS,
          bounds.maxEndS,
        );
      }
    } else {
      nextEnd = cleanWithin(
        nextStart + minGap,
        bounds.minStartS,
        bounds.maxEndS,
      );
      if (nextEnd - nextStart < minGap - DURATION_EPSILON_S) {
        nextStart = cleanWithin(
          nextEnd - minGap,
          bounds.minStartS,
          bounds.maxEndS,
        );
      }
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
  const maxStart = cleanWithin(
    current.endS - MIN_TRIM_DURATION_S,
    minStart,
    bounds.maxEndS,
  );
  const nextStart = fitMaxDurationStart(
    roundWithin(current.startS + deltaS, minStart, maxStart),
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
  const minEnd = cleanWithin(
    current.startS + MIN_TRIM_DURATION_S,
    bounds.minStartS,
    maxEnd,
  );
  const nextEnd = fitMaxDurationEnd(
    current.startS,
    roundWithin(current.endS + deltaS, minEnd, maxEnd),
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
    return `Keep the clip between ${formatBoundS(bounds.minStartS)}s and ${formatBoundS(bounds.maxEndS)}s.`;
  }
  const duration = interval.endS - interval.startS;
  if (duration <= 0) {
    return "End time must be after start time.";
  }
  if (duration < MIN_TRIM_DURATION_S - DURATION_EPSILON_S) {
    return `Clips must be at least ${MIN_TRIM_DURATION_S} seconds long. Widen the selection to export it.`;
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
