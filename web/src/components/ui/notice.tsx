import type { ReactNode } from "react";

export type NoticeTone = "info" | "signal" | "success" | "danger";

const TONE_CLASS: Record<NoticeTone, string> = {
  info: "border-border-strong bg-surface-elevated text-ink-secondary",
  signal: "border-signal-border bg-signal-soft text-signal-soft-ink",
  success: "border-success-border bg-success-soft text-success-soft-ink",
  danger: "border-danger-border bg-danger-soft text-danger-soft-ink",
};

const BAR_CLASS: Record<NoticeTone, string> = {
  info: "bg-ink-tertiary",
  signal: "bg-signal",
  success: "bg-success",
  danger: "bg-danger",
};

/**
 * An inline message inside a screen or form: a tinted strip with a colored edge. Use
 * `ErrorState` for a whole-screen failure and this for everything smaller.
 */
export function Notice({
  tone = "info",
  role,
  title,
  action,
  className = "",
  children,
}: {
  tone?: NoticeTone;
  role?: "alert" | "status";
  title?: ReactNode;
  action?: ReactNode;
  className?: string;
  children?: ReactNode;
}) {
  return (
    <div
      role={role}
      className={`relative flex flex-wrap items-center justify-between gap-x-4 gap-y-2 overflow-hidden rounded-control border py-2.5 pr-3 pl-4 text-sm leading-6 ${TONE_CLASS[tone]} ${className}`}
    >
      <span
        aria-hidden="true"
        className={`absolute inset-y-0 left-0 w-[3px] ${BAR_CLASS[tone]}`}
      />
      <div className="min-w-0 flex-1 break-words">
        {title && <p className="font-semibold">{title}</p>}
        {children}
      </div>
      {action && <div className="shrink-0">{action}</div>}
    </div>
  );
}
