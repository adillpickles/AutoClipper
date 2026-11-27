// Clean, cross-platform filename formatting and sanitization for browser downloads.
// Mirrors `src/autoclipper/export/filenames.py`.

const WINDOWS_RESERVED_NAMES = new Set([
  "con",
  "prn",
  "aux",
  "nul",
  "com1",
  "com2",
  "com3",
  "com4",
  "com5",
  "com6",
  "com7",
  "com8",
  "com9",
  "lpt1",
  "lpt2",
  "lpt3",
  "lpt4",
  "lpt5",
  "lpt6",
  "lpt7",
  "lpt8",
  "lpt9",
]);

const DEFAULT_MAX_FILENAME_BYTES = 200;
const encoder = new TextEncoder();

function utf8ByteLength(text: string): number {
  return encoder.encode(text).length;
}

function truncateUtf8(text: string, maxBytes: number): string {
  if (maxBytes <= 0 || !text) return "";
  if (utf8ByteLength(text) <= maxBytes) return text;
  let bytes = 0;
  let result = "";
  for (const ch of text) {
    const chBytes = encoder.encode(ch).length;
    if (bytes + chBytes > maxBytes) break;
    result += ch;
    bytes += chBytes;
  }
  return result.replace(/^[ ._-]+|[ ._-]+$/g, "");
}

function guardReserved(stem: string, maxBytes: number): string {
  const dot = stem.indexOf(".");
  const base = (dot === -1 ? stem : stem.slice(0, dot)).toLowerCase();
  if (!WINDOWS_RESERVED_NAMES.has(base)) return stem;
  const prefixed = `_${stem}`;
  if (utf8ByteLength(prefixed) <= maxBytes) return prefixed;
  const trimmed = truncateUtf8(stem, Math.max(1, maxBytes - 1));
  return trimmed ? `_${trimmed}` : "_clip";
}

export function sanitizeFilenameComponent(
  text: string,
  fallback = "clip",
  maxBytes = 80,
): string {
  const normalized = text.normalize("NFKC");
  const chars: string[] = [];
  for (const ch of normalized) {
    if (/[\p{Cf}\p{Cs}\p{Co}\p{Cn}\p{Default_Ignorable_Code_Point}]/u.test(ch))
      continue;
    if (/^\p{Cc}$/u.test(ch)) {
      chars.push("-");
      continue;
    }
    if (ch === "-" || ch === "_" || /^[\p{L}\p{N}]$/u.test(ch)) {
      chars.push(ch);
    } else if (/^\p{M}$/u.test(ch)) {
      if (chars.length > 0 && chars[chars.length - 1] !== "-") {
        chars.push(ch);
      }
    } else {
      chars.push("-");
    }
  }

  let collapsed = chars
    .join("")
    .replace(/-{2,}/g, "-")
    .replace(/^[ ._-]+|[ ._-]+$/g, "")
    .normalize("NFC");
  if (!collapsed) {
    const safeFallback = fallback.replace(/^[ ._-]+|[ ._-]+$/g, "") || "clip";
    collapsed =
      safeFallback === "clip"
        ? "clip"
        : sanitizeFilenameComponent(safeFallback, "clip", maxBytes);
  }

  let bounded = truncateUtf8(collapsed, maxBytes);
  if (!bounded) {
    bounded =
      truncateUtf8(
        fallback.replace(/^[ ._-]+|[ ._-]+$/g, "") || "clip",
        maxBytes,
      ) || "clip";
  }
  return guardReserved(bounded, maxBytes);
}

export function formatVodDate(createdAt: string | null | undefined): string {
  if (!createdAt) return "unknown-date";
  const parsed = new Date(createdAt);
  if (Number.isNaN(parsed.getTime())) return "unknown-date";
  return parsed.toISOString().slice(0, 10);
}

export function formatOffsetHms(seconds: number): string {
  const total =
    Number.isFinite(seconds) && seconds >= 0 ? Math.floor(seconds) : 0;
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  return [
    String(hours).padStart(2, "0"),
    String(minutes).padStart(2, "0"),
    String(secs).padStart(2, "0"),
  ].join("-");
}

