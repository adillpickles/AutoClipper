import Link, { type LinkProps } from "next/link";
import type {
  AnchorHTMLAttributes,
  ButtonHTMLAttributes,
  ReactNode,
} from "react";

export type ButtonVariant =
  "primary" | "secondary" | "subtle" | "ghost" | "danger" | "media";

export type ButtonSize = "sm" | "md" | "lg";

const BASE =
  "inline-flex items-center justify-center gap-2 rounded-control font-semibold transition-colors focus-visible:ring-2 focus-visible:ring-offset-2 focus-visible:outline-none disabled:cursor-not-allowed disabled:opacity-55";

const VARIANT_CLASS: Record<ButtonVariant, string> = {
  primary:
    "bg-accent text-accent-ink shadow-card hover:bg-accent-hover focus-visible:ring-focus-ring focus-visible:ring-offset-surface-panel",
  secondary:
    "border border-border-strong bg-surface-panel text-ink-primary hover:bg-surface-elevated focus-visible:ring-focus-ring focus-visible:ring-offset-surface-panel",
  subtle:
    "border border-border-subtle bg-surface-elevated text-ink-primary hover:bg-surface-subtle focus-visible:ring-focus-ring focus-visible:ring-offset-surface-panel",
  ghost:
    "text-ink-secondary hover:bg-surface-elevated hover:text-ink-primary focus-visible:ring-focus-ring focus-visible:ring-offset-surface-canvas",
  danger:
    "bg-danger text-danger-ink shadow-card hover:opacity-90 focus-visible:ring-focus-ring focus-visible:ring-offset-surface-panel",
  media:
    "border border-white/25 bg-white/10 text-white hover:bg-white/20 focus-visible:ring-focus-ring-media focus-visible:ring-offset-surface-media",
};

const SIZE_CLASS: Record<ButtonSize, string> = {
  sm: "min-h-9 px-3 py-1.5 text-xs",
  md: "min-h-10 px-4 py-2 text-sm",
  lg: "min-h-11 px-5 py-3 text-sm",
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
