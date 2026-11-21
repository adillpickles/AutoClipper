// Pure layout helpers for the whole-stream timeline view (#67, api-contract.md § 4.5).

import type { Candidate } from "@/lib/api/types";

/**
 * Minimum horizontal bar width (% of timeline) so short clips stay wide enough to show their
 * rank and stay clickable even on a 320 px viewport (~19 px at 6%).
 */
export const MIN_MARKER_WIDTH_PCT = 6.0;

/** Minimum horizontal breathing room (% of timeline) before two markers share a lane. */
const LANE_GAP_PCT = 1.0;

export interface TimelineMarker {
  candidate: Candidate;
  leftPct: number;
  widthPct: number;
  lane: number;
  scoreFraction: number;
}

export interface TimelineTick {
  seconds: number;
  pct: number;
}

export interface TimelineLayout {
  markers: TimelineMarker[];
  laneCount: number;
  ticks: TimelineTick[];
}

function clamp(value: number, low: number, high: number): number {
  return Math.min(high, Math.max(low, value));
}

/**
 * Place every candidate on `[0, 100]%` of `durationS` and pack overlapping markers into
 * non-overlapping horizontal lanes so every marker stays visible and clickable.
 */
export function layoutTimelineMarkers(
  candidates: readonly Candidate[],
  durationS: number,
): TimelineLayout {
  const safeDuration =
    Number.isFinite(durationS) && durationS > 0 ? durationS : 1;
  const ticks: TimelineTick[] = [0, 25, 50, 75, 100].map((pct) => ({
    pct,
    seconds: Math.round((safeDuration * pct) / 100),
  }));
  if (candidates.length === 0) {
    return { markers: [], laneCount: 0, ticks };
  }

  const scores = candidates.map((c) => c.score);
  const low = Math.min(...scores);
  const high = Math.max(...scores);
  const scale = (score: number) =>
    high > low ? 0.15 + (0.85 * (score - low)) / (high - low) : 1;

  // Sort chronologically for deterministic greedy lane packing; ties go to higher rank.
  const ordered = [...candidates].sort(
    (a, b) => a.start_s - b.start_s || a.end_s - b.end_s || a.rank - b.rank,
  );

  // Track the rightmost visual edge (%) occupied in each lane.
  const laneEnds: number[] = [];
  const markers: TimelineMarker[] = [];

  for (const candidate of ordered) {
    const rawLeft = clamp((candidate.start_s / safeDuration) * 100, 0, 100);
    const rawRight = clamp(
      (candidate.end_s / safeDuration) * 100,
      rawLeft,
      100,
    );
    const widthPct = Math.min(
      100,
      Math.max(MIN_MARKER_WIDTH_PCT, rawRight - rawLeft),
    );
    const leftPct = Math.min(rawLeft, 100 - widthPct);
    const visualRight = leftPct + widthPct;

    let lane = laneEnds.findIndex(
      (laneEnd) => laneEnd + LANE_GAP_PCT <= leftPct,
    );
    if (lane === -1) {
      lane = laneEnds.length;
      laneEnds.push(visualRight);
    } else {
      laneEnds[lane] = visualRight;
    }

    markers.push({
      candidate,
      leftPct,
      widthPct,
      lane,
      scoreFraction: scale(candidate.score),
    });
  }

  return { markers, laneCount: laneEnds.length, ticks };
}

/**
 * Compute normalized `[0, 1]` activity intensity bins across `[0, durationS]` from candidate
 * intervals and scores, for the timeline heatmap strip.
 */
export function buildActivityBins(
  candidates: readonly Candidate[],
  durationS: number,
  binCount = 48,
): number[] {
  const count = Math.max(1, Math.floor(binCount));
  const bins = new Array<number>(count).fill(0);
  if (
    candidates.length === 0 ||
    !Number.isFinite(durationS) ||
    durationS <= 0
  ) {
    return bins;
  }

  const scores = candidates.map((c) => c.score);
  const low = Math.min(...scores);
  const high = Math.max(...scores);
  const weight = (score: number) =>
    high > low ? 0.25 + (0.75 * (score - low)) / (high - low) : 1;

  const binWidth = durationS / count;
  for (const candidate of candidates) {
    const startBin = clamp(
      Math.floor(candidate.start_s / binWidth),
      0,
      count - 1,
    );
    const endBin = clamp(
      Math.ceil(candidate.end_s / binWidth) - 1,
      startBin,
      count - 1,
    );
    const w = weight(candidate.score);
    for (let i = startBin; i <= endBin; i += 1) {
      bins[i] = Math.max(bins[i], w);
    }
  }
  return bins;
}
