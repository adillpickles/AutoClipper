import { describe, expect, it } from "vitest";

import type { Candidate } from "@/lib/api/types";
import { layoutTimelineMarkers, MIN_MARKER_WIDTH_PCT } from "@/lib/timeline";

function candidate(
  id: string,
  rank: number,
  start_s: number,
  end_s: number,
  score: number,
): Candidate {
  return {
    candidate_id: id,
    rank,
    score,
    start_s,
    end_s,
    source: "discovered",
    n_viewer_clips: 0,
    top_signals: [],
  };
}

describe("timeline layout helpers", () => {
  it("packs overlapping markers into distinct lanes and reuses lanes once clear", () => {
    const items = [
      candidate("c001", 1, 100, 140, 4.0),
      candidate("c002", 2, 110, 150, 3.0), // overlaps c001
      candidate("c003", 3, 120, 160, 2.0), // overlaps c001 and c002
      candidate("c004", 4, 220, 260, 1.0), // clear of c001 -> reuses lane 0
    ];

    const layout = layoutTimelineMarkers(items, 360);

    expect(layout.laneCount).toBe(3);
    const laneById = Object.fromEntries(
      layout.markers.map((m) => [m.candidate.candidate_id, m.lane]),
    );
    expect(laneById).toEqual({
      c001: 0,
      c002: 1,
      c003: 2,
      c004: 0,
    });
    for (const marker of layout.markers) {
      expect(marker.isShort).toBe(false);
      expect(marker.hitLeftPct).toBeCloseTo(marker.anchorLeftPct);
      expect(marker.hitWidthPct).toBeCloseTo(marker.anchorWidthPct);
      expect(marker.anchorOffsetInHitPct).toBe(0);
      expect(marker.anchorSpanInHitPct).toBe(100);
    }
  });

  it("keeps short moments anchored at their true time inside an enlarged clickable hit area", () => {
    const durationS = 21600; // 6-hour VOD
    const items = [
      candidate("c001", 1, 10, 30, 3.0), // 20 s near start (~0.05%)
      candidate("c002", 2, 100, 120, 2.5), // visually within c001's 6% hit area -> lane 1
      candidate("c003", 3, 10800, 10830, 2.2), // 30 s right at the 3 h midpoint (50%)
      candidate("c004", 4, 21570, 21600, 2.0), // 30 s right at the VOD's end (~99.86%)
    ];

    const layout = layoutTimelineMarkers(items, durationS);

    for (const marker of layout.markers) {
      expect(marker.isShort).toBe(true);
      expect(marker.hitWidthPct).toBeGreaterThanOrEqual(MIN_MARKER_WIDTH_PCT);
      expect(marker.hitLeftPct).toBeGreaterThanOrEqual(0);
      expect(marker.hitLeftPct + marker.hitWidthPct).toBeLessThanOrEqual(100);
      // True anchor always lies inside the hit area.
      expect(marker.anchorLeftPct).toBeGreaterThanOrEqual(marker.hitLeftPct);
      expect(marker.anchorLeftPct + marker.anchorWidthPct).toBeLessThanOrEqual(
        marker.hitLeftPct + marker.hitWidthPct + 1e-9,
      );
    }

    const byId = Object.fromEntries(
      layout.markers.map((m) => [m.candidate.candidate_id, m]),
    );

    // End-of-VOD 30 s moment stays anchored at ~99.86%, not shifted back to 94%.
    expect(byId.c004.anchorLeftPct).toBeCloseTo((21570 / durationS) * 100, 4);
    expect(byId.c004.anchorWidthPct).toBeCloseTo((30 / durationS) * 100, 4);
    expect(byId.c004.hitLeftPct).toBeCloseTo(100 - MIN_MARKER_WIDTH_PCT, 4);
    expect(byId.c004.anchorOffsetInHitPct).toBeGreaterThan(95);

    // Mid-VOD 30 s moment centers its hit area around 50% and anchors near the middle of the hit box.
    expect(byId.c003.anchorLeftPct).toBeCloseTo(50, 4);
    expect(byId.c003.anchorOffsetInHitPct).toBeCloseTo(49.31, 1);

    // Nearby short clips still separate into distinct lanes; distant ones reuse lane 0.
    expect(byId.c001.lane).toBe(0);
    expect(byId.c002.lane).toBe(1);
    expect(byId.c003.lane).toBe(0);
    expect(byId.c004.lane).toBe(0);
  });

  it("handles negative z-scores, aligned tick percentages, and empty lists cleanly", () => {
    expect(layoutTimelineMarkers([], 360)).toEqual({
      markers: [],
      laneCount: 0,
      ticks: [
        { pct: 0, seconds: 0 },
        { pct: 25, seconds: 90 },
        { pct: 50, seconds: 180 },
        { pct: 75, seconds: 270 },
        { pct: 100, seconds: 360 },
      ],
    });

    const layout = layoutTimelineMarkers(
      [
        candidate("c001", 1, 20, 50, -0.5),
        candidate("c002", 2, 200, 230, -2.5),
      ],
      360,
    );
    expect(layout.markers[0].scoreFraction).toBeCloseTo(1.0);
    expect(layout.markers[1].scoreFraction).toBeCloseTo(0.15);
  });
});
