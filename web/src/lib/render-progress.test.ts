import { describe, expect, it } from "vitest";

import { renderProgressText } from "@/lib/render-progress";

describe("renderProgressText", () => {
  it("says the stage message once, with the step on its own line", () => {
    const text = renderProgressText(
      {
        stage: "rendering",
        stage_index: 3,
        stage_count: 3,
        stage_fraction: null,
        message: "Rendering the clip",
      },
      "Rendering…",
    );
    expect(text).toEqual({ label: "Rendering the clip", step: "Step 3 of 3" });
    // The QA bug: "Rendering… Rendering the clip".
    expect(`${text.label} ${text.step}`).not.toMatch(/Rendering.*Rendering/);
  });

  it("falls back before the first progress update or on an empty message", () => {
    expect(renderProgressText(null, "Rendering…")).toEqual({
      label: "Rendering…",
      step: null,
    });
    expect(
      renderProgressText(
        {
          stage: "mystery",
          stage_index: 0,
          stage_count: 0,
          stage_fraction: null,
          message: "  ",
        },
        "Starting the render…",
      ),
    ).toEqual({ label: "Starting the render…", step: null });
  });
});
