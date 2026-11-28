import { describe, expect, it } from "vitest";

import {
  LOUDNESS_TOP,
  activitySummary,
  curvePaths,
  drawableActivity,
  normalizeSeries,
  quantile,
  seriesRuns,
  usableSeries,
} from "./activity";

describe("usableSeries", () => {
  it("keeps numbers and nulls, and drops series with nothing measured", () => {
    expect(usableSeries([1, null, 2])).toEqual([1, null, 2]);
    expect(usableSeries([1, Number.NaN, "x"])).toEqual([1, null, null]);
    expect(usableSeries([null, null])).toBeNull();
    expect(usableSeries(null)).toBeNull();
    expect(usableSeries(undefined)).toBeNull();
  });
});

describe("quantile", () => {
  it("interpolates between neighbours", () => {
    expect(quantile([0, 10], 0.5)).toBe(5);
    expect(quantile([1, 2, 3], 1)).toBe(3);
    expect(quantile([], 0.5)).toBeNaN();
  });
});

describe("normalizeSeries", () => {
  it("scales chat from zero to its 98th percentile, so one spike doesn't flatten the rest", () => {
    const rates = [...Array.from({ length: 100 }, (_, i) => i), 1000];
    const heights = normalizeSeries(rates, "chat");
    expect(heights[49]).toBeCloseTo(0.5);
    expect(heights[100]).toBe(1); // the spike is clamped
    expect(normalizeSeries([0, 2, null, 4], "chat")[2]).toBeNull();
  });

  it("falls back to the maximum when a chat is silent except for one burst", () => {
    const heights = normalizeSeries([...Array(60).fill(0), 5], "chat");
    expect(heights[0]).toBe(0);
    expect(heights[60]).toBe(1);
  });

  it("keeps digital silence at the baseline and out of the loudness scale", () => {
    const audible = Array.from({ length: 81 }, (_, i) => -35 + i * 0.25);
    const heights = normalizeSeries(
      [...Array(20).fill(-60), ...audible],
      "loudness",
    );
    expect(heights.slice(0, 20).every((h) => h === 0)).toBe(true);
    // -25 dBFS sits mid-track instead of squashed near the top by the silence.
    expect(heights[20 + 40]).toBeGreaterThan(0.4);
    expect(heights[20 + 40]).toBeLessThan(0.6);
    expect(heights[20]).toBe(0);
    expect(heights[20 + 80]).toBe(1);
  });

  it("draws a flat series without dividing by zero", () => {
    expect(normalizeSeries([0, 0], "chat")).toEqual([0, 0]);
    expect(normalizeSeries([-20, -20], "loudness")).toEqual([0.5, 0.5]);
    expect(normalizeSeries([-60, -60], "loudness")).toEqual([0.5, 0.5]);
  });
});

describe("seriesRuns", () => {
  it("splits at nulls", () => {
    expect(seriesRuns([null, 1, 2, null, null, 3])).toEqual([
      [1, 2],
      [5, 5],
    ]);
    expect(seriesRuns([null])).toEqual([]);
  });
});

describe("curvePaths", () => {
  it("draws one sub-path per measured run, leaving gaps", () => {
    const paths = curvePaths([1, 1, null, 0], 10, 40, { area: false });
    expect(paths).toHaveLength(2);
    // First run spans 0–20 s (0–500 in the viewBox), points at the step centres.
    expect(paths[0]).toBe("M0 24 L125 24 L375 24 L500 24");
    // The lone last point is a flat plateau across its own step, at the baseline.
    expect(paths[1]).toBe("M750 100 L875 100 L1000 100");
  });

  it("caps a curve at a lower top, anchored at the bottom", () => {
    const [line] = curvePaths([1, 0], 10, 20, {
      area: false,
      top: LOUDNESS_TOP,
    });
    expect(LOUDNESS_TOP).toBe(40); // 60% of the track's height
    expect(line).toBe("M0 40 L250 40 L750 100 L1000 100");
  });

  it("closes an area down to the baseline", () => {
    const [area] = curvePaths([0.5], 10, 10, { area: true });
    expect(area).toBe("M0 62 L500 62 L1000 62 L1000 100 L0 100 Z");
  });

  it("cuts the last step at the VOD's end and drops points past it", () => {
    const [line] = curvePaths([1, 1, 1], 10, 15, { area: false });
    expect(line).toBe("M0 24 L333.33 24 L833.33 24 L1000 24");
  });

  it("draws nothing for an all-null series or a bad step", () => {
    expect(curvePaths([null, null], 10, 20, { area: true })).toEqual([]);
    expect(curvePaths([1], 0, 20, { area: true })).toEqual([]);
  });
});

describe("drawableActivity and activitySummary", () => {
  it("is empty and silent without activity", () => {
    expect(drawableActivity(undefined)).toEqual({});
    expect(drawableActivity(null)).toEqual({});
    expect(activitySummary(undefined, 60)).toBeNull();
    expect(
      activitySummary({ step_s: 10, chat: null, loudness: null }, 60),
    ).toBeNull();
  });

  it("summarizes peaks, a missing family and gaps", () => {
    const text = activitySummary(
      { step_s: 10, chat: null, loudness: [-30, null, -10, -25] },
      40,
    );
    expect(text).toBe(
      "Activity behind the timeline: the stream is loudest around 0:25. No chat was measured. Some stretches have no loudness measurement.",
    );
  });

  it("ignores a last null point in the tail too short for a window", () => {
    const activity = { step_s: 10, chat: [1, 2, 1, null], loudness: null };
    // 34 s: the last point starts at 30 s, within half a window of the end.
    expect(drawableActivity(activity, 34).chat).toEqual([1, 2, 1]);
    expect(activitySummary(activity, 34)).not.toContain("Some stretches");
    // 40 s: a window fits there, so the null point is a real gap.
    expect(drawableActivity(activity, 40).chat).toEqual([1, 2, 1, null]);
    expect(activitySummary(activity, 40)).toContain(
      "Some stretches have no chat measurement.",
    );
  });

  it("names chat's peak rate", () => {
    expect(
      activitySummary({ step_s: 10, chat: [1, 8.06, 2], loudness: null }, 30),
    ).toContain("chat is busiest around 0:15, at 8.1 messages a second");
  });
});
