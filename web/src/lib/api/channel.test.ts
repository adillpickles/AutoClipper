import { describe, expect, it } from "vitest";

import { channelInputMessage, parseChannel } from "@/lib/api/channel";

describe("parseChannel", () => {
  it.each([
    ["user_001", "user_001"],
    ["  User_001  ", "user_001"],
    ["@User_001", "user_001"],
    ["https://www.twitch.tv/user_001", "user_001"],
    ["http://twitch.tv/User_001/", "user_001"],
    ["twitch.tv/user_001", "user_001"],
    ["m.twitch.tv/User_001/videos?filter=archives&sort=time", "user_001"],
    ["https://www.twitch.tv/user_001/about#bio", "user_001"],
    ["https://www.twitch.tv/user_001/home", "user_001"],
    ["https://www.twitch.tv/user_001/schedule", "user_001"],
    ["https://www.twitch.tv/user_001/clips", "user_001"],
  ])("parses %s as %s", (raw, expected) => {
    expect(parseChannel(raw)).toBe(expected);
    expect(channelInputMessage(raw)).toBeNull();
  });

  it.each([
    "",
    "   ",
    "9000000001",
    "https://www.twitch.tv/videos/9000000001",
    "twitch.tv/videos",
    "https://www.twitch.tv/directory",
    "https://clips.twitch.tv/SomeClip",
    "https://www.twitch.tv/user_001/clip/SomeClip",
    "https://www.youtube.com/@user_001",
    "bad name",
    "a".repeat(26),
    "_leading",
  ])("rejects %s", (raw) => {
    expect(parseChannel(raw)).toBeNull();
    expect(channelInputMessage(raw)).not.toBeNull();
  });

  it("explains VOD links, clip links and other platforms specifically", () => {
    expect(
      channelInputMessage("https://www.twitch.tv/videos/9000000001"),
    ).toMatch(/VOD link/);
    expect(channelInputMessage("9000000001")).toMatch(/VOD link/);
    expect(channelInputMessage("https://clips.twitch.tv/ClipSlug")).toMatch(
      /clip link/i,
    );
    expect(channelInputMessage("https://www.youtube.com/@someone")).toMatch(
      /Twitch channels/,
    );
  });
});
