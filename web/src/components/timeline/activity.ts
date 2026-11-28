// Pure math for the activity curve behind the scrub track (#213): the result's downsampled chat
// rate and loudness (contract § 4.1 `activity`) as SVG paths in a 1000 × 100 viewBox stretched
// over the track. `null` entries are stretches where nothing was measured, so each run of values
// is its own sub-path and the curve has a gap there instead of dropping to zero.

import type { Activity } from "@/lib/api/types";
import { formatTime } from "@/lib/format";

import { safeDuration } from "./geometry";

/** viewBox width; x is VOD time scaled to `[0, VIEW_W]`. */
export const VIEW_W = 1000;
/** viewBox height; y = 0 is the top of the track. */
export const VIEW_H = 100;
/** Highest a curve reaches by default, leaving the top of the track to the rank labels. */
const CURVE_TOP = 24;
/**
 * Highest the loudness line reaches: 60% of the track, anchored at the bottom, so it reads as
 * context behind the chat area rather than competing with it.
 */
export const LOUDNESS_TOP = VIEW_H * 0.4;
/**
 * Loudness at or below this (dBFS) is digital silence at the contract's -60 floor: a muted song
 * or a starting screen. It is drawn at the baseline and left out of the scale, so a few minutes
 * of silence don't squash the rest of the stream into the top of the track.
 */
const SILENCE_DBFS = -59.5;
/** The scale's top: one raid or emote-wall spike shouldn't flatten every other point. */
const TOP_PERCENTILE = 0.98;
/** Loudness scale bottom, robust to a few unusually quiet points. */
const LOUDNESS_BOTTOM_PERCENTILE = 0.05;
/**
 * Half a 10 s window: the pipeline makes no window for a shorter tail, so a last point starting
 * this close to the end is a tail without data, not an unmeasured stretch.
 */
const MIN_WINDOW_S = 5;

export type ActivityKind = "chat" | "loudness";

type Series = readonly (number | null)[];

/** The entries of a series as numbers or `null`, or `null` when it has no finite value at all. */
export function usableSeries(values: unknown): (number | null)[] | null {
  if (!Array.isArray(values)) return null;
  const clean = values.map((v) =>
    typeof v === "number" && Number.isFinite(v) ? v : null,
  );
  return clean.some((v) => v !== null) ? clean : null;
}

/** The `q` quantile (0–1) of ascending `sorted`, interpolating between neighbours. */
export function quantile(sorted: readonly number[], q: number): number {
  if (sorted.length === 0) return Number.NaN;
  const position = Math.min(Math.max(q, 0), 1) * (sorted.length - 1);
  const below = Math.floor(position);
  const above = Math.min(below + 1, sorted.length - 1);
  return sorted[below] + (sorted[above] - sorted[below]) * (position - below);
}

/**
 * Heights in `[0, 1]` for drawing, on a robust scale; points outside it are clamped. Chat runs
 * from 0 to its 98th percentile, so a quiet chat stays low and one spike doesn't flatten the
 * rest. Loudness (dBFS) runs from the 5th to the 98th percentile of its non-silent points:
 * stream audio lives in a narrow band, so a fixed -60..0 scale would draw a flat line, and
 * silence (see `SILENCE_DBFS`) sits at the baseline.
 */
export function normalizeSeries(
  values: Series,
  kind: ActivityKind,
): (number | null)[] {
  const finite = values.filter((v): v is number => v !== null);
  if (finite.length === 0) return values.map(() => null);
  const audible =
    kind === "loudness" ? finite.filter((v) => v > SILENCE_DBFS) : finite;
  const basis = (audible.length > 0 ? audible : finite).sort((a, b) => a - b);
  const low = kind === "chat" ? 0 : quantile(basis, LOUDNESS_BOTTOM_PERCENTILE);
  let high = quantile(basis, TOP_PERCENTILE);
  // Mostly-flat series (e.g. a silent chat with one burst): fall back to the maximum.
  if (!(high > low)) high = basis[basis.length - 1];
  const span = high - low;
  return values.map((v) => {
    if (v === null) return null;
    if (!(span > 0)) {
      if (kind === "chat") return high <= 0 ? 0 : 0.5;
      // Silence stays on the baseline even when every audible point has the same level.
      return audible.length > 0 && v <= SILENCE_DBFS ? 0 : 0.5;
    }
    return Math.min(1, Math.max(0, (v - low) / span));
  });
}

/** Index ranges `[first, last]` of consecutive non-null entries. */
export function seriesRuns(values: Series): [number, number][] {
  const runs: [number, number][] = [];
  let first: number | null = null;
  values.forEach((v, i) => {
    if (v !== null && first === null) first = i;
    if (v === null && first !== null) {
      runs.push([first, i - 1]);
      first = null;
    }
  });
  if (first !== null) runs.push([first, values.length - 1]);
  return runs;
}

function fmt(n: number): string {
  return Number(n.toFixed(2)).toString();
}

