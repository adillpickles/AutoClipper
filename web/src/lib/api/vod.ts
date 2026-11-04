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

/** A path the server could treat as absolute: `/…` on macOS and Linux, `C:\…` or `\\server\…` on Windows. */
export function looksAbsolute(path: string): boolean {
  const p = path.trim();
  return p.startsWith("/") || /^[A-Za-z]:[\\/]/.test(p) || p.startsWith("\\\\");
}
