import type { ReactNode } from "react";

export type ErrorStateTone = "danger" | "signal";

const TONE_WRAPPER: Record<ErrorStateTone, string> = {
  danger: "border-danger-border",
  signal: "border-signal-border",
};

const TONE_EDGE: Record<ErrorStateTone, string> = {
  danger: "bg-danger",
  signal: "bg-signal",
};

const TONE_BADGE: Record<ErrorStateTone, string> = {
  danger: "border-danger-border bg-danger-soft text-danger",
  signal: "border-signal-border bg-signal-soft text-signal",
};

const TONE_TITLE: Record<ErrorStateTone, string> = {
  danger: "text-danger-soft-ink",
  signal: "text-signal-soft-ink",
};

export function ErrorState({
  title,
  message,
  details,
  heading: Heading = "h2",
  tone = "danger",
  alert = false,
  primaryAction,
  secondaryAction,
  className = "",
}: {
  title: string;
  message: string;
  details?: string;
  heading?: "h1" | "h2" | "h3";
  tone?: ErrorStateTone;
  alert?: boolean;
  primaryAction?: ReactNode;
  secondaryAction?: ReactNode;
  className?: string;
}) {
  return (
    <section
      role={alert ? "alert" : undefined}
      className={`relative flex flex-col gap-4 overflow-hidden rounded-card border bg-surface-panel p-5 pl-6 shadow-card sm:p-6 sm:pl-7 ${TONE_WRAPPER[tone]} ${className}`}
    >
      <span
        aria-hidden="true"
        className={`absolute inset-y-0 left-0 w-[3px] ${TONE_EDGE[tone]}`}
      />
      <div className="flex items-start gap-3">
        <span
          aria-hidden="true"
          className={`mt-0.5 flex size-7 shrink-0 items-center justify-center rounded-control border ${TONE_BADGE[tone]}`}
        >
          <svg
            viewBox="0 0 20 20"
            fill="none"
            className="size-4"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
          >
            <path d="M10 5.5v5.5" />
            <circle cx="10" cy="14.5" r="0.6" fill="currentColor" />
          </svg>
        </span>
        <div className="flex min-w-0 flex-1 flex-col gap-1.5">
          <Heading
            className={`font-display font-semibold tracking-[-0.015em] text-balance ${TONE_TITLE[tone]} ${
              Heading === "h1" ? "text-2xl sm:text-3xl" : "text-base"
            }`}
          >
            {title}
          </Heading>
          <p className="text-sm leading-6 break-words text-ink-secondary">
            {message}
          </p>
          {details && (
            <p className="mt-1 rounded-control border border-border-subtle bg-surface-canvas/70 px-3 py-2 font-mono text-xs leading-5 break-words text-ink-secondary">
              {details}
            </p>
          )}
        </div>
      </div>
      {(primaryAction || secondaryAction) && (
        <div className="flex flex-wrap items-center gap-3 sm:pl-10">
          {primaryAction}
          {secondaryAction}
        </div>
      )}
    </section>
  );
}
