// Pure placement math for the scrubbable stream timeline (#189). Every position is a percentage
// of the VOD duration on `[0, 100]`, so the scrub track, the moment spans, the playhead and the
// time ruler all share one scale.

import type { Candidate } from "@/lib/api/types";

/** Clamp `value` into `[low, high]`. */
export function clamp(value: number, low: number, high: number): number {
  return Math.min(high, Math.max(low, value));
}

/** A usable duration: positive and finite, else 1 s so the math never divides by zero. */
export function safeDuration(durationS: number): number {
  return Number.isFinite(durationS) && durationS > 0 ? durationS : 1;
}

/** Position of a VOD time on the timeline, in `[0, 100]`%. */
export function pctOf(timeS: number, durationS: number): number {
  return clamp((timeS / safeDuration(durationS)) * 100, 0, 100);
}

export interface SpanGeometry {
  /** Left edge at the moment's true `start_s`. */
  leftPct: number;
  /** True span width (`end_s - start_s`), never negative. */
  widthPct: number;
}

/** True-time placement of a moment's `[start_s, end_s)` on the timeline. */
export function spanGeometry(
  candidate: Pick<Candidate, "start_s" | "end_s">,
  durationS: number,
): SpanGeometry {
  const leftPct = pctOf(candidate.start_s, durationS);
  const rightPct = clamp(pctOf(candidate.end_s, durationS), leftPct, 100);
  return { leftPct, widthPct: rightPct - leftPct };
}

/** VOD time under a pointer at `clientX` over an element spanning `rect`. */
export function timeAtClientX(
  clientX: number,
  rect: { left: number; width: number },
  durationS: number,
): number {
  if (!(rect.width > 0)) return 0;
  const fraction = clamp((clientX - rect.left) / rect.width, 0, 1);
  return fraction * safeDuration(durationS);
}

/**
 * The best-ranked moment whose `[start_s, end_s)` contains `timeS`, or `null`. With `durationS`,
 * the very end of the VOD also counts as inside a moment that runs to the end, so the playhead at
 * End (or a click on the track's right edge) still finds it.
 */
export function momentAt<
  T extends Pick<Candidate, "start_s" | "end_s" | "rank">,
>(timeS: number, candidates: readonly T[], durationS?: number): T | null {
  const atEnd = durationS !== undefined && timeS >= safeDuration(durationS);
  let best: T | null = null;
  for (const candidate of candidates) {
    // Half-open `[start_s, end_s)`; a zero-length moment still matches its own start.
    const inside =
      timeS >= candidate.start_s &&
      (timeS < candidate.end_s ||
        timeS === candidate.start_s ||
        (atEnd && candidate.end_s >= timeS));
    if (inside && (best === null || candidate.rank < best.rank)) {
      best = candidate;
    }
  }
  return best;
}

/**
 * Start time of the next (`direction` 1) or previous (`-1`) moment relative to `timeS`, or `null`
 * when there is none in that direction.
 */
export function adjacentMomentStart(
  timeS: number,
  candidates: readonly Pick<Candidate, "start_s">[],
  direction: 1 | -1,
): number | null {
  const starts = [...new Set(candidates.map((c) => c.start_s))].sort(
    (a, b) => a - b,
  );
  if (direction === 1) {
    return starts.find((start) => start > timeS + 0.5) ?? null;
  }
  for (let i = starts.length - 1; i >= 0; i -= 1) {
    if (starts[i] < timeS - 0.5) return starts[i];
  }
  return null;
}

export interface RulerTick {
  seconds: number;
  pct: number;
}

const NICE_STEPS_S = [
  5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 10800,
] as const;

/**
 * Ruler ticks at a round step (5 s … 3 h) chosen so at most `maxTicks` intervals fit, always
 * including 0 and the VOD end. Interior ticks that would crowd the end label are dropped.
 */
export function rulerTicks(durationS: number, maxTicks = 4): RulerTick[] {
  const duration = safeDuration(durationS);
  const target = duration / Math.max(1, maxTicks);
  const step =
    NICE_STEPS_S.find((candidate) => candidate >= target) ??
    Math.ceil(target / 3600) * 3600;
  const ticks: RulerTick[] = [];
  for (let seconds = 0; seconds < duration; seconds += step) {
    const pct = pctOf(seconds, duration);
    if (seconds > 0 && 100 - pct < 12) break;
    ticks.push({ seconds, pct });
  }
  ticks.push({ seconds: duration, pct: 100 });
  return ticks;
}

/**
 * Where to pin a span's rank label so it stays inside the track: centered on the span, except
 * near the edges, where it is pinned to that edge instead of being cut off.
 */
export function rankLabelAnchor(
  leftPct: number,
  widthPct: number,
): "left" | "right" | "center" {
  const center = leftPct + widthPct / 2;
  if (center < 3) return "left";
  if (center > 97) return "right";
  return "center";
}

export type PopoverPlacement = "above" | "beside-left" | "beside-right";

/**
 * Where the scrub popover goes, so it never covers content outside the timeline card (the
 * "Download all clips" row above it, say) and never sticks out of the card's sides. It sits
 * `gap` px above the track, centred on the playhead, when that fits inside the card (`inset` px
 * from its top edge). Otherwise it is clamped to the card's top and moved beside the playhead
 * (`offset` px clear of it), on the side with more room, so it overlaps the track without hiding
 * the spot being previewed. Either way its left edge is then clamped so the whole label stays
 * `inset` px inside the card's left and right edges; on a card too narrow for that it may cover
 * the playhead rather than leave the card.
 *
 * `height` should be the tallest the label can get (with a moment and a frame), not its current
 * height, so the placement doesn't flip while scrubbing on and off a moment (#238). Inputs are
 * viewport pixels; `topPx` and `leftPx` are the popover's `top` and `left` relative to the track.
 */
export function popoverPosition({
  track,
  bounds,
  width,
  height,
  cursorPct,
  gap = 8,
  inset = 8,
  offset = 12,
}: {
  track: { left: number; top: number; width: number };
  bounds: { left: number; right: number; top: number };
  width: number;
  height: number;
  cursorPct: number;
  gap?: number;
  inset?: number;
  offset?: number;
}): { topPx: number; leftPx: number; placement: PopoverPlacement } {
  const anchor = track.left + (clamp(cursorPct, 0, 100) / 100) * track.width;
  const minTop = bounds.top + inset;
  const minLeft = bounds.left + inset;
  const maxRight = bounds.right - inset;
  const above = track.top - gap - height;

  let placement: PopoverPlacement;
  let top: number;
  let left: number;
  if (above >= minTop) {
    placement = "above";
    top = above;
    left = anchor - width / 2;
  } else {
    top = minTop;
    placement =
      maxRight - anchor >= anchor - minLeft ? "beside-right" : "beside-left";
    left =
      placement === "beside-right" ? anchor + offset : anchor - offset - width;
  }
  // A label wider than the card (never the case at 320 px and up) keeps its left edge inside.
  left = clamp(left, minLeft, Math.max(minLeft, maxRight - width));
  return { topPx: top - track.top, leftPx: left - track.left, placement };
}

/**
 * Which way a floating label anchored at `pct` should extend so it stays inside the track. Only a
 * first guess before the label is measured (`popoverPosition` then clamps it to the card).
 */
export function popoverAlign(pct: number): "start" | "center" | "end" {
  if (pct < 18) return "start";
  if (pct > 82) return "end";
  return "center";
}
