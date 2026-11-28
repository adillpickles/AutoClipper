// Sharing a finished clip or highlights video (#196): suggested caption text, Twitch VOD
// timestamp links, and the Web Share API with its fallbacks. The UI lives in
// `components/share-menu.tsx`; everything here is plain functions so it can be tested
// without a browser.

/** Files larger than this are shared as text and a link instead of being loaded into memory. */
export const MAX_SHARE_FILE_BYTES = 200 * 1024 * 1024;

/**
 * A VOD offset as Twitch's `t=` value: `1h2m3s`, `4m5s`, `6s`. Leading zero units are dropped,
 * inner ones kept (`1h0m5s`). Fractions round down so the link never starts after the moment.
 */
export function twitchTimestamp(seconds: number): string {
  const total =
    Number.isFinite(seconds) && seconds > 0 ? Math.floor(seconds) : 0;
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  if (h > 0) return `${h}h${m}m${s}s`;
  if (m > 0) return `${m}m${s}s`;
  return `${s}s`;
}

/**
 * The Twitch VOD link, at `startS` when given. Null when `videoId` isn't a Twitch VOD ID
 * (Twitch IDs are digits; the API contract never casts them, so neither do we).
 */
export function vodTimestampUrl(
  videoId: string,
  startS?: number | null,
): string | null {
  if (!/^\d+$/.test(videoId)) return null;
  const base = `https://www.twitch.tv/videos/${videoId}`;
  if (startS === null || startS === undefined) return base;
  return `${base}?t=${twitchTimestamp(startS)}`;
}

export interface ShareDetails {
  /** The moment's title, or the highlights video's name. */
  title: string;
  channel?: string | null;
  videoId: string;
  /** Where the moment starts in the VOD; omit for a whole-stream video. */
  startS?: number | null;
}

/** Suggested post text: the title, the channel, and the VOD link at the moment. */
export function buildShareCaption({
  title,
  channel,
  videoId,
  startS,
}: ShareDetails): string {
  const name = channel?.trim();
  const heading = title.trim() || "Stream moment";
  const lines = [name ? `${heading} · ${name} on Twitch` : heading];
  const link = vodTimestampUrl(videoId, startS);
  if (link) lines.push(`Watch it on the VOD: ${link}`);
  return lines.join("\n");
}

/** The parts of `navigator` the share flow uses, injectable for tests. */
export interface ShareEnvironment {
  share?: (data: ShareData) => Promise<void>;
  canShare?: (data?: ShareData) => boolean;
  writeText?: (text: string) => Promise<void>;
  fetch?: typeof fetch;
}

export function browserShareEnvironment(): ShareEnvironment {
  if (typeof navigator === "undefined") return {};
  return {
    share:
      typeof navigator.share === "function"
        ? (data) => navigator.share(data)
        : undefined,
    canShare:
      typeof navigator.canShare === "function"
        ? (data) => navigator.canShare(data)
        : undefined,
    writeText:
      typeof navigator.clipboard?.writeText === "function"
        ? (text) => navigator.clipboard.writeText(text)
        : undefined,
    fetch: typeof fetch === "function" ? fetch.bind(globalThis) : undefined,
  };
}

/** What the share button can do in this browser, before any file is loaded. */
export type ShareCapability = "files" | "text" | "none";

export function shareCapability(env: ShareEnvironment): ShareCapability {
  if (!env.share) return "none";
  if (env.canShare && typeof File === "function") {
    try {
      const probe = new File([new Uint8Array(1)], "clip.mp4", {
        type: "video/mp4",
      });
      if (env.canShare({ files: [probe] })) return "files";
    } catch {
      // Some browsers throw for unsupported data instead of returning false.
    }
  }
  return "text";
}

/**
 * Load the finished MP4 as a `File` for `navigator.share`. Null when it can't be loaded (no
 * fetch, a network or CORS error, a non-2xx status) or is too large to hold in memory.
 */
export async function loadShareFile(
  url: string,
  filename: string,
  env: ShareEnvironment,
  maxBytes = MAX_SHARE_FILE_BYTES,
): Promise<File | null> {
  if (!env.fetch || typeof File !== "function") return null;
  try {
    const response = await env.fetch(url);
    if (!response.ok) return null;
    const declared = Number(response.headers.get("Content-Length"));
    if (Number.isFinite(declared) && declared > maxBytes) return null;
    const blob = await response.blob();
    if (blob.size === 0 || blob.size > maxBytes) return null;
    return new File([blob], filename, { type: "video/mp4" });
  } catch {
    return null;
  }
}

