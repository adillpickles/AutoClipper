import type { ReactNode } from "react";

export type ErrorStateTone = "danger" | "signal";

const TONE_WRAPPER: Record<ErrorStateTone, string> = {
  danger: "border-danger-border bg-danger-soft text-danger-soft-ink",
  signal: "border-signal-border bg-signal-soft text-signal-soft-ink",
};

const TONE_BADGE: Record<ErrorStateTone, string> = {
  danger: "bg-danger text-danger-ink",
  signal: "bg-signal text-surface-media",
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
      className={`flex flex-col gap-4 rounded-card border p-5 shadow-card sm:p-6 ${TONE_WRAPPER[tone]} ${className}`}
    >
      <div className="flex items-start gap-3">
        <span
          aria-hidden="true"
          className={`mt-0.5 flex size-6 shrink-0 items-center justify-center rounded-pill text-xs font-bold ${TONE_BADGE[tone]}`}
        >
          !
        </span>
        <div className="flex min-w-0 flex-1 flex-col gap-1.5">
          <Heading
            className={`font-display font-semibold tracking-tight text-balance ${
              Heading === "h1" ? "text-2xl sm:text-3xl" : "text-base"
            }`}
          >
            {title}
          </Heading>
          <p className="text-sm leading-6 break-words">{message}</p>
          {details && (
            <p className="font-mono text-xs break-words opacity-90">
              {details}
            </p>
          )}
        </div>
      </div>
      {(primaryAction || secondaryAction) && (
        <div className="flex flex-wrap items-center gap-3 sm:pl-9">
          {primaryAction}
          {secondaryAction}
        </div>
      )}
    </section>
  );
}
