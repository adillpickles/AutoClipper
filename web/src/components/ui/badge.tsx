import type { ReactNode } from "react";

export type BadgeTone =
  | "neutral"
  | "accent"
  | "signal"
  | "success"
  | "danger"
  | "solid-accent"
  | "media";

const TONE_CLASS: Record<BadgeTone, string> = {
  neutral: "border-border-strong bg-surface-elevated text-ink-secondary",
  accent: "border-accent-border bg-accent-soft text-accent-soft-ink",
  signal: "border-signal-border bg-signal-soft text-signal-soft-ink",
  success: "border-success-border bg-success-soft text-success-soft-ink",
  danger: "border-danger-border bg-danger-soft text-danger-soft-ink",
  "solid-accent": "border-transparent bg-accent text-accent-ink",
  // On top of video thumbnails.
  media: "border-white/15 bg-black/70 text-white backdrop-blur-sm",
};

const DOT_CLASS: Record<BadgeTone, string> = {
  neutral: "bg-ink-tertiary",
  accent: "bg-accent",
  signal: "bg-signal",
  success: "bg-success",
  danger: "bg-danger",
  "solid-accent": "bg-accent-ink",
  media: "bg-white",
};

/**
 * A short status label. `dot` adds a tally light; `live` makes it pulse (motion-safe only),
 * for work that is running right now.
 */
export function Badge({
  tone = "neutral",
  dot = false,
  live = false,
  mono = false,
  className = "",
  children,
}: {
  tone?: BadgeTone;
  dot?: boolean;
  live?: boolean;
  mono?: boolean;
  className?: string;
  children: ReactNode;
}) {
  return (
    <span
      className={`inline-flex shrink-0 items-center gap-1.5 rounded-pill border px-2.5 py-0.5 text-xs font-semibold whitespace-nowrap ${
        mono ? "font-mono font-medium tabular-nums" : ""
      } ${TONE_CLASS[tone]} ${className}`}
    >
      {(dot || live) && (
        <span
          aria-hidden="true"
          className={`size-1.5 shrink-0 rounded-full ${DOT_CLASS[tone]} ${
            live ? "motion-safe:animate-pulse" : ""
          }`}
        />
      )}
      {children}
    </span>
  );
}
