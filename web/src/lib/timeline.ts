// Pure layout helpers for the whole-stream timeline view (#67, api-contract.md § 4.5). The
// scrubbable timeline (#189) uses them to pack the top moments' rank chips into rows.

import type { Candidate } from "@/lib/api/types";

/**
 * Minimum horizontal interactive hit-area width (% of timeline) so short clips stay clickable and
 * fit their rank badge cleanly inside the button even on a 320 px viewport (~25 px at 10%).
 *
 * The visual anchor for short clips is drawn at the true start/span (`anchorLeftPct`,
 * `anchorWidthPct`) inside this hit area so a 30 s clip near the end of a 6 h VOD is never drawn
 * minutes early.
 */
export const MIN_MARKER_WIDTH_PCT = 10.0;

/** Minimum horizontal breathing room (% of timeline) before two markers share a lane. */
const LANE_GAP_PCT = 1.0;

export interface TimelineMarker {
  candidate: Candidate;
  /** Exact start position on `[0, 100]%` of `durationS` (true VOD time). */
  anchorLeftPct: number;
  /** Exact interval span on `[0, 100]%` of `durationS` (true VOD duration). */
  anchorWidthPct: number;
  /** Enlarged interactive hit-area left edge (`[0, 100 - hitWidthPct]%`), used for lane packing. */
  hitLeftPct: number;
  /** Enlarged interactive hit-area width (`>= MIN_MARKER_WIDTH_PCT` and `<= 100`). */
  hitWidthPct: number;
  /** True when the real interval is narrower than the minimum hit area and uses an anchor pin. */
  isShort: boolean;
  /** Anchor start offset inside the hit area (`[0, 100]%` of `hitWidthPct`). */
  anchorOffsetInHitPct: number;
  /** Anchor span width inside the hit area (`[0, 100]%` of `hitWidthPct`). */
  anchorSpanInHitPct: number;
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
 * Place every candidate on `[0, 100]%` of `durationS`, keeping its true-time anchor intact while
 * packing enlarged clickable hit areas into non-overlapping horizontal lanes.
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

  // Compute true-time anchor and enlarged hit-area geometry before lane packing.
  const positioned = candidates.map((candidate) => {
    const anchorLeftPct = clamp(
      (candidate.start_s / safeDuration) * 100,
      0,
      100,
    );
    const anchorRightPct = clamp(
      (candidate.end_s / safeDuration) * 100,
      anchorLeftPct,
      100,
    );
    const anchorWidthPct = anchorRightPct - anchorLeftPct;
    const isShort = anchorWidthPct < MIN_MARKER_WIDTH_PCT;
    const hitWidthPct = Math.min(
      100,
      Math.max(MIN_MARKER_WIDTH_PCT, anchorWidthPct),
    );
    const hitLeftPct = isShort
      ? clamp(
          anchorLeftPct + anchorWidthPct / 2 - hitWidthPct / 2,
          0,
          100 - hitWidthPct,
        )
      : Math.min(anchorLeftPct, 100 - hitWidthPct);
    const anchorOffsetInHitPct = isShort
      ? clamp(((anchorLeftPct - hitLeftPct) / hitWidthPct) * 100, 0, 100)
      : 0;
    const anchorSpanInHitPct = isShort
      ? clamp(
          (anchorWidthPct / hitWidthPct) * 100,
          0,
          100 - anchorOffsetInHitPct,
        )
      : 100;

    return {
      candidate,
      anchorLeftPct,
      anchorWidthPct,
      hitLeftPct,
      hitWidthPct,
      isShort,
      anchorOffsetInHitPct,
      anchorSpanInHitPct,
      scoreFraction: scale(candidate.score),
    };
  });

  // Sort by hit-area left edge so greedy lane packing guarantees non-overlapping lanes.
  positioned.sort(
    (a, b) =>
      a.hitLeftPct - b.hitLeftPct ||
      a.anchorLeftPct - b.anchorLeftPct ||
      a.candidate.end_s - b.candidate.end_s ||
      a.candidate.rank - b.candidate.rank,
  );

  // Track the rightmost hit-area edge (%) occupied in each lane.
  const laneEnds: number[] = [];
  const markers: TimelineMarker[] = [];

  for (const item of positioned) {
    const hitRightPct = item.hitLeftPct + item.hitWidthPct;

    let lane = laneEnds.findIndex(
      (laneEnd) => laneEnd + LANE_GAP_PCT <= item.hitLeftPct,
    );
    if (lane === -1) {
      lane = laneEnds.length;
      laneEnds.push(hitRightPct);
    } else {
      laneEnds[lane] = hitRightPct;
    }

    markers.push({
      ...item,
      lane,
    });
  }

  return { markers, laneCount: laneEnds.length, ticks };
}
