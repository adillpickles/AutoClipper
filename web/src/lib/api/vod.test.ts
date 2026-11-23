import { describe, expect, it } from "vitest";

import { looksAbsolute, parseVod, vodInputMessage } from "@/lib/api/vod";

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

describe("vodInputMessage", () => {
  it("accepts a parsed VOD link", () => {
    expect(vodInputMessage(" twitch.tv/videos/2345678901 ")).toBeNull();
  });

  it("asks for a link when the field is empty", () => {
    expect(vodInputMessage("  ")).toMatch(/Paste a Twitch VOD link/);
  });

  it("explains a channel page, a clip link, a player embed, and a YouTube link", () => {
    expect(vodInputMessage("https://www.twitch.tv/somechannel")).toMatch(
      /channel page/,
    );
    expect(
      vodInputMessage("https://www.twitch.tv/somechannel/clip/SomeSlug"),
    ).toMatch(/clip link/);
    expect(vodInputMessage("https://clips.twitch.tv/AbcdEfgh")).toMatch(
      /clip link/,
    );
    expect(vodInputMessage("https://player.twitch.tv/?video=v12345")).toMatch(
      /player embed/,
    );
    expect(vodInputMessage("https://youtu.be/dQw4w9WgXcQ")).toMatch(
      /Twitch VODs/,
    );
    expect(vodInputMessage("https://example.com/clips.twitch.tv")).toMatch(
      /isn't a Twitch VOD link/,
    );
  });

  it("falls back to the server's invalid-link message", () => {
    expect(vodInputMessage("not a link")).toMatch(/isn't a Twitch VOD link/);
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
