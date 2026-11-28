import { describe, expect, it } from "vitest";

import {
  adjacentMomentStart,
  momentAt,
  pctOf,
  popoverAlign,
  rankLabelAnchor,
  rulerTicks,
  spanGeometry,
  timeAtClientX,
} from "./geometry";
import { previewSourceFor, type PreviewSource } from "./use-preview-media";

describe("timeline geometry", () => {
  it("places spans at their true start and length on the duration scale", () => {
    expect(spanGeometry({ start_s: 90, end_s: 180 }, 360)).toEqual({
      leftPct: 25,
      widthPct: 25,
    });
    // A 30 s moment at the end of a 6 h VOD stays at its real time instead of shifting left.
    const late = spanGeometry({ start_s: 21570, end_s: 21600 }, 21600);
    expect(late.leftPct).toBeCloseTo((21570 / 21600) * 100, 6);
    expect(late.leftPct + late.widthPct).toBeCloseTo(100, 6);
  });

  it("clamps spans past the VOD edges and survives a zero or bad duration", () => {
    expect(spanGeometry({ start_s: 300, end_s: 400 }, 360)).toEqual({
      leftPct: (300 / 360) * 100,
      widthPct: 100 - (300 / 360) * 100,
    });
    expect(spanGeometry({ start_s: 40, end_s: 20 }, 360).widthPct).toBe(0);
    expect(pctOf(5, 0)).toBe(100);
    expect(pctOf(5, Number.NaN)).toBe(100);
    expect(pctOf(-5, 360)).toBe(0);
  });

  it("maps a pointer position to VOD time and clamps outside the track", () => {
    const rect = { left: 100, width: 400 };
    expect(timeAtClientX(300, rect, 360)).toBe(180);
    expect(timeAtClientX(50, rect, 360)).toBe(0);
    expect(timeAtClientX(900, rect, 360)).toBe(360);
    expect(timeAtClientX(300, { left: 0, width: 0 }, 360)).toBe(0);
  });

  it("finds the best-ranked moment under a time with half-open spans", () => {
    const moments = [
      { rank: 2, start_s: 100, end_s: 160 },
      { rank: 1, start_s: 130, end_s: 150 },
      { rank: 3, start_s: 300, end_s: 300 },
    ];
    expect(momentAt(110, moments)?.rank).toBe(2);
    expect(momentAt(140, moments)?.rank).toBe(1);
    expect(momentAt(150, moments)?.rank).toBe(2);
    expect(momentAt(160, moments)).toBeNull();
    expect(momentAt(300, moments)?.rank).toBe(3);
    expect(momentAt(10, moments)).toBeNull();
  });

  it("counts the VOD's last instant as inside a moment that runs to the end", () => {
    const tail = [{ rank: 1, start_s: 330, end_s: 360 }];
    expect(momentAt(360, tail)).toBeNull();
    expect(momentAt(360, tail, 360)?.rank).toBe(1);
    expect(momentAt(359.9, tail, 360)?.rank).toBe(1);
    expect(
      momentAt(360, [{ rank: 1, start_s: 300, end_s: 330 }], 360),
    ).toBeNull();
  });

  it("pins rank labels to the track's edges instead of cutting them off", () => {
    expect(rankLabelAnchor(0, 2)).toBe("left");
    expect(rankLabelAnchor(98, 2)).toBe("right");
    expect(rankLabelAnchor(40, 10)).toBe("center");
  });

  it("jumps to the next and previous moment start", () => {
    const moments = [{ start_s: 40 }, { start_s: 118 }, { start_s: 251.5 }];
    expect(adjacentMomentStart(0, moments, 1)).toBe(40);
    expect(adjacentMomentStart(40, moments, 1)).toBe(118);
    expect(adjacentMomentStart(118, moments, -1)).toBe(40);
    expect(adjacentMomentStart(251.5, moments, 1)).toBeNull();
    expect(adjacentMomentStart(40, moments, -1)).toBeNull();
  });

  it("puts ruler ticks on round steps and always ends at the VOD duration", () => {
    expect(rulerTicks(360).map((t) => t.seconds)).toEqual([0, 120, 240, 360]);
    const sixHours = rulerTicks(21600);
    expect(sixHours.map((t) => t.seconds)).toEqual([0, 7200, 14400, 21600]);
    expect(sixHours.at(-1)?.pct).toBe(100);
    // An interior tick too close to the end label is dropped.
    expect(rulerTicks(250).map((t) => t.seconds)).toEqual([0, 120, 250]);
  });

  it("keeps floating labels inside the track near its edges", () => {
    expect(popoverAlign(2)).toBe("start");
    expect(popoverAlign(50)).toBe("center");
    expect(popoverAlign(97)).toBe("end");
  });
});

describe("previewSourceFor", () => {
  const whole: PreviewSource = {
    candidateId: "c001",
    url: "/media/whole.mp4",
    startS: 0,
    endS: 360,
  };
  const segment: PreviewSource = {
    candidateId: "c002",
    url: "/media/seg.mp4",
    startS: 240,
    endS: 290,
  };

  it("prefers the preview prepared for the moment under the playhead", () => {
    expect(previewSourceFor(250, [whole, segment], "c002")).toBe(segment);
    expect(previewSourceFor(250, [whole, segment], null)).toBe(segment);
    expect(previewSourceFor(100, [whole, segment], null)).toBe(whole);
  });

  it("returns null when no preview covers the time (no frame)", () => {
    expect(previewSourceFor(100, [segment], null)).toBeNull();
    expect(previewSourceFor(100, [], "c001")).toBeNull();
  });
});
