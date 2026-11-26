// Client-side check of a Twitch channel URL or login (api-contract.md § 9.7).
// The server is authoritative; this gives instant feedback before making a network request.

import { parseVod } from "@/lib/api/vod";

const CHANNEL_LOGIN_PATTERN = /^@?([A-Za-z0-9][A-Za-z0-9_]{0,24})$/;
const CHANNEL_URL_PATTERN =
  /^(?:https?:\/\/)?(?:www\.|m\.)?twitch\.tv\/([A-Za-z0-9][A-Za-z0-9_]{0,24})(?:\/(?:videos|home|about|schedule|clips))?\/?(?:[?#].*)?$/i;

const TWITCH_RESERVED_PATHS = new Set([
  "clips",
  "directory",
  "downloads",
  "drops",
  "inventory",
  "jobs",
  "login",
  "moderator",
  "p",
  "popout",
  "prime",
  "search",
  "settings",
  "signup",
  "subscriptions",
  "turbo",
  "u",
  "user",
  "videos",
  "wallet",
]);

/** Server message for `invalid_channel` (§ 9.7). */
export const INVALID_CHANNEL_MESSAGE =
  "That isn't a Twitch channel link or login. Paste a link like twitch.tv/<name> or just the channel name.";

const EMPTY_CHANNEL_MESSAGE =
  "Paste a Twitch channel link, like twitch.tv/user_001, or just the channel login.";

const VOD_LINK_MESSAGE =
  "That's a VOD link or ID, not a channel. Paste a channel link like twitch.tv/<name>, or analyze a single VOD from the home page.";

const CLIP_LINK_MESSAGE =
  "That's a clip link, not a channel. Paste the streamer's channel link (twitch.tv/<name>) or login.";

const OTHER_PLATFORM_MESSAGE =
  "AutoClipper browses Twitch channels. Paste a link like twitch.tv/<name> or a Twitch channel login.";

/** Example inputs shown on the channel browser form. */
export const CHANNEL_FORMAT_EXAMPLES = [
  { label: "Channel link", value: "https://www.twitch.tv/user_001" },
  { label: "Videos tab", value: "twitch.tv/user_001/videos" },
  { label: "Channel login", value: "user_001" },
] as const;

function isAllDigits(value: string): boolean {
  return /^[0-9]+$/.test(value);
}

/** Normalized lowercase channel login from a Twitch channel URL or bare login, or `null`. */
export function parseChannel(text: string): string | null {
  const trimmed = text.trim();
  if (!trimmed) return null;

  const urlMatch = CHANNEL_URL_PATTERN.exec(trimmed);
  if (urlMatch) {
    const login = urlMatch[1].toLowerCase();
    if (TWITCH_RESERVED_PATHS.has(login) || isAllDigits(login)) return null;
    return login;
  }

  const loginMatch = CHANNEL_LOGIN_PATTERN.exec(trimmed);
  if (loginMatch) {
    const login = loginMatch[1].toLowerCase();
    if (isAllDigits(login)) return null;
    return login;
  }

  return null;
}

function tryHttpUrl(text: string): URL | null {
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

/** Instant validation message for the channel input; `null` when `parseChannel` accepts it. */
export function channelInputMessage(text: string): string | null {
  const trimmed = text.trim();
  if (parseChannel(trimmed) !== null) return null;
  if (trimmed === "") return EMPTY_CHANNEL_MESSAGE;
  if (parseVod(trimmed) !== null) return VOD_LINK_MESSAGE;

  const url = tryHttpUrl(trimmed);
  if (url) {
    const host = url.hostname.replace(/^www\./i, "").toLowerCase();
    const isTwitchHost = host === "twitch.tv" || host === "m.twitch.tv";
    if (
      host === "clips.twitch.tv" ||
      (isTwitchHost && /\/clip\//i.test(url.pathname))
    ) {
      return CLIP_LINK_MESSAGE;
    }
    if (
      host === "youtube.com" ||
      host === "youtu.be" ||
      host.endsWith(".youtube.com")
    ) {
      return OTHER_PLATFORM_MESSAGE;
    }
  }
  return INVALID_CHANNEL_MESSAGE;
}