export function buildClipFilename({
  channel,
  createdAt,
  title,
  rank,
  startS,
  fallbackId,
  maxBytes = DEFAULT_MAX_FILENAME_BYTES,
}: {
  channel?: string | null;
  createdAt?: string | null;
  title?: string | null;
  rank?: number | null;
  startS: number;
  fallbackId: string;
  maxBytes?: number;
}): string {
  const idFallback = sanitizeFilenameComponent(fallbackId, "vod", 48);
  const channelPart = sanitizeFilenameComponent(channel ?? "", idFallback, 48);
  const datePart = formatVodDate(createdAt);
  const momentFallback =
    rank !== null && rank !== undefined ? `rank-${rank}` : "clip";
  let momentPart = sanitizeFilenameComponent(title ?? "", momentFallback, 72);
  const timePart = formatOffsetHms(startS);

  const ext = ".mp4";
  const stemBudget = Math.max(1, maxBytes - utf8ByteLength(ext));
  const fixedBytes = utf8ByteLength(`${channelPart}_${datePart}__${timePart}`);
  const remaining = stemBudget - fixedBytes;
  if (remaining < utf8ByteLength(momentPart)) {
    if (remaining >= 4) {
      momentPart =
        truncateUtf8(momentPart, remaining) ||
        truncateUtf8(momentFallback, remaining) ||
        "clip";
    } else {
      const rawStem = `${channelPart}_${datePart}_${momentPart}_${timePart}`;
      const bounded = guardReserved(
        truncateUtf8(rawStem, stemBudget) || "clip",
        stemBudget,
      );
      return `${bounded}${ext}`;
    }
  }
  const stem = guardReserved(
    `${channelPart}_${datePart}_${momentPart}_${timePart}`,
    stemBudget,
  );
  return `${stem}${ext}`;
}

export function buildHighlightsFilename({
  channel,
  createdAt,
  fallbackId,
  maxBytes = DEFAULT_MAX_FILENAME_BYTES,
}: {
  channel?: string | null;
  createdAt?: string | null;
  fallbackId: string;
  maxBytes?: number;
}): string {
  const idFallback = sanitizeFilenameComponent(fallbackId, "vod", 48);
  const suffix = `_${formatVodDate(createdAt)}_highlights.mp4`;
  const channelBudget = maxBytes - utf8ByteLength(suffix);
  if (channelBudget < 1) {
    throw new RangeError("filename byte limit leaves no room for the channel");
  }
  const channelPart = sanitizeFilenameComponent(channel ?? "", idFallback, 48);
  const bounded = truncateUtf8(channelPart, channelBudget) || "v";
  return `${bounded}${suffix}`;
}

function collisionKey(filename: string): string {
  // Unicode default case folding, matching Python's str.casefold(). Upper then
  // lower per code point captures expansions and avoids contextual final sigma.
  let folded = "";
  for (const character of filename.normalize("NFC")) {
    if (character === "\u0131")
      folded += character; // Dotless i stays distinct.
    else if (character === "\u1e9e") folded += "ss";
    else if (/[\u13a0-\u13ff\uab70-\uabbf]/u.test(character)) {
      folded += character.toUpperCase(); // Cherokee folds to uppercase.
    } else {
      folded += character.toUpperCase().toLowerCase();
    }
  }
  return folded.normalize("NFC");
}

export function deduplicateFilename(
  filename: string,
  usedLower: Set<string>,
  maxBytes = DEFAULT_MAX_FILENAME_BYTES,
): string {
  const key = collisionKey(filename);
  if (!usedLower.has(key)) {
    usedLower.add(key);
    return filename;
  }
  const dot = filename.lastIndexOf(".");
  const stem = dot > 0 ? filename.slice(0, dot) : filename;
  const ext = dot > 0 ? filename.slice(dot) : "";
  const extBytes = utf8ByteLength(ext);

  let counter = 2;
  while (true) {
    const suffix = `_${counter}`;
    const budget = Math.max(1, maxBytes - extBytes - utf8ByteLength(suffix));
    const trimmed = guardReserved(truncateUtf8(stem, budget) || "clip", budget);
    const candidate = `${trimmed}${suffix}${ext}`;
    const candKey = collisionKey(candidate);
    if (!usedLower.has(candKey)) {
      usedLower.add(candKey);
      return candidate;
    }
    counter += 1;
  }
}
