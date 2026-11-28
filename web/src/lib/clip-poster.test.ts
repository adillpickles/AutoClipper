import { describe, expect, it } from "vitest";

import { createLimiter, posterTime } from "@/lib/clip-poster";

describe("posterTime", () => {
  it("picks a frame a little way into the clip", () => {
    expect(posterTime(30)).toBe(1.5);
    expect(posterTime(2)).toBeCloseTo(0.6);
    expect(posterTime(0)).toBe(0);
    expect(posterTime(Number.NaN)).toBe(0);
    expect(posterTime(Number.POSITIVE_INFINITY)).toBe(0);
  });
});

describe("createLimiter", () => {
  it("runs at most `limit` tasks at once, in order, and keeps going after failures", async () => {
    const run = createLimiter(2);
    let active = 0;
    let peak = 0;
    const order: number[] = [];
    const task =
      (id: number, fail = false) =>
      async () => {
        active += 1;
        peak = Math.max(peak, active);
        order.push(id);
        await new Promise((resolve) => setTimeout(resolve, 5));
        active -= 1;
        if (fail) throw new Error(`task ${id}`);
        return id;
      };
    const results = await Promise.allSettled([
      run(task(1)),
      run(task(2, true)),
      run(task(3)),
      run(task(4)),
      run(task(5)),
    ]);
    expect(peak).toBe(2);
    expect(order).toEqual([1, 2, 3, 4, 5]);
    expect(results.map((r) => r.status)).toEqual([
      "fulfilled",
      "rejected",
      "fulfilled",
      "fulfilled",
      "fulfilled",
    ]);
  });
});
