import { describe, expect, it } from "vitest";

import type { Candidate } from "@/lib/api/types";
import {
  buildActivityBins,
  layoutTimelineMarkers,
  MIN_MARKER_WIDTH_PCT,
} from "@/lib/timeline";

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
  });

  it("enforces minimum clickable marker width and separates visually close short clips", () => {
    const items = [
      candidate("c001", 1, 10, 30, 3.0), // 20s in a 6-hour VOD (~0.09%)
      candidate("c002", 2, 100, 120, 2.5), // visually within c001's 6% min width -> lane 1
      candidate("c003", 3, 21580, 21600, 2.0), // right at the VOD's end
    ];

    const layout = layoutTimelineMarkers(items, 21600);

    for (const marker of layout.markers) {
      expect(marker.widthPct).toBeGreaterThanOrEqual(MIN_MARKER_WIDTH_PCT);
      expect(marker.leftPct).toBeGreaterThanOrEqual(0);
      expect(marker.leftPct + marker.widthPct).toBeLessThanOrEqual(100);
    }
    expect(layout.markers[0].lane).toBe(0);
    expect(layout.markers[1].lane).toBe(1);
    expect(layout.markers[2].lane).toBe(0);
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

  it("builds activity heatmap bins concentrated over candidate intervals", () => {
    const bins = buildActivityBins(
      [candidate("c001", 1, 180, 210, 5.0)],
      360,
      12,
    );

    expect(bins).toHaveLength(12);
    expect(bins[0]).toBe(0);
    expect(bins[6]).toBe(1); // 180..210 falls in bin 6 of 12 (30 s per bin)
    expect(bins[11]).toBe(0);
  });
});
