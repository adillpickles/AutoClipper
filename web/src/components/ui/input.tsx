import type { InputHTMLAttributes, ReactNode, Ref } from "react";

export type InputSize = "sm" | "md" | "lg";

const SIZE_CLASS: Record<InputSize, string> = {
  sm: "min-h-9 px-2.5 py-1.5 text-sm",
  md: "min-h-11 px-3.5 py-2.5 text-sm",
  lg: "min-h-12 px-4 py-3 text-base",
};

export function inputClasses({
  size = "md",
  mono = false,
  fullWidth = true,
  className = "",
}: {
  size?: InputSize;
  mono?: boolean;
  /** Stretch to the container (the default); turn off to size with `className`. */
  fullWidth?: boolean;
  className?: string;
} = {}): string {
  return [
    fullWidth ? "w-full" : "",
    "min-w-0 rounded-control border border-border-strong bg-surface-canvas text-ink-primary shadow-[0_1px_0_rgb(0_0_0/0.35)_inset] transition-[border-color,box-shadow] duration-150 placeholder:text-ink-tertiary hover:border-ink-tertiary/70 focus-visible:border-accent focus-visible:shadow-[0_0_0_3px_rgb(167_139_250/0.22)] focus-visible:outline-none disabled:cursor-not-allowed disabled:opacity-50 aria-invalid:border-danger aria-invalid:focus-visible:shadow-[0_0_0_3px_rgb(248_113_113/0.22)]",
    SIZE_CLASS[size],
    mono ? "font-mono tabular-nums" : "",
    className,
  ]
    .filter(Boolean)
    .join(" ");
}

export interface InputProps extends Omit<
  InputHTMLAttributes<HTMLInputElement>,
  "size"
> {
  inputSize?: InputSize;
  mono?: boolean;
  ref?: Ref<HTMLInputElement>;
}

export function Input({
  inputSize = "md",
  mono = false,
  className,
  ...rest
}: InputProps) {
  return (
    <input
      className={inputClasses({ size: inputSize, mono, className })}
      {...rest}
    />
  );
}

export function Label({
  htmlFor,
  optional = false,
  children,
  className = "",
}: {
  htmlFor: string;
  optional?: boolean;
  children: ReactNode;
  className?: string;
}) {
  return (
    <label
      htmlFor={htmlFor}
      className={`text-sm font-semibold text-ink-primary ${className}`}
    >
      {children}
      {optional && (
        <span className="font-normal text-ink-tertiary"> (optional)</span>
      )}
    </label>
  );
}

/** Help text under a control. `tone="error"` is for validation messages (pair with role=alert). */
export function FieldMessage({
  id,
  tone = "hint",
  role,
  children,
  className = "",
}: {
  id?: string;
  tone?: "hint" | "error" | "success";
  role?: "alert" | "status";
  children: ReactNode;
  className?: string;
}) {
  const toneClass =
    tone === "error"
      ? "font-medium text-danger"
      : tone === "success"
        ? "text-success-soft-ink"
        : "text-ink-secondary";
  return (
    <p
      id={id}
      role={role}
      className={`text-sm leading-6 text-pretty ${toneClass} ${className}`}
    >
      {children}
    </p>
  );
}
