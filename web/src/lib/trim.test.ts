import { describe, expect, it } from "vitest";

import {
  clampTrim,
  nudgeEnd,
  nudgeStart,
  trimError,
  unavailableReasonLabel,
} from "@/lib/trim";

const BOUNDS = { minStartS: 80, maxEndS: 200, maxDurationS: 90 };

describe("trim helpers", () => {
  it("clamps start, end, and maxDurationS into allowed export bounds", () => {
    expect(clampTrim(50, 250, BOUNDS)).toEqual({ startS: 80, endS: 170 });
    expect(clampTrim(50, 250, BOUNDS, "end")).toEqual({
      startS: 110,
      endS: 200,
    });
    expect(clampTrim(120, 120, BOUNDS)).toEqual({ startS: 120, endS: 120.5 });
    expect(clampTrim(200, 100, BOUNDS)).toEqual({ startS: 199.5, endS: 200 });
  });

  it("nudges start and end without crossing each other or exceeding maxDurationS", () => {
    expect(nudgeStart({ startS: 118, endS: 148 }, -5, BOUNDS)).toEqual({
      startS: 113,
      endS: 148,
    });
    expect(nudgeStart({ startS: 82, endS: 148 }, -5, BOUNDS)).toEqual({
      startS: 80,
      endS: 148,
    });
    expect(nudgeStart({ startS: 147, endS: 148 }, 5, BOUNDS)).toEqual({
      startS: 147.5,
      endS: 148,
    });
    // Clamps to endS - maxDurationS (190 - 90 = 100) rather than expanding past 90s.
    expect(nudgeStart({ startS: 102, endS: 190 }, -5, BOUNDS)).toEqual({
      startS: 100,
      endS: 190,
    });

    expect(nudgeEnd({ startS: 118, endS: 148 }, 5, BOUNDS)).toEqual({
      startS: 118,
      endS: 153,
    });
    expect(nudgeEnd({ startS: 118, endS: 198 }, 5, BOUNDS)).toEqual({
      startS: 118,
      endS: 200,
    });
    expect(nudgeEnd({ startS: 118, endS: 119 }, -5, BOUNDS)).toEqual({
      startS: 118,
      endS: 118.5,
    });
    // Clamps to startS + maxDurationS (90 + 90 = 180) rather than expanding past 90s.
    expect(nudgeEnd({ startS: 90, endS: 178 }, 5, BOUNDS)).toEqual({
      startS: 90,
      endS: 180,
    });
  });

  it("never exceeds maxDurationS due to float rounding or multi-decimal edges", () => {
    const wide = { minStartS: 0, maxEndS: 360, maxDurationS: 90 };
    // 128.3 - 38.3 === 90.00000000000001 in IEEE-754; clamped end must still pass trimError.
    const clampedEnd = clampTrim(38.3, 200, wide, "start");
    expect(clampedEnd.endS - clampedEnd.startS).toBeLessThanOrEqual(90);
    expect(trimError(clampedEnd, wide)).toBeNull();

    const nudgedEnd = nudgeEnd({ startS: 38.349, endS: 125 }, 10, wide);
    expect(nudgedEnd.endS - nudgedEnd.startS).toBeLessThanOrEqual(90);
    expect(trimError(nudgedEnd, wide)).toBeNull();

    const nudgedStart = nudgeStart({ startS: 60, endS: 148.349 }, -10, wide);
    expect(nudgedStart.endS - nudgedStart.startS).toBeLessThanOrEqual(90);
    expect(trimError(nudgedStart, wide)).toBeNull();
  });

  it("preserves exact fractional media limits instead of rounding past them", () => {
    const probed = { minStartS: 10.04, maxEndS: 159.96, maxDurationS: 90 };

    // Nudging end to a probed limit of 159.96 must keep 159.96 (never round to 160.0).
    const nudgedEnd = nudgeEnd({ startS: 120, endS: 158 }, 5, probed);
    expect(nudgedEnd).toEqual({ startS: 120, endS: 159.96 });
    expect(trimError(nudgedEnd, probed)).toBeNull();

    // Nudging start to a fractional minStartS of 10.04 must keep 10.04 (never round to 10.0).
    const nudgedStart = nudgeStart({ startS: 12, endS: 40 }, -5, probed);
    expect(nudgedStart).toEqual({ startS: 10.04, endS: 40 });
    expect(trimError(nudgedStart, probed)).toBeNull();

    // Clamping values past or near fractional media limits keeps exact limits valid.
    expect(clampTrim(120, 170, probed, "start")).toEqual({
      startS: 120,
      endS: 159.96,
    });
    expect(clampTrim(120, 159.95, probed, "start")).toEqual({
      startS: 120,
      endS: 159.96,
    });
    expect(clampTrim(5, 40, probed, "end")).toEqual({
      startS: 10.04,
      endS: 40,
    });
    expect(clampTrim(159.8, 159.96, probed, "end")).toEqual({
      startS: 159.46,
      endS: 159.96,
    });
    expect(
      trimError(clampTrim(159.8, 159.96, probed, "end"), probed),
    ).toBeNull();
  });

  it("enforces the 0.5 s vertical clip minimum in both clampTrim and trimError", () => {
    // Clamping a manually entered 0.1 s selection widens it to 0.5 s respecting anchor.
    expect(clampTrim(120, 120.1, BOUNDS, "start")).toEqual({
      startS: 120,
      endS: 120.5,
    });
    expect(clampTrim(120.4, 120.5, BOUNDS, "end")).toEqual({
      startS: 120,
      endS: 120.5,
    });

    // Direct validation rejects clips shorter than 0.5 s and accepts the 0.5 s boundary
    // including float subtraction like 4.1 - 3.6 = 0.49999999999999956.
    expect(trimError({ startS: 120, endS: 120.1 }, BOUNDS)).toContain(
      "at least 0.5 seconds",
    );
    expect(trimError({ startS: 120, endS: 120.49 }, BOUNDS)).toContain(
      "at least 0.5 seconds",
    );
    expect(trimError({ startS: 120, endS: 120.5 }, BOUNDS)).toBeNull();
    expect(
      trimError(
        { startS: 3.6, endS: 4.1 },
        { minStartS: 0, maxEndS: 10, maxDurationS: 90 },
      ),
    ).toBeNull();

    // Fractional bounds are formatted without rounding past the actual limit.
    expect(
      trimError(
        { startS: 120, endS: 160 },
        { minStartS: 80, maxEndS: 159.96, maxDurationS: 90 },
      ),
    ).toContain("159.96s");
    expect(
      trimError(
        { startS: 120, endS: 159.93 },
        { minStartS: 80, maxEndS: 159.926, maxDurationS: 90 },
      ),
    ).toContain("159.926s");
  });

  it("reports validation errors for out-of-range, reversed, and too-long trims", () => {
    expect(trimError({ startS: 118, endS: 148 }, BOUNDS)).toBeNull();
    expect(trimError({ startS: 75, endS: 120 }, BOUNDS)).toContain("80.0s");
    expect(trimError({ startS: 148, endS: 118 }, BOUNDS)).toContain(
      "after start time",
    );
    expect(trimError({ startS: 80, endS: 185 }, BOUNDS)).toContain(
      "at most 90 seconds",
    );
  });

  it("formats contract unavailable reasons with a safe default for unknown values", () => {
    expect(unavailableReasonLabel("source_unavailable")).toBe(
      "Source unavailable",
    );
    expect(unavailableReasonLabel("local_file_missing")).toBe(
      "Local file missing",
    );
    expect(unavailableReasonLabel("fetch_failed")).toBe("Download failed");
    expect(unavailableReasonLabel("new_future_reason")).toBe(
      "Video unavailable",
    );
    expect(unavailableReasonLabel(null)).toBe("Video unavailable");
  });
});
