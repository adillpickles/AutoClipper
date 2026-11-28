import type { HTMLAttributes, ReactNode } from "react";

export type CardTone = "panel" | "elevated" | "inset";
export type CardPadding = "none" | "sm" | "md" | "lg";

const TONE_CLASS: Record<CardTone, string> = {
  // A top-level section of a screen.
  panel:
    "rounded-panel border border-border-subtle bg-surface-panel shadow-panel",
  // An item inside a panel, or a standalone tile.
  elevated:
    "rounded-card border border-border-subtle bg-surface-elevated shadow-card",
  // A recessed well inside a panel (rows, readouts).
  inset: "rounded-card border border-border-subtle bg-surface-canvas/60",
};

const PADDING_CLASS: Record<CardPadding, string> = {
  none: "",
  sm: "p-3 sm:p-4",
  md: "p-4 sm:p-5",
  lg: "p-5 sm:p-7",
};

export function cardClasses({
  tone = "panel",
  padding = "md",
  className = "",
}: {
  tone?: CardTone;
  padding?: CardPadding;
  className?: string;
} = {}): string {
  return [TONE_CLASS[tone], PADDING_CLASS[padding], className]
    .filter(Boolean)
    .join(" ");
}

export interface CardProps extends HTMLAttributes<HTMLElement> {
  as?: "div" | "section" | "article" | "li" | "form";
  tone?: CardTone;
  padding?: CardPadding;
  children?: ReactNode;
}

export function Card({
  as: Element = "div",
  tone = "panel",
  padding = "md",
  className,
  ...rest
}: CardProps) {
  return (
    <Element className={cardClasses({ tone, padding, className })} {...rest} />
  );
}

/** Heading block for a card or panel: title, optional description and trailing actions. */
export function CardHeader({
  title,
  description,
  id,
  heading: Heading = "h2",
  actions,
  className = "",
}: {
  title: ReactNode;
  description?: ReactNode;
  id?: string;
  heading?: "h2" | "h3";
  actions?: ReactNode;
  className?: string;
}) {
  return (
    <div
      className={`flex flex-wrap items-start justify-between gap-x-4 gap-y-3 ${className}`}
    >
      <div className="flex min-w-0 flex-col gap-1">
        <Heading
          id={id}
          className="font-display text-[1.0625rem] font-semibold tracking-[-0.01em] text-ink-primary"
        >
          {title}
        </Heading>
        {description && (
          <p className="max-w-prose text-sm leading-6 text-pretty text-ink-secondary">
            {description}
          </p>
        )}
      </div>
      {actions && (
        <div className="flex shrink-0 flex-wrap items-center gap-2">
          {actions}
        </div>
      )}
    </div>
  );
}
