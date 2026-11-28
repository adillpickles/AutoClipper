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
/** Highest a curve reaches, leaving the top of the track to the rank labels. */
const CURVE_TOP = 24;
/** Quietest loudness the contract allows (dBFS); the scale never goes below it. */
const LOUDNESS_FLOOR_DBFS = -60;

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

/**
 * Heights in `[0, 1]` for drawing. Chat is scaled from 0 to its busiest point, so a quiet chat
 * stays low. Loudness (dBFS, from -60) is scaled between its quietest and loudest measured
 * points: stream audio lives in a narrow band, and a fixed -60..0 scale would draw a flat line.
 */
export function normalizeSeries(
  values: Series,
  kind: ActivityKind,
): (number | null)[] {
  const finite = values.filter((v): v is number => v !== null);
  if (finite.length === 0) return values.map(() => null);
  const high = Math.max(...finite);
  const low =
    kind === "chat" ? 0 : Math.max(LOUDNESS_FLOOR_DBFS, Math.min(...finite));
  const span = high - low;
  return values.map((v) => {
    if (v === null) return null;
    if (!(span > 0)) return kind === "chat" && high <= 0 ? 0 : 0.5;
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
 * `area` closes each run down to the baseline.
 */
export function curvePaths(
  heights: Series,
  stepS: number,
  durationS: number,
  { area }: { area: boolean },
): string[] {
  const duration = safeDuration(durationS);
  if (!(stepS > 0)) return [];
  const x = (s: number) =>
    (Math.min(Math.max(s, 0), duration) / duration) * VIEW_W;
  const y = (h: number) => VIEW_H - h * (VIEW_H - CURVE_TOP);
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

/** The series worth drawing, keyed by kind; empty when `activity` is absent or unusable. */
export function drawableActivity(
  activity: Activity | null | undefined,
): Partial<Record<ActivityKind, (number | null)[]>> {
  if (!activity || !(activity.step_s > 0)) return {};
  const out: Partial<Record<ActivityKind, (number | null)[]>> = {};
  const chat = usableSeries(activity.chat);
  const loudness = usableSeries(activity.loudness);
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
  const series = drawableActivity(activity);
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