/**
 * One SVG path per run of measured points. Each point sits at the centre of its step; a run's
 * ends extend flat to its outer step edges, so a lone point still shows as a short plateau.
 * `area` closes each run down to the baseline; `top` is the y a height of 1 reaches.
 */
export function curvePaths(
  heights: Series,
  stepS: number,
  durationS: number,
  { area, top = CURVE_TOP }: { area: boolean; top?: number },
): string[] {
  const duration = safeDuration(durationS);
  if (!(stepS > 0)) return [];
  const x = (s: number) =>
    (Math.min(Math.max(s, 0), duration) / duration) * VIEW_W;
  const y = (h: number) => VIEW_H - h * (VIEW_H - top);
  // Points past the VOD's end can't be placed; drop them.
  const placed = heights.map((h, i) => (i * stepS < duration ? h : null));
  return seriesRuns(placed).map(([first, last]) => {
    const vertices: [number, number][] = [];
    vertices.push([x(first * stepS), y(placed[first] ?? 0)]);
    for (let i = first; i <= last; i += 1) {
      const centre = (i * stepS + Math.min((i + 1) * stepS, duration)) / 2;
      vertices.push([x(centre), y(placed[i] ?? 0)]);
    }
    const endX = x((last + 1) * stepS);
    vertices.push([endX, y(placed[last] ?? 0)]);
    const line = vertices
      .map(([vx, vy], i) => `${i === 0 ? "M" : "L"}${fmt(vx)} ${fmt(vy)}`)
      .join(" ");
    if (!area) return line;
    return `${line} L${fmt(endX)} ${VIEW_H} L${fmt(vertices[0][0])} ${VIEW_H} Z`;
  });
}

/**
 * `values` without a last `null` point that starts within half a window of the VOD's end:
 * nothing could be measured there, so it is neither drawn nor reported as a gap. (Results from
 * before the pipeline stopped writing that point still have it.)
 */
function withoutDatalessTail(
  values: (number | null)[] | null,
  stepS: number,
  durationS: number | undefined,
): (number | null)[] | null {
  if (!values || values.length < 2 || durationS === undefined) return values;
  const last = values.length - 1;
  if (values[last] === null && last * stepS > durationS - MIN_WINDOW_S) {
    return values.slice(0, last);
  }
  return values;
}

/** The series worth drawing, keyed by kind; empty when `activity` is absent or unusable. */
export function drawableActivity(
  activity: Activity | null | undefined,
  durationS?: number,
): Partial<Record<ActivityKind, (number | null)[]>> {
  if (!activity || !(activity.step_s > 0)) return {};
  const out: Partial<Record<ActivityKind, (number | null)[]>> = {};
  const step = activity.step_s;
  const chat = withoutDatalessTail(
    usableSeries(activity.chat),
    step,
    durationS,
  );
  const loudness = withoutDatalessTail(
    usableSeries(activity.loudness),
    step,
    durationS,
  );
  if (chat) out.chat = chat;
  if (loudness) out.loudness = loudness;
  return out;
}

function peak(
  values: Series,
  stepS: number,
  durationS: number,
): { value: number; timeS: number } | null {
  let best: number | null = null;
  values.forEach((v, i) => {
    if (v !== null && (best === null || v > (values[best] as number))) {
      best = i;
    }
  });
  if (best === null) return null;
  const startS = best * stepS;
  return {
    value: values[best] as number,
    timeS: (startS + Math.min(startS + stepS, safeDuration(durationS))) / 2,
  };
}

function gapCount(values: Series): number {
  return Math.max(
    0,
    seriesRuns(values).length -
      1 +
      (values[0] === null ? 1 : 0) +
      (values[values.length - 1] === null ? 1 : 0),
  );
}

/** One or two sentences for screen readers describing what the decorative curve shows. */
export function activitySummary(
  activity: Activity | null | undefined,
  durationS: number,
): string | null {
  const series = drawableActivity(activity, durationS);
  if (!activity || (!series.chat && !series.loudness)) return null;
  const parts: string[] = [];
  const gaps: string[] = [];
  if (series.chat) {
    const p = peak(series.chat, activity.step_s, durationS);
    if (p) {
      parts.push(
        `chat is busiest around ${formatTime(p.timeS)}, at ${p.value.toFixed(1)} messages a second`,
      );
    }
    if (gapCount(series.chat) > 0) gaps.push("chat");
  }
  if (series.loudness) {
    const p = peak(series.loudness, activity.step_s, durationS);
    if (p) parts.push(`the stream is loudest around ${formatTime(p.timeS)}`);
    if (gapCount(series.loudness) > 0) gaps.push("loudness");
  }
  const missing = [
    !series.chat ? "chat" : null,
    !series.loudness ? "loudness" : null,
  ].filter((m): m is string => m !== null);
  let text = `Activity behind the timeline: ${parts.join("; ")}.`;
  if (missing.length > 0) text += ` No ${missing.join(" or ")} was measured.`;
  if (gaps.length > 0) {
    text += ` Some stretches have no ${gaps.join(" or ")} measurement.`;
  }
  return text;
}
