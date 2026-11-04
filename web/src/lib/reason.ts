// The one-line reason on a clip card, built from the candidate's `top_signals` (feature names,
// strongest first; api-contract.md § 4.2 and docs/ml/features.md).

import type { Candidate } from "@/lib/api/types";

// Prefixes of the feature names in docs/ml/features.md, most specific first.
const PHRASES: ReadonlyArray<[prefix: string, phrase: string]> = [
  ["chat_unique_rate", "lots of viewers reacted"],
  ["chat_msg_rate", "chat exploded"],
  ["chat_laugh_rate", "chat was laughing"],
  ["chat_hype_rate", "chat got hyped"],
  ["chat_shock_rate", "chat was shocked"],
  ["chat_keyword_rate", "chat called for a clip"],
  ["chat_emote", "chat spammed emotes"],
  ["chat_caps_frac", "chat was shouting"],
  ["chat_repeat_frac", "chat spammed the same message"],
  ["chat_event_count", "subs and cheers rolled in"],
  ["audio_evt_scream", "the streamer screamed"],
  ["audio_evt_laughter", "the streamer laughed"],
  ["audio_evt_cheer", "there was cheering"],
  ["audio_evt_music", "music kicked in"],
  ["audio_rms", "the streamer got loud"],
  ["audio_onset_rate", "a lot was happening on stream"],
  ["speech_", "the streamer reacted out loud"],
];

function phraseFor(signal: string): string | null {
  for (const [prefix, phrase] of PHRASES) {
    if (signal.startsWith(prefix)) return phrase;
  }
  return null;
}

/** "Chat exploded and the streamer got loud", or the contract's fallbacks when nothing stands out. */
export function reasonFor(
  candidate: Pick<Candidate, "top_signals" | "n_viewer_clips">,
): string {
  const phrases: string[] = [];
  for (const signal of candidate.top_signals) {
    const phrase = phraseFor(signal);
    if (phrase && !phrases.includes(phrase)) phrases.push(phrase);
    if (phrases.length === 2) break;
  }
  if (phrases.length === 0) {
    if (candidate.n_viewer_clips > 0) return "Viewers clipped this moment";
    // The contract's "Steady activity throughout" is for an empty `top_signals` only.
    return candidate.top_signals.length > 0
      ? "Chat and audio stood out here"
      : "Steady activity throughout";
  }
  const sentence = phrases.join(" and ");
  return sentence.charAt(0).toUpperCase() + sentence.slice(1);
}
