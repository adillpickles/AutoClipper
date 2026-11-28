import { describe, expect, it } from "vitest";

import { momentsSummary, TOP_CLIPS } from "@/lib/top-clips";

describe("momentsSummary", () => {
  it("tells one story with the results screen's top 10", () => {
    expect(TOP_CLIPS).toBe(10);
    expect(momentsSummary(82)).toBe("Scored 82 moments · picked the top 10.");
    expect(momentsSummary(10)).toBe("Picked the top 10 moments.");
    expect(momentsSummary(3)).toBe("Picked the top 3 moments.");
    expect(momentsSummary(1)).toBe("Picked the top moment.");
  });
});
