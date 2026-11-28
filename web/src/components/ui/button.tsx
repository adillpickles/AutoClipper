import Link, { type LinkProps } from "next/link";
import type {
  AnchorHTMLAttributes,
  ButtonHTMLAttributes,
  ReactNode,
} from "react";

export type ButtonVariant =
  "primary" | "secondary" | "subtle" | "ghost" | "danger" | "media";

export type ButtonSize = "sm" | "md" | "lg";

/** Focus ring shared by every interactive primitive: an outline, so it needs no offset color. */
export const FOCUS_RING =
  "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus-ring";

const BASE = `inline-flex items-center justify-center gap-2 rounded-control font-semibold whitespace-nowrap transition-[background-color,border-color,color,box-shadow] duration-150 disabled:cursor-not-allowed disabled:opacity-50 aria-disabled:cursor-not-allowed aria-disabled:opacity-50`;

const VARIANT_CLASS: Record<ButtonVariant, string> = {
  // The one action a screen is about: filled violet with a soft glow.
  primary: `bg-accent text-accent-ink shadow-[0_1px_0_rgb(255_255_255/0.28)_inset,0_8px_24px_-12px_rgb(167_139_250/0.7)] hover:bg-accent-hover disabled:shadow-none ${FOCUS_RING}`,
  secondary: `border border-border-strong bg-surface-elevated text-ink-primary hover:border-ink-tertiary hover:bg-surface-subtle ${FOCUS_RING}`,
  subtle: `border border-border-subtle bg-surface-elevated text-ink-secondary hover:bg-surface-subtle hover:text-ink-primary ${FOCUS_RING}`,
  ghost: `text-ink-secondary hover:bg-surface-elevated hover:text-ink-primary ${FOCUS_RING}`,
  danger: `bg-danger text-danger-ink hover:brightness-110 ${FOCUS_RING}`,
  media:
    "border border-white/25 bg-white/10 text-white backdrop-blur-sm hover:bg-white/20 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus-ring-media",
};

const SIZE_CLASS: Record<ButtonSize, string> = {
  sm: "min-h-9 px-3 py-1.5 text-xs",
  md: "min-h-10 px-4 py-2 text-sm",
  lg: "min-h-12 px-5 py-3 text-[0.9375rem]",
};

export function buttonClasses({
  variant = "primary",
  size = "md",
  className = "",
}: {
  variant?: ButtonVariant;
  size?: ButtonSize;
  className?: string;
} = {}): string {
  return [BASE, VARIANT_CLASS[variant], SIZE_CLASS[size], className]
    .filter(Boolean)
    .join(" ");
}

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  size?: ButtonSize;
}

export function Button({
  variant = "primary",
  size = "md",
  type = "button",
  className,
  ...rest
}: ButtonProps) {
  return (
    <button
      type={type}
      className={buttonClasses({ variant, size, className })}
      {...rest}
    />
  );
}

export interface ButtonLinkProps
  extends
    Omit<AnchorHTMLAttributes<HTMLAnchorElement>, "href">,
    Pick<LinkProps, "href" | "prefetch" | "replace" | "scroll"> {
  variant?: ButtonVariant;
  size?: ButtonSize;
  children: ReactNode;
}

export function ButtonLink({
  variant = "primary",
  size = "md",
  className,
  children,
  ...rest
}: ButtonLinkProps) {
  return (
    <Link className={buttonClasses({ variant, size, className })} {...rest}>
      {children}
    </Link>
  );
}

/** Inline text link in the accent color, for links inside sentences. */
export const TEXT_LINK = `font-semibold text-accent underline decoration-accent/40 underline-offset-[3px] transition-colors hover:text-accent-hover hover:decoration-accent-hover rounded-[3px] ${FOCUS_RING}`;
