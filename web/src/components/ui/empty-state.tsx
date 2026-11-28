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
      className={`flex flex-col items-center gap-4 rounded-panel border border-dashed border-border-strong bg-surface-panel/60 px-5 py-9 text-center sm:px-8 sm:py-12 ${className}`}
    >
      <span
        aria-hidden="true"
        className="flex size-11 items-center justify-center rounded-card border border-border-subtle bg-surface-elevated text-ink-tertiary"
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
            <path d="M4 18V14M8 18V9M12 18v-5M16 18V6M20 18v-3" />
          </svg>
        )}
      </span>
      <div className="flex max-w-md flex-col gap-1.5">
        <Heading
          className={`font-display font-semibold tracking-[-0.02em] text-balance text-ink-primary ${
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
