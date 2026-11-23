// Client-side check of a VOD link (api-contract.md § 3.1). The server is authoritative; this
// only gives instant feedback. Matching is case-insensitive after trimming whitespace.

const VOD_PATTERN =
  /^(?:(?:https?:\/\/)?(?:www\.|m\.)?twitch\.tv\/videos\/([0-9]+)\/?(?:[?#].*)?|([0-9]+))$/i;

/** The video ID in a Twitch VOD link or bare ID, or `null` if it isn't one. */
export function parseVod(text: string): string | null {
  const match = VOD_PATTERN.exec(text.trim());
  if (!match) return null;
  return match[1] ?? match[2] ?? null;
}

/** Message the server sends for `invalid_vod_link` (§ 3.6). */
export const INVALID_VOD_MESSAGE =
  "That isn't a Twitch VOD link. Paste a link like twitch.tv/videos/<id> or just the numeric ID.";

const EMPTY_VOD_MESSAGE =
  "Paste a Twitch VOD link, like twitch.tv/videos/2345678901, or just the numeric ID.";

/**
 * Instant feedback for the start form. `null` when `parseVod` accepts the text. The server
 * remains authoritative and still answers with `INVALID_VOD_MESSAGE` for anything it rejects.
 */
export function vodInputMessage(text: string): string | null {
  const trimmed = text.trim();
  if (parseVod(trimmed) !== null) return null;
  if (trimmed === "") return EMPTY_VOD_MESSAGE;
  if (/clips\.twitch\.tv/i.test(trimmed)) {
    return "That's a clip link. Paste the VOD it came from — twitch.tv/videos/<id> — or just the numeric ID.";
  }
  if (
    /(?:^https?:\/\/)?(?:www\.)?(?:youtube\.com|youtu\.be)\//i.test(trimmed)
  ) {
    return "AutoClipper reads Twitch VODs. Paste a link like twitch.tv/videos/<id> or just the numeric ID.";
  }
  if (/twitch\.tv\//i.test(trimmed) && !/twitch\.tv\/videos\//i.test(trimmed)) {
    return "That's a channel page. Open a past broadcast and copy its twitch.tv/videos/<id> link, or paste the numeric ID.";
  }
  return INVALID_VOD_MESSAGE;
}

/** A path the server could treat as absolute: `/…` on macOS and Linux, `C:\…` or `\\server\…` on Windows. */
export function looksAbsolute(path: string): boolean {
  const p = path.trim();
  return p.startsWith("/") || /^[A-Za-z]:[\\/]/.test(p) || p.startsWith("\\\\");
}
