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

const CLIP_LINK_MESSAGE =
  "That's a clip link. Paste the VOD it came from — twitch.tv/videos/<id> — or just the numeric ID.";

const CHANNEL_PAGE_MESSAGE =
  "That's a channel page. Open a past broadcast and copy its twitch.tv/videos/<id> link, or paste the numeric ID.";

const PLAYER_EMBED_MESSAGE =
  "That's a Twitch player embed. Paste the VOD link (twitch.tv/videos/<id>) or just the numeric ID.";

const OTHER_PLATFORM_MESSAGE =
  "AutoClipper reads Twitch VODs. Paste a link like twitch.tv/videos/<id> or just the numeric ID.";

/** Placeholder shapes shown on the start form. They are not a VOD the user chose. */
export const VOD_FORMAT_EXAMPLES = [
  "https://www.twitch.tv/videos/2345678901",
  "twitch.tv/videos/2345678901",
  "2345678901",
] as const;

/** True when the field still holds one of `VOD_FORMAT_EXAMPLES`, unchanged. */
export function isVodFormatExample(text: string): boolean {
  const trimmed = text.trim();
  return (VOD_FORMAT_EXAMPLES as readonly string[]).includes(trimmed);
}

function httpUrl(text: string): URL | null {
  const candidate = /^[a-z][a-z0-9+.-]*:/i.test(text)
    ? text
    : `https://${text}`;
  try {
    const url = new URL(candidate);
    if (url.protocol !== "http:" && url.protocol !== "https:") return null;
    return url;
  } catch {
    return null;
  }
}

function siteHost(url: URL): string {
  return url.hostname.replace(/^www\./i, "").toLowerCase();
}

const TWITCH_NON_CHANNEL_ROUTES = new Set([
  "videos",
  "directory",
  "search",
  "settings",
  "downloads",
  "p",
]);

const TWITCH_CHANNEL_TABS = new Set([
  "videos",
  "clips",
  "about",
  "schedule",
  "home",
]);

/**
 * Instant feedback for the start form. `null` when `parseVod` accepts the text. The server
 * remains authoritative and still answers with `INVALID_VOD_MESSAGE` for anything it rejects.
 */
export function vodInputMessage(text: string): string | null {
  const trimmed = text.trim();
  if (parseVod(trimmed) !== null) return null;
  if (trimmed === "") return EMPTY_VOD_MESSAGE;

  const url = httpUrl(trimmed);
  if (!url) return INVALID_VOD_MESSAGE;
  const host = siteHost(url);
  const path = url.pathname;
  const isTwitchHost = host === "twitch.tv" || host === "m.twitch.tv";

  if (host === "clips.twitch.tv" || (isTwitchHost && /\/clip\//i.test(path))) {
    return CLIP_LINK_MESSAGE;
  }
  if (
    host === "youtube.com" ||
    host === "youtu.be" ||
    host.endsWith(".youtube.com")
  ) {
    return OTHER_PLATFORM_MESSAGE;
  }
  if (host === "player.twitch.tv") return PLAYER_EMBED_MESSAGE;
  if (isTwitchHost) {
    const segments = path.split("/").filter(Boolean);
    const first = segments[0]?.toLowerCase();
    const second = segments[1]?.toLowerCase();
    const isChannelPath =
      first !== undefined &&
      !TWITCH_NON_CHANNEL_ROUTES.has(first) &&
      (segments.length === 1 ||
        (segments.length === 2 &&
          second !== undefined &&
          TWITCH_CHANNEL_TABS.has(second)));
    if (isChannelPath) return CHANNEL_PAGE_MESSAGE;
  }
  return INVALID_VOD_MESSAGE;
}

/** A path the server could treat as absolute: `/…` on macOS and Linux, `C:\…` or `\\server\…` on Windows. */
export function looksAbsolute(path: string): boolean {
  const p = path.trim();
  return p.startsWith("/") || /^[A-Za-z]:[\\/]/.test(p) || p.startsWith("\\\\");
}
