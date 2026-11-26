// Helpers for turning a Candidate into a display title, one-line reason, and structured
// contributing signals. Prefers server-supplied `title`, `reason`, and `signal_details`
// (schema 1.1; api-contract.md § 4.2) and falls back cleanly to `top_signals` and
// `n_viewer_clips` for older 1.0 results.

import type { Candidate, SignalDetail } from "@/lib/api/types";

type CandidateSummary = Pick<
  Candidate,
  "top_signals" | "n_viewer_clips" | "title" | "reason" | "signal_details"
>;

// Prefixes of the feature names in docs/ml/features.md, most specific first.
const PHRASES: ReadonlyArray<
  [prefix: string, kind: string, label: string, phrase: string]
> = [
  [
    "chat_unique_rate",
    "chat_unique",
    "Unique chatters",
    "lots of viewers reacted",
  ],
  ["chat_msg_rate", "chat_rate", "Chat rate", "chat exploded"],
  ["chat_laugh_rate", "chat_laugh", "Chat reaction", "chat was laughing"],
  ["chat_hype_rate", "chat_hype", "Chat reaction", "chat got hyped"],
  ["chat_shock_rate", "chat_shock", "Chat reaction", "chat was shocked"],
  [
    "chat_keyword_rate",
    "chat_keyword",
    "Chat reaction",
    "chat called for a clip",
  ],
  ["chat_emote", "chat_emotes", "Emote density", "chat spammed emotes"],
  ["chat_caps_frac", "chat_caps", "All-caps chat", "chat was shouting"],
  [
    "chat_repeat_frac",
    "chat_repeat",
    "Repeated messages",
    "chat spammed the same message",
  ],
  [
    "chat_event_count",
    "chat_events",
    "Stream events",
    "subs and cheers rolled in",
  ],
  ["audio_evt_scream", "audio_event", "Audio event", "the streamer screamed"],
  ["audio_evt_laughter", "audio_event", "Audio event", "the streamer laughed"],
  ["audio_evt_cheer", "audio_event", "Audio event", "there was cheering"],
  ["audio_evt_music", "audio_event", "Audio event", "music kicked in"],
  ["audio_rms", "audio_loudness", "Audio loudness", "the streamer got loud"],
  [
    "audio_onset_rate",
    "audio_onset",
    "Audio activity",
    "a lot was happening on stream",
  ],
  ["speech_", "speech", "Speech", "the streamer reacted out loud"],
];

function matchSignal(
  signal: string,
): { kind: string; label: string; phrase: string } | null {
  for (const [prefix, kind, label, phrase] of PHRASES) {
    if (signal.startsWith(prefix)) return { kind, label, phrase };
  }
  return null;
}

function capitalize(text: string): string {
  return text.length > 0 ? text.charAt(0).toUpperCase() + text.slice(1) : text;
}

/** Short human title for a candidate, preferring server `title` with a fallback for 1.0 results. */
export function titleFor(candidate: CandidateSummary): string {
  const serverTitle = candidate.title?.trim();
  if (serverTitle) return serverTitle;

  const hasChat = candidate.top_signals.some((s) => s.startsWith("chat_"));
  const hasAudio = candidate.top_signals.some((s) => s.startsWith("audio_"));
  if (hasChat && hasAudio) return "Loud audio & chat spike";
  if (hasChat) return "Chat surge";
  if (hasAudio) return "Loud audio spike";
  if (candidate.n_viewer_clips >= 3) return "Popular viewer clip";
  if (candidate.n_viewer_clips > 0) return "Viewer-clipped moment";
  return "Stream moment";
}

/** One-line "why this moment" reason, preferring server `reason` with a fallback for 1.0 results. */
export function reasonFor(candidate: CandidateSummary): string {
  const serverReason = candidate.reason?.trim();
  if (serverReason) return serverReason;

  const phrases: string[] = [];
  for (const signal of candidate.top_signals) {
    const matched = matchSignal(signal);
    if (matched && !phrases.includes(matched.phrase)) {
      phrases.push(matched.phrase);
    }
    if (phrases.length === 2) break;
  }
  if (phrases.length === 0) {
    if (candidate.n_viewer_clips > 0) return "Viewers clipped this moment";
    // The contract's "Steady activity throughout" is for an empty `top_signals` only.
    return candidate.top_signals.length > 0
      ? "Chat and audio stood out here"
      : "Steady activity throughout";
  }
  return capitalize(phrases.join(" and "));
}

/** Structured contributing signals for the "Why" breakdown, with a fallback for 1.0 results. */
export function signalDetailsFor(candidate: CandidateSummary): SignalDetail[] {
  if (Array.isArray(candidate.signal_details)) {
    return candidate.signal_details;
  }

  const details: SignalDetail[] = [];
  const seenKinds = new Set<string>();
  for (const signal of candidate.top_signals) {
    const matched = matchSignal(signal);
    if (matched) {
      if (seenKinds.has(matched.kind)) continue;
      seenKinds.add(matched.kind);
      details.push({
        kind: matched.kind,
        label: matched.label,
        detail: capitalize(matched.phrase),
      });
    } else if (!seenKinds.has(signal)) {
      seenKinds.add(signal);
      details.push({
        kind: signal,
        label: "Signal",
        detail: signal,
      });
    }
  }
  if (candidate.n_viewer_clips > 0) {
    details.push({
      kind: "viewer_clips",
      label: "Viewer clips",
      detail:
        candidate.n_viewer_clips === 1
          ? "1 viewer clip overlaps this moment"
          : `${candidate.n_viewer_clips} viewer clips overlap this moment`,
    });
  }
  return details;
}
