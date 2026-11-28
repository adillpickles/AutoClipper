import { describe, expect, it, vi } from "vitest";

import {
  buildShareCaption,
  copyText,
  loadShareFile,
  menuPosition,
  shareCapability,
  shareVideo,
  twitchTimestamp,
  vodTimestampUrl,
  type ShareEnvironment,
} from "@/lib/share";

function domError(name: string): Error {
  const error = new Error(name);
  error.name = name;
  return error;
}

describe("twitchTimestamp", () => {
  it.each([
    [0, "0s"],
    [5, "5s"],
    [59, "59s"],
    [60, "1m0s"],
    [61, "1m1s"],
    [3599, "59m59s"],
    [3600, "1h0m0s"],
    [3605, "1h0m5s"],
    [3723, "1h2m3s"],
    [36000 + 59 * 60 + 59, "10h59m59s"],
  ])("formats %d s as %s", (seconds, expected) => {
    expect(twitchTimestamp(seconds)).toBe(expected);
  });

  it("rounds fractions down so the link never starts after the moment", () => {
    expect(twitchTimestamp(61.999)).toBe("1m1s");
  });

  it("clamps negative and non-finite offsets to 0s", () => {
    expect(twitchTimestamp(-4)).toBe("0s");
    expect(twitchTimestamp(Number.NaN)).toBe("0s");
    expect(twitchTimestamp(Number.POSITIVE_INFINITY)).toBe("0s");
  });
});

describe("vodTimestampUrl", () => {
  it("links the VOD at the moment's start", () => {
    expect(vodTimestampUrl("9000000001", 3723.4)).toBe(
      "https://www.twitch.tv/videos/9000000001?t=1h2m3s",
    );
  });

  it("links the whole VOD without a start", () => {
    expect(vodTimestampUrl("9000000001")).toBe(
      "https://www.twitch.tv/videos/9000000001",
    );
    expect(vodTimestampUrl("9000000001", null)).toBe(
      "https://www.twitch.tv/videos/9000000001",
    );
  });

  it("keeps a start of zero", () => {
    expect(vodTimestampUrl("42", 0)).toBe(
      "https://www.twitch.tv/videos/42?t=0s",
    );
  });

  it("returns null for IDs that aren't Twitch VOD IDs", () => {
    expect(vodTimestampUrl("local_abc")).toBeNull();
    expect(vodTimestampUrl("v123")).toBeNull();
    expect(vodTimestampUrl("")).toBeNull();
  });
});

describe("buildShareCaption", () => {
  it("has the title, channel and timestamp link", () => {
    expect(
      buildShareCaption({
        title: "Chat lost it",
        channel: "Streamer",
        videoId: "9000000001",
        startS: 125,
      }),
    ).toBe(
      "Chat lost it · Streamer on Twitch\nWatch it on the VOD: https://www.twitch.tv/videos/9000000001?t=2m5s",
    );
  });

  it("drops a missing channel and a link it can't build", () => {
    expect(
      buildShareCaption({ title: "Big play", channel: " ", videoId: "x_1" }),
    ).toBe("Big play");
  });

  it("falls back to a generic title", () => {
    expect(buildShareCaption({ title: "  ", videoId: "x" })).toBe(
      "Stream moment",
    );
  });
});

describe("shareCapability", () => {
  it("is none without navigator.share", () => {
    expect(shareCapability({})).toBe("none");
  });

  it("is files when canShare accepts an MP4", () => {
    const canShare = vi.fn((data?: ShareData) => Boolean(data?.files));
    expect(shareCapability({ share: vi.fn(), canShare })).toBe("files");
  });

  it("is text when canShare rejects or throws on files", () => {
    expect(shareCapability({ share: vi.fn(), canShare: () => false })).toBe(
      "text",
    );
    expect(
      shareCapability({
        share: vi.fn(),
        canShare: () => {
          throw new TypeError("no files");
        },
      }),
    ).toBe("text");
  });

  it("is text when there is share but no canShare", () => {
    expect(shareCapability({ share: vi.fn() })).toBe("text");
  });
});

