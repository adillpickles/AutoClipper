import { describe, expect, it } from "vitest";

import {
  activitySummary,
  curvePaths,
  drawableActivity,
  normalizeSeries,
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

describe("normalizeSeries", () => {
  it("scales chat from zero to its busiest point", () => {
    expect(normalizeSeries([0, 2, null, 4], "chat")).toEqual([0, 0.5, null, 1]);
  });

  it("scales loudness between its quietest and loudest points", () => {
    expect(normalizeSeries([-40, -30, -20], "loudness")).toEqual([0, 0.5, 1]);
  });

  it("draws a flat series without dividing by zero", () => {
    expect(normalizeSeries([0, 0], "chat")).toEqual([0, 0]);
    expect(normalizeSeries([-20, -20], "loudness")).toEqual([0.5, 0.5]);
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

  it("names chat's peak rate", () => {
    expect(
      activitySummary({ step_s: 10, chat: [1, 8.06, 2], loudness: null }, 30),
    ).toContain("chat is busiest around 0:15, at 8.1 messages a second");
  });
});
