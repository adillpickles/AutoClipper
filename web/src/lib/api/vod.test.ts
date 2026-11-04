import { describe, expect, it } from "vitest";

import { looksAbsolute, parseVod } from "@/lib/api/vod";

describe("parseVod", () => {
  it.each([
    ["9000000001", "9000000001"],
    ["  https://www.twitch.tv/videos/9000000001  ", "9000000001"],
    ["twitch.tv/videos/9000000001/", "9000000001"],
    ["HTTP://M.TWITCH.TV/videos/9000000001?t=1h2m3s", "9000000001"],
    ["https://twitch.tv/videos/9000000001#chat", "9000000001"],
  ])("accepts %s", (input, id) => {
    expect(parseVod(input)).toBe(id);
  });

  it.each([
    "twitch.tv/user_001",
    "v9000000001",
    "https://clips.twitch.tv/Abc",
    "https://www.twitch.tv/user_001/video/9000000001",
    "",
    "https://evil.example/twitch.tv/videos/1",
  ])("rejects %s", (input) => {
    expect(parseVod(input)).toBeNull();
  });
});

describe("looksAbsolute", () => {
  it.each([
    "/Users/me/vod.mp4",
    "C:\\Videos\\vod.mp4",
    "d:/vod.mp4",
    "\\\\server\\share\\vod.mp4",
  ])("accepts %s", (path) => expect(looksAbsolute(path)).toBe(true));

  it.each(["vod.mp4", "./vod.mp4", "Videos\\vod.mp4"])("rejects %s", (path) =>
    expect(looksAbsolute(path)).toBe(false),
  );
});
