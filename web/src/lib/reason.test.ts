import { describe, expect, it } from "vitest";

import { reasonFor } from "@/lib/reason";

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
    [[], 1, "Viewers clipped this moment"],
    [[], 0, "Steady activity throughout"],
  ])("%j with %i clips reads %s", (signals, clips, reason) => {
    expect(reasonFor({ top_signals: signals, n_viewer_clips: clips })).toBe(
      reason,
    );
  });
});
