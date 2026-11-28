export type ProgressTone = "accent" | "signal" | "success";

const FILL_CLASS: Record<ProgressTone, string> = {
  accent: "bg-accent",
  signal: "bg-signal",
  success: "bg-success",
};

/** Clamp a percentage to 0–100 and round it; `null` and non-finite values stay `null`. */
export function clampPercent(value: number | null | undefined): number | null {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return null;
  }
  return Math.round(Math.min(100, Math.max(0, value)));
}

/**
 * A determinate progress bar (`value` 0–100), or an indeterminate one when `value` is null:
 * a sliding segment that holds still under reduced motion.
 */
export function ProgressBar({
  value,
  label,
  tone = "accent",
  size = "md",
  className = "",
}: {
  value: number | null;
  label: string;
  tone?: ProgressTone;
  size?: "sm" | "md";
  className?: string;
}) {
  const percent = clampPercent(value);
  return (
    <div
      role="progressbar"
      aria-label={label}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={percent ?? undefined}
      className={`relative w-full overflow-hidden rounded-pill bg-surface-subtle ${
        size === "sm" ? "h-1.5" : "h-2"
      } ${className}`}
    >
      {percent === null ? (
        <div
          className={`absolute inset-y-0 left-0 w-1/3 rounded-pill ${FILL_CLASS[tone]} motion-safe:animate-pulse`}
        />
      ) : (
        <div
          className={`h-full rounded-pill ${FILL_CLASS[tone]} transition-[width] duration-300 ease-out`}
          style={{ width: `${percent}%` }}
        />
      )}
    </div>
  );
}

/** A small spinner; spins only when motion is allowed. */
export function Spinner({
  className = "",
  tone = "accent",
}: {
  className?: string;
  tone?: ProgressTone;
}) {
  const border =
    tone === "signal"
      ? "border-signal"
      : tone === "success"
        ? "border-success"
        : "border-accent";
  return (
    <span
      aria-hidden="true"
      className={`inline-block size-5 shrink-0 rounded-full border-2 ${border} border-t-transparent motion-safe:animate-spin ${className}`}
    />
  );
}