describe("loadShareFile", () => {
  const mp4 = () =>
    new Response(new Blob([new Uint8Array([1, 2, 3])]), {
      status: 200,
      headers: { "Content-Type": "video/mp4" },
    });

  it("loads the MP4 as a named video/mp4 File", async () => {
    const fetchMock = vi.fn(async () => mp4());
    const file = await loadShareFile("/api/v1/exports/e/file", "clip.mp4", {
      fetch: fetchMock as unknown as typeof fetch,
    });
    expect(fetchMock).toHaveBeenCalledWith("/api/v1/exports/e/file", {
      signal: undefined,
    });
    expect(file?.name).toBe("clip.mp4");
    expect(file?.type).toBe("video/mp4");
    expect(file?.size).toBe(3);
  });

  it("is null on an HTTP error, a network/CORS error or without fetch", async () => {
    const notFound = vi.fn(async () => new Response("", { status: 404 }));
    expect(
      await loadShareFile("/f", "c.mp4", {
        fetch: notFound as unknown as typeof fetch,
      }),
    ).toBeNull();
    const offline = vi.fn(async () => {
      throw new TypeError("Failed to fetch");
    });
    expect(
      await loadShareFile("/f", "c.mp4", {
        fetch: offline as unknown as typeof fetch,
      }),
    ).toBeNull();
    expect(await loadShareFile("/f", "c.mp4", {})).toBeNull();
  });

  it("passes the abort signal and is null once aborted", async () => {
    const controller = new AbortController();
    const fetchMock = vi.fn(async (_url: string, init?: RequestInit) => {
      controller.abort();
      if (init?.signal?.aborted) throw domError("AbortError");
      return mp4();
    });
    expect(
      await loadShareFile(
        "/f",
        "c.mp4",
        { fetch: fetchMock as unknown as typeof fetch },
        undefined,
        controller.signal,
      ),
    ).toBeNull();
    expect(fetchMock.mock.calls[0][1]?.signal).toBe(controller.signal);
  });

  it("is null when the file is larger than the limit", async () => {
    const declared = vi.fn(
      async () =>
        new Response("abc", { headers: { "Content-Length": "999999" } }),
    );
    expect(
      await loadShareFile(
        "/f",
        "c.mp4",
        { fetch: declared as unknown as typeof fetch },
        10,
      ),
    ).toBeNull();
    const big = vi.fn(async () => mp4());
    expect(
      await loadShareFile(
        "/f",
        "c.mp4",
        { fetch: big as unknown as typeof fetch },
        2,
      ),
    ).toBeNull();
  });
});

