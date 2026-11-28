"use client";

import { usePosterFrame } from "@/lib/use-poster-frame";

/**
 * A finished video that shows a real frame before it plays (#217): the poster comes from
 * `usePosterFrame`, and a soft placeholder with a play glyph covers it until a frame is in.
 * It fills its parent (`absolute inset-0`), so the parent sets the size and aspect ratio and
 * must be `relative`. Used by the clip cards, the clip editor and the highlights player.
 */
export function PosterVideo({
  src,
  label,
  onFailed,
  posterWidth,
}: {
  src: string;
  label: string;
  onFailed?: () => void;
  /** Widest poster frame to grab, in pixels; larger views want a sharper one. */
  posterWidth?: number;
}) {
  const { showFrame, videoProps } = usePosterFrame(src, {
    width: posterWidth,
  });

  return (
    <>
      <video
        {...videoProps}
        src={src}
        controls
        playsInline
        onError={onFailed}
        className="absolute inset-0 size-full object-contain"
        aria-label={label}
      />
      {!showFrame && (
        <div
          aria-hidden="true"
          data-testid="clip-frame-placeholder"
          className="pointer-events-none absolute inset-0 flex items-center justify-center bg-[radial-gradient(80%_60%_at_50%_40%,var(--surface-elevated),var(--surface-media))]"
        >
          <span className="flex size-14 items-center justify-center rounded-pill bg-white/10 ring-1 ring-white/20 backdrop-blur-sm motion-safe:animate-pulse">
            <svg viewBox="0 0 20 20" className="ml-0.5 size-6 text-white">
              <polygon points="6,4.5 6,15.5 15,10" fill="currentColor" />
            </svg>
          </span>
        </div>
      )}
    </>
  );
}
