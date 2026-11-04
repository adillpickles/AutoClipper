// The one-line reason on a clip card, built from the candidate's `top_signals` (feature names,
// strongest first; api-contract.md § 4.2 and docs/ml/features.md).

import type { Candidate } from "@/lib/api/types";

const PHRASES: ReadonlyArray<[prefix: string, phrase: string]> = [
  ["chat_unique_rate", "lots of viewers reacted"],
  ["chat_msg_rate", "chat exploded"],
  ["chat_laugh_rate", "chat was laughing"],
  ["chat_hype_rate", "chat got hyped"],
  ["chat_shock_rate", "chat was shocked"],
  ["chat_emote_rate", "chat spammed emotes"],
  ["audio_rms", "the streamer got loud"],
  ["audio_onset_rate", "a lot was happening on stream"],
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
    return candidate.n_viewer_clips > 0
      ? "Viewers clipped this moment"
      : "Steady activity throughout";
  }
  const sentence = phrases.join(" and ");
  return sentence.charAt(0).toUpperCase() + sentence.slice(1);
}
