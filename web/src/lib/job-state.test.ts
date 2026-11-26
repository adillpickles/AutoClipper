// @vitest-environment jsdom

import { beforeEach, describe, expect, it } from "vitest";

import {
  clearRememberedTrim,
  isTrimReset,
  rememberExport,
  rememberTrim,
  rememberedExport,
  rememberedTrim,
  rememberedTrims,
} from "@/lib/job-state";

describe("shared job state helpers", () => {
  beforeEach(() => {
    window.localStorage.clear();
  });

  it("persists and reads export IDs per job and candidate", () => {
    expect(rememberedExport("job_1", "c001")).toBeNull();
    rememberExport("job_1", "c001", "exp_a");
    rememberExport("job_1", "c002", "exp_b");
    rememberExport("job_2", "c001", "exp_other");

    expect(rememberedExport("job_1", "c001")).toBe("exp_a");
    expect(rememberedExport("job_1", "c002")).toBe("exp_b");
    expect(rememberedExport("job_2", "c001")).toBe("exp_other");
  });

  it("persists, reads, and clears per-candidate trims independently per job", () => {
    expect(rememberedTrims("job_1")).toEqual({});
    expect(rememberedTrim("job_1", "c001")).toBeNull();

    rememberTrim("job_1", "c001", { start_s: 113, end_s: 149 });
    rememberTrim("job_1", "c002", { start_s: 250.5, end_s: 275.25 });
    rememberTrim("job_2", "c001", { start_s: 10, end_s: 20 });

    expect(rememberedTrim("job_1", "c001")).toEqual({
      start_s: 113,
      end_s: 149,
    });
    expect(rememberedTrims("job_1")).toEqual({
      c001: { start_s: 113, end_s: 149 },
      c002: { start_s: 250.5, end_s: 275.25 },
    });

    clearRememberedTrim("job_1", "c001");
    expect(rememberedTrim("job_1", "c001")).toBeNull();
    expect(rememberedTrims("job_1")).toEqual({
      c002: { start_s: 250.5, end_s: 275.25 },
    });

    clearRememberedTrim("job_1", "c002");
    expect(rememberedTrims("job_1")).toEqual({});
    expect(window.localStorage.getItem("autoclipper.trims.job_1")).toBeNull();
  });

  it("ignores invalid trims and malformed localStorage payloads safely", () => {
    rememberTrim("job_1", "c001", { start_s: 50, end_s: 40 });
    rememberTrim("job_1", "c002", { start_s: -1, end_s: 10 });
    rememberTrim("job_1", "c003", { start_s: Number.NaN, end_s: 20 });
    expect(rememberedTrims("job_1")).toEqual({});

    window.localStorage.setItem(
      "autoclipper.trims.job_1",
      JSON.stringify({
        good: { start_s: 12, end_s: 34 },
        reversed: { start_s: 34, end_s: 12 },
        badType: "oops",
      }),
    );
    expect(rememberedTrims("job_1")).toEqual({
      good: { start_s: 12, end_s: 34 },
    });

    window.localStorage.setItem("autoclipper.trims.job_1", "{not-json");
    expect(rememberedTrims("job_1")).toEqual({});
  });

  it("records explicit reset markers without exposing them as saved trims", () => {
    rememberTrim("job_1", "c001", { start_s: 113, end_s: 148 });
    expect(isTrimReset("job_1", "c001")).toBe(false);

    clearRememberedTrim("job_1", "c001", { recordReset: true });
    expect(rememberedTrim("job_1", "c001")).toBeNull();
    expect(isTrimReset("job_1", "c001")).toBe(true);

    // Re-trimming clears the reset marker.
    rememberTrim("job_1", "c001", { start_s: 115, end_s: 148 });
    expect(isTrimReset("job_1", "c001")).toBe(false);
    expect(rememberedTrim("job_1", "c001")).toEqual({
      start_s: 115,
      end_s: 148,
    });
  });
});
