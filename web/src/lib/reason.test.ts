import { describe, expect, it } from "vitest";

import { reasonFor, signalDetailsFor, titleFor } from "@/lib/reason";

describe("reasonFor", () => {
  it.each([
    [
      ["chat_msg_rate_z", "audio_rms_max_z"],
      3,
      "Chat exploded and the streamer got loud",
    ],
    [["audio_rms_max_z"], 0, "The streamer got loud"],
    [
      ["chat_msg_rate_z", "chat_msg_rate_lead1", "audio_rms_rz"],
      0,
      "Chat exploded and the streamer got loud",
    ],
    [["chat_unique_rate_z"], 0, "Lots of viewers reacted"],
    [["something_new"], 2, "Viewers clipped this moment"],
    [["something_new"], 0, "Chat and audio stood out here"],
    [
      ["audio_evt_scream", "chat_msg_rate_z"],
      0,
      "The streamer screamed and chat exploded",
    ],
    [["chat_keyword_rate"], 0, "Chat called for a clip"],
    [[], 1, "Viewers clipped this moment"],
    [[], 0, "Steady activity throughout"],
  ])("%j with %i clips reads %s", (signals, clips, reason) => {
    expect(reasonFor({ top_signals: signals, n_viewer_clips: clips })).toBe(
      reason,
    );
  });

  it("prefers server-supplied reason over top_signals fallback", () => {
    expect(
      reasonFor({
        top_signals: ["chat_msg_rate_z"],
        n_viewer_clips: 3,
        reason:
          "Chat jumped to 6× its usual rate with lots of laugh reactions; 3 viewer clips",
      }),
    ).toBe(
      "Chat jumped to 6× its usual rate with lots of laugh reactions; 3 viewer clips",
    );
  });

  it("falls back when server reason is null or whitespace", () => {
    expect(
      reasonFor({
        top_signals: ["audio_rms_max_z"],
        n_viewer_clips: 0,
        reason: "   ",
      }),
    ).toBe("The streamer got loud");
  });
});

describe("titleFor", () => {
  it("prefers server-supplied title when present", () => {
    expect(
      titleFor({
        top_signals: ["chat_msg_rate_z"],
        n_viewer_clips: 0,
        title: "Loud audio & laughing chat",
      }),
    ).toBe("Loud audio & laughing chat");
  });

  it.each([
    [["chat_msg_rate_z", "audio_rms_max_z"], 0, "Loud audio & chat spike"],
    [["chat_msg_rate_z"], 0, "Chat surge"],
    [["audio_rms_max_z"], 0, "Loud audio spike"],
    [[], 3, "Popular viewer clip"],
    [[], 1, "Viewer-clipped moment"],
    [[], 0, "Stream moment"],
  ])("falls back for %j with %i clips to %s", (signals, clips, expected) => {
    expect(titleFor({ top_signals: signals, n_viewer_clips: clips })).toBe(
      expected,
    );
  });
});

describe("signalDetailsFor", () => {
  it("prefers server-supplied signal_details", () => {
    const serverDetails = [
      {
        kind: "chat_rate",
        label: "Chat rate",
        detail: "6× usual stream rate (z=3.4)",
      },
      {
        kind: "viewer_clips",
        label: "Viewer clips",
        detail: "3 viewer clips overlap this moment",
      },
    ];
    expect(
      signalDetailsFor({
        top_signals: ["chat_msg_rate_z"],
        n_viewer_clips: 3,
        signal_details: serverDetails,
      }),
    ).toEqual(serverDetails);
  });

  it("derives deduplicated fallback details from top_signals and viewer clips", () => {
    expect(
      signalDetailsFor({
        top_signals: [
          "chat_msg_rate_z",
          "chat_msg_rate_lead1",
          "audio_rms_max_z",
        ],
        n_viewer_clips: 2,
      }),
    ).toEqual([
      { kind: "chat_rate", label: "Chat rate", detail: "Chat exploded" },
      {
        kind: "audio_loudness",
        label: "Audio loudness",
        detail: "The streamer got loud",
      },
      {
        kind: "viewer_clips",
        label: "Viewer clips",
        detail: "2 viewer clips overlap this moment",
      },
    ]);
  });

  it("returns empty array when neither signals nor viewer clips exist", () => {
    expect(signalDetailsFor({ top_signals: [], n_viewer_clips: 0 })).toEqual(
      [],
    );
  });
});
