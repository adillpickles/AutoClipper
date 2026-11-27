import { describe, expect, it } from "vitest";

import {
  buildClipFilename,
  buildHighlightsFilename,
  deduplicateFilename,
  formatOffsetHms,
  formatVodDate,
  sanitizeFilenameComponent,
} from "@/lib/filenames";

describe("export filename helpers", () => {
  it.each([
    ["user_001", "vod", "user_001"],
    ["Loud audio & laughing chat", "rank-1", "Loud-audio-laughing-chat"],
    ['bad<>:"/\\|?*chars', "clip", "bad-chars"],
    ["CON", "clip", "_CON"],
    ["nul", "clip", "_nul"],
    ["COM\u00b9", "clip", "_COM1"],
    ["Café stream!", "vod", "Café-stream"],
    ["加藤純一", "vod", "加藤純一"],
    ["\u202eevil\x00name", "clip", "evil-name"],
    ["", "rank-2", "rank-2"],
    ["🎮🔥", "rank-4", "rank-4"],
  ])("sanitizes %j to %j", (raw, fallback, expected) => {
    expect(sanitizeFilenameComponent(raw, fallback)).toBe(expected);
  });

  it("formats dates in UTC and offsets in HH-MM-SS", () => {
    expect(formatVodDate("2026-10-13T02:30:00+05:00")).toBe("2026-10-12");
    expect(formatVodDate(null)).toBe("unknown-date");
    expect(formatOffsetHms(118)).toBe("00-01-58");
    expect(formatOffsetHms(3661.9)).toBe("01-01-01");
    expect(formatOffsetHms(-3)).toBe("00-00-00");
  });

  it("builds vertical clip and highlights filenames", () => {
    expect(
      buildClipFilename({
        channel: "User 001",
        createdAt: "2026-10-12T18:00:00Z",
        title: "Loud audio & laughing chat",
        rank: 1,
        startS: 118,
        fallbackId: "9000000001",
      }),
    ).toBe("User-001_2026-10-12_Loud-audio-laughing-chat_00-01-58.mp4");

    expect(
      buildClipFilename({
        channel: null,
        createdAt: null,
        title: null,
        rank: 3,
        startS: 40,
        fallbackId: "9000000001",
      }),
    ).toBe("9000000001_unknown-date_rank-3_00-00-40.mp4");

    expect(
      buildHighlightsFilename({
        channel: "User_001",
        createdAt: "2026-10-12T18:00:00Z",
        fallbackId: "9000000001",
      }),
    ).toBe("User_001_2026-10-12_highlights.mp4");
    const boundedHighlights = buildHighlightsFilename({
      channel: "ExtremelyLongStreamerName",
      createdAt: "2026-10-12T18:00:00Z",
      fallbackId: "9000000001",
      maxBytes: 32,
    });
    expect(boundedHighlights.endsWith("_2026-10-12_highlights.mp4")).toBe(true);
    expect(
      new TextEncoder().encode(boundedHighlights).length,
    ).toBeLessThanOrEqual(32);

    expect(() =>
      buildHighlightsFilename({
        channel: "User",
        createdAt: "2026-10-12T18:00:00Z",
        fallbackId: "vod",
        maxBytes: 25,
      }),
    ).toThrow(RangeError);
    expect(sanitizeFilenameComponent("e\u200b\u0301clair")).toBe("éclair");
    expect(sanitizeFilenameComponent("e\u034f\ufe0f\u0301clair")).toBe(
      "éclair",
    );
  });

  it("deduplicates colliding names case-insensitively and across NFC/NFD", () => {
    const used = new Set<string>();
    expect(deduplicateFilename("User_2026-10-12_Clip_00-01-00.mp4", used)).toBe(
      "User_2026-10-12_Clip_00-01-00.mp4",
    );
    expect(deduplicateFilename("user_2026-10-12_clip_00-01-00.mp4", used)).toBe(
      "user_2026-10-12_clip_00-01-00_2.mp4",
    );

    const unicodeUsed = new Set<string>();
    expect(deduplicateFilename("e\u0301clair.mp4", unicodeUsed)).toBe(
      "e\u0301clair.mp4",
    );
    expect(deduplicateFilename("\u00e9clair.mp4", unicodeUsed)).toBe(
      "\u00e9clair_2.mp4",
    );
    expect(deduplicateFilename("\u03a3.mp4", unicodeUsed)).toBe("\u03a3.mp4");
    expect(deduplicateFilename("\u03c2.mp4", unicodeUsed)).toBe("\u03c2_2.mp4");
    expect(deduplicateFilename("\u03c3.mp4", unicodeUsed)).toBe("\u03c3_3.mp4");
    expect(deduplicateFilename("ᾀ.mp4", unicodeUsed)).toBe("ᾀ.mp4");
    expect(deduplicateFilename("ἀι.mp4", unicodeUsed)).toBe("ἀι_2.mp4");
    expect(deduplicateFilename("ẞ.mp4", unicodeUsed)).toBe("ẞ.mp4");
    expect(deduplicateFilename("ss.mp4", unicodeUsed)).toBe("ss_2.mp4");
    expect(deduplicateFilename("I.mp4", unicodeUsed)).toBe("I.mp4");
    expect(deduplicateFilename("ı.mp4", unicodeUsed)).toBe("ı.mp4");
    expect(deduplicateFilename("Ꭰ.mp4", unicodeUsed)).toBe("Ꭰ.mp4");
    expect(deduplicateFilename("ꭰ.mp4", unicodeUsed)).toBe("ꭰ_2.mp4");
  });
});
