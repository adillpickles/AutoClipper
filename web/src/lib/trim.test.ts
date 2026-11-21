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
  it("clamps start and end into allowed export bounds", () => {
    expect(clampTrim(50, 250, BOUNDS)).toEqual({ startS: 80, endS: 200 });
    expect(clampTrim(120, 120, BOUNDS)).toEqual({ startS: 120, endS: 120.5 });
    expect(clampTrim(200, 100, BOUNDS)).toEqual({ startS: 199.5, endS: 200 });
  });

  it("nudges start and end without crossing each other or leaving bounds", () => {
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