describe("shareVideo", () => {
  const file = new File([new Uint8Array([1])], "clip.mp4", {
    type: "video/mp4",
  });
  const base = {
    title: "Chat lost it",
    caption: "Chat lost it\nhttps://www.twitch.tv/videos/1?t=5s",
    link: "https://www.twitch.tv/videos/1?t=5s",
  };

  it("shares the file when canShare accepts it", async () => {
    const share = vi.fn(async () => {});
    const env: ShareEnvironment = { share, canShare: () => true };
    expect(await shareVideo({ ...base, file, env })).toEqual({
      kind: "shared-file",
    });
    expect(share).toHaveBeenCalledWith({
      files: [file],
      title: base.title,
      text: base.caption,
    });
  });

  it("falls back to text when files are refused", async () => {
    const share = vi.fn(async () => {});
    const canShare = vi.fn((data?: ShareData) => !data?.files);
    expect(
      await shareVideo({ ...base, file, env: { share, canShare } }),
    ).toEqual({ kind: "shared-text" });
    // The caption already carries the link, so it isn't sent twice.
    expect(share).toHaveBeenCalledWith({
      title: base.title,
      text: base.caption,
    });
  });

  it("adds the link as url when the caption lacks it", async () => {
    const share = vi.fn(async () => {});
    await shareVideo({
      ...base,
      caption: "Chat lost it",
      file: null,
      env: { share },
    });
    expect(share).toHaveBeenCalledWith({
      title: base.title,
      text: "Chat lost it",
      url: base.link,
    });
  });

  it("shares text when the file couldn't be loaded", async () => {
    const share = vi.fn(async (data: ShareData) => {
      void data;
    });
    expect(
      await shareVideo({
        ...base,
        file: null,
        env: { share, canShare: () => true },
      }),
    ).toEqual({ kind: "shared-text" });
    expect(share.mock.calls[0][0]).not.toHaveProperty("files");
  });

  it("is unsupported without navigator.share or when canShare refuses text", async () => {
    expect(await shareVideo({ ...base, file, env: {} })).toEqual({
      kind: "unsupported",
    });
    const share = vi.fn(async () => {});
    expect(
      await shareVideo({
        ...base,
        file,
        env: { share, canShare: () => false },
      }),
    ).toEqual({ kind: "unsupported" });
    expect(share).not.toHaveBeenCalled();
  });

  it("reports a cancelled share sheet as cancelled, not shared", async () => {
    const share = vi.fn(async () => {
      throw domError("AbortError");
    });
    expect(
      await shareVideo({
        ...base,
        file,
        env: { share, canShare: () => true },
      }),
    ).toEqual({ kind: "cancelled" });
  });

  it("asks to retry when the activation expired, and fails on other errors", async () => {
    const notAllowed = vi.fn(async () => {
      throw domError("NotAllowedError");
    });
    expect(
      await shareVideo({ ...base, file: null, env: { share: notAllowed } }),
    ).toEqual({ kind: "retry" });
    const broken = vi.fn(async () => {
      throw domError("DataError");
    });
    expect(
      await shareVideo({ ...base, file: null, env: { share: broken } }),
    ).toEqual({ kind: "failed" });
  });
});

describe("copyText", () => {
  it("uses the Clipboard API", async () => {
    const writeText = vi.fn(async () => {});
    expect(await copyText("hi", { writeText })).toBe(true);
    expect(writeText).toHaveBeenCalledWith("hi");
  });

  it("is false when the clipboard rejects and there is no document", async () => {
    const writeText = vi.fn(async () => {
      throw domError("NotAllowedError");
    });
    expect(await copyText("hi", { writeText })).toBe(false);
    expect(await copyText("hi", {})).toBe(false);
  });
});

describe("menuPosition", () => {
  const viewport = { width: 375, height: 667 };
  const menu = { width: 256, height: 200 };
  const trigger = (left: number, top: number) => ({
    left,
    right: left + 90,
    top,
    bottom: top + 40,
  });

  it("opens below, lined up with the trigger's start", () => {
    expect(
      menuPosition(trigger(20, 100), menu, viewport, "bottom", "start"),
    ).toEqual({ left: 20, top: 148 });
  });

  it("lines up with the trigger's end edge", () => {
    expect(
      menuPosition(trigger(200, 100), menu, viewport, "bottom", "end"),
    ).toEqual({ left: 34, top: 148 });
  });

  it("keeps the menu inside a 375 px viewport on both edges", () => {
    expect(
      menuPosition(trigger(10, 100), menu, viewport, "bottom", "end").left,
    ).toBe(8);
    expect(
      menuPosition(trigger(300, 100), menu, viewport, "bottom", "start").left,
    ).toBe(375 - 8 - 256);
  });

  it("flips to the side that has room", () => {
    expect(
      menuPosition(trigger(20, 600), menu, viewport, "bottom", "start").top,
    ).toBe(600 - 8 - 200);
    expect(
      menuPosition(trigger(20, 50), menu, viewport, "top", "start").top,
    ).toBe(98);
  });

  it("clamps into the viewport when neither side has room", () => {
    const tall = { width: 256, height: 640 };
    expect(
      menuPosition(trigger(20, 300), tall, viewport, "bottom", "start").top,
    ).toBe(667 - 8 - 640);
  });
});
