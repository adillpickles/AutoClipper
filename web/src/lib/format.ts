/** A VOD position in seconds as `m:ss` or `h:mm:ss`. */
export function formatTime(seconds: number): string {
  const total = Math.max(0, Math.floor(seconds));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const ss = String(s).padStart(2, "0");
  return h > 0 ? `${h}:${String(m).padStart(2, "0")}:${ss}` : `${m}:${ss}`;
}

const UTC_DATE_FORMATTER = new Intl.DateTimeFormat("en-US", {
  month: "short",
  day: "numeric",
  year: "numeric",
  timeZone: "UTC",
});

/** Format a UTC ISO-8601 timestamp as a short date like `Oct 13, 2026`. */
export function formatBroadcastDate(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return UTC_DATE_FORMATTER.format(date);
}

/** Format a Twitch view count compactly (e.g. `4,820 views` or `12.4K views`). */
export function formatViewCount(views: number): string {
  if (!Number.isFinite(views) || views < 0) return "0 views";
  if (views >= 1_000_000) {
    return `${(views / 1_000_000).toFixed(1).replace(/\.0$/, "")}M views`;
  }
  if (views >= 10_000) {
    return `${(views / 1_000).toFixed(1).replace(/\.0$/, "")}K views`;
  }
  return `${Math.floor(views).toLocaleString("en-US")} ${views === 1 ? "view" : "views"}`;
}
