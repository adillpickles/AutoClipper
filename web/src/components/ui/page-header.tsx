import type { ReactNode } from "react";

/** The title block at the top of a screen: one h1, an optional lead and trailing actions. */
export function PageHeader({
  title,
  description,
  actions,
  className = "",
}: {
  title: ReactNode;
  description?: ReactNode;
  actions?: ReactNode;
  className?: string;
}) {
  return (
    <header
      className={`flex flex-wrap items-end justify-between gap-x-6 gap-y-4 ${className}`}
    >
      <div className="flex max-w-2xl min-w-0 flex-col gap-2.5">
        <h1 className="font-display text-title font-semibold text-balance text-ink-primary">
          {title}
        </h1>
        {description && (
          <p className="text-[0.9375rem] leading-7 text-pretty text-ink-secondary sm:text-base/7">
            {description}
          </p>
        )}
      </div>
      {actions && (
        <div className="flex shrink-0 flex-wrap items-center gap-2">
          {actions}
        </div>
      )}
    </header>
  );
}