export type ShareOutcome =
  /** The share sheet took the video file. */
  | { kind: "shared-file" }
  /** The share sheet took the caption and link (the file couldn't be shared). */
  | { kind: "shared-text" }
  /** The user closed the share sheet. Nothing was shared; say nothing happened. */
  | { kind: "cancelled" }
  /** The browser refused, usually because loading the file took the click's activation. */
  | { kind: "retry" }
  /** No Web Share here, or it failed: use download and copy instead. */
  | { kind: "unsupported" }
  | { kind: "failed" };

function errorName(error: unknown): string | null {
  if (error && typeof error === "object" && "name" in error) {
    const name = (error as { name: unknown }).name;
    return typeof name === "string" ? name : null;
  }
  return null;
}

async function attempt(
  env: ShareEnvironment,
  data: ShareData,
  success: ShareOutcome,
): Promise<ShareOutcome> {
  try {
    await env.share!(data);
    return success;
  } catch (error) {
    const name = errorName(error);
    if (name === "AbortError") return { kind: "cancelled" };
    if (name === "NotAllowedError") return { kind: "retry" };
    return { kind: "failed" };
  }
}

/**
 * Share the finished video: the MP4 itself where the browser accepts files, otherwise the
 * caption and VOD link. `file` is the preloaded MP4 (see `loadShareFile`), or null.
 */
export async function shareVideo({
  file,
  title,
  caption,
  link,
  env,
}: {
  file: File | null;
  title: string;
  caption: string;
  link: string | null;
  env: ShareEnvironment;
}): Promise<ShareOutcome> {
  if (!env.share) return { kind: "unsupported" };

  if (file && env.canShare) {
    const fileData: ShareData = { files: [file], title, text: caption };
    let accepted = false;
    try {
      accepted = env.canShare(fileData);
    } catch {
      accepted = false;
    }
    if (accepted) return attempt(env, fileData, { kind: "shared-file" });
  }

  // The caption already ends with the link; pass it as `url` only when the caption has none.
  const textData: ShareData = { title, text: caption };
  if (link && !caption.includes(link)) textData.url = link;
  if (env.canShare) {
    try {
      if (!env.canShare(textData)) return { kind: "unsupported" };
    } catch {
      return { kind: "unsupported" };
    }
  }
  return attempt(env, textData, { kind: "shared-text" });
}

/**
 * Copy `text` to the clipboard. Tries the async Clipboard API, then the legacy
 * `execCommand("copy")` (blocked clipboard permissions, insecure origins such as a LAN IP).
 */
export async function copyText(
  text: string,
  env: ShareEnvironment,
): Promise<boolean> {
  if (env.writeText) {
    try {
      await env.writeText(text);
      return true;
    } catch {
      // Fall through to the legacy path.
    }
  }
  if (typeof document === "undefined") return false;
  const area = document.createElement("textarea");
  area.value = text;
  area.setAttribute("readonly", "");
  area.style.position = "fixed";
  area.style.opacity = "0";
  const active =
    document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null;
  document.body.appendChild(area);
  try {
    area.select();
    return typeof document.execCommand === "function"
      ? document.execCommand("copy")
      : false;
  } catch {
    return false;
  } finally {
    area.remove();
    active?.focus();
  }
}

const EDGE_PX = 8;
const GAP_PX = 8;

/**
 * Where to put a menu of size `menu` next to `trigger` (viewport coordinates): on `side`
 * unless only the other side has room, lined up with the trigger's start or end edge, and
 * kept `EDGE_PX` inside the viewport.
 */
export function menuPosition(
  trigger: { left: number; right: number; top: number; bottom: number },
  menu: { width: number; height: number },
  viewport: { width: number; height: number },
  side: "top" | "bottom",
  align: "start" | "end",
): { left: number; top: number } {
  const below = trigger.bottom + GAP_PX;
  const above = trigger.top - GAP_PX - menu.height;
  const fitsBelow = below + menu.height <= viewport.height - EDGE_PX;
  const fitsAbove = above >= EDGE_PX;
  let top = side === "bottom" ? below : above;
  if (side === "bottom" && !fitsBelow && fitsAbove) top = above;
  if (side === "top" && !fitsAbove && fitsBelow) top = below;
  top = Math.max(
    EDGE_PX,
    Math.min(top, viewport.height - EDGE_PX - menu.height),
  );

  let left = align === "end" ? trigger.right - menu.width : trigger.left;
  left = Math.max(
    EDGE_PX,
    Math.min(left, viewport.width - EDGE_PX - menu.width),
  );
  return { left, top };
}
