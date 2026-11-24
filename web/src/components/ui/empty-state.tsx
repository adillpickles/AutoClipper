import type { ReactNode } from "react";

export function EmptyState({
  title,
  description,
  heading: Heading = "h2",
  icon,
  action,
  secondaryAction,
  className = "",
}: {
  title: string;
  description: string;
  heading?: "h1" | "h2" | "h3";
  icon?: ReactNode;
  action?: ReactNode;
  secondaryAction?: ReactNode;
  className?: string;
}) {
  return (
    <section
      className={`flex flex-col items-center gap-4 rounded-panel border border-dashed border-border-strong bg-surface-panel px-5 py-8 text-center shadow-card sm:px-8 sm:py-10 ${className}`}
    >
      <span
        aria-hidden="true"
        className="flex size-11 items-center justify-center rounded-pill border border-border-subtle bg-surface-elevated text-ink-secondary"
      >
        {icon ?? (
          <svg
            viewBox="0 0 24 24"
            fill="none"
            className="size-5"
            stroke="currentColor"
            strokeWidth="1.8"
            strokeLinecap="round"
            strokeLinejoin="round"
          >
            <rect x="3" y="5" width="18" height="14" rx="2.5" />
            <path d="M8 12h8" />
          </svg>
        )}
      </span>
      <div className="flex max-w-md flex-col gap-1.5">
        <Heading
          className={`font-display font-semibold tracking-tight text-balance text-ink-primary ${
            Heading === "h1" ? "text-2xl sm:text-3xl" : "text-lg"
          }`}
        >
          {title}
        </Heading>
        <p className="text-sm leading-6 text-pretty text-ink-secondary">
          {description}
        </p>
      </div>
      {(action || secondaryAction) && (
        <div className="flex flex-wrap items-center justify-center gap-3 pt-1">
          {action}
          {secondaryAction}
        </div>
      )}
    </section>
  );
}
