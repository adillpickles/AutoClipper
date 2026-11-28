// The brand motif shared with the landing page (site/): a stream's chat activity as bars, with
// the biggest spikes lit in Peak Amber and pinned with their rank. Decorative: it illustrates
// how AutoClipper picks moments and carries no data, so it is hidden from assistive tech.

export interface SignalPeak {
  /** Where the spike sits, 0–1 across the strip. */
  at: number;
  rank: number;
}

const DEFAULT_PEAKS: SignalPeak[] = [
  { at: 0.61, rank: 1 },
  { at: 0.84, rank: 2 },
  { at: 0.23, rank: 3 },
];

/**
 * Bar heights (0–1) for a strip of `count` bars: a deterministic murmur of chat with a
 * spike at each peak. No randomness, so server and client renders match.
 */
export function signalBars(
  count: number,
  peaks: SignalPeak[] = DEFAULT_PEAKS,
): { height: number; hot: boolean }[] {
  return Array.from({ length: count }, (_, index) => {
    const x = count === 1 ? 0 : index / (count - 1);
    const murmur =
      0.16 +
      0.1 * Math.abs(Math.sin(index * 1.73)) +
      0.08 * Math.abs(Math.sin(index * 0.41 + 1.2)) +
      0.05 * Math.abs(Math.cos(index * 2.9));
    let spike = 0;
    for (const peak of peaks) {
      const distance = (x - peak.at) * count;
      const strength = 1 - (peak.rank - 1) * 0.12;
      spike = Math.max(spike, strength * Math.exp(-(distance * distance) / 3));
    }
    const height = Math.min(1, murmur + spike * (1 - murmur));
    return { height, hot: spike > 0.35 };
  });
}

export function SignalStrip({
  bars = 72,
  peaks = DEFAULT_PEAKS,
  pins = true,
  animate = true,
  className = "",
}: {
  bars?: number;
  peaks?: SignalPeak[];
  /** Pin each peak with its rank. */
  pins?: boolean;
  /** Play the one-time rise on mount (motion-safe only). */
  animate?: boolean;
  className?: string;
}) {
  const data = signalBars(bars, peaks);
  return (
    <div
      aria-hidden="true"
      className={`relative ${pins ? "pt-7" : ""} ${className}`}
    >
      {(pins ? peaks : []).map((peak) => (
        <span
          key={peak.rank}
          className="absolute top-0 -translate-x-1/2 rounded-pill bg-accent px-1.5 py-px font-mono text-[0.6875rem] leading-4 font-bold text-accent-ink after:absolute after:top-full after:left-1/2 after:h-2.5 after:w-px after:bg-accent motion-safe:animate-dialog-in"
          style={{
            left: `${peak.at * 100}%`,
            animationDelay: `${700 + peak.rank * 120}ms`,
          }}
        >
          #{peak.rank}
        </span>
      ))}
      <div className="flex h-full items-end gap-[2px] sm:gap-[3px]">
        {data.map((bar, index) => (
          <span
            key={index}
            className={`block flex-1 origin-bottom rounded-[2px] ${
              animate ? "motion-safe:animate-signal-rise" : ""
            } ${bar.hot ? "bg-signal" : "bg-border-strong"}`}
            style={{
              height: `${Math.round(bar.height * 100)}%`,
              animationDelay: `${index * 9}ms`,
            }}
          />
        ))}
      </div>
    </div>
  );
}
