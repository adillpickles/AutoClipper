"use client";

import { useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { usePosterFrame } from "@/lib/use-poster-frame";

/**
 * A finished video that shows a real frame before it plays (#217): the poster comes from
 * `usePosterFrame`, and a soft placeholder with a play glyph covers it until a frame is in.
 * It fills its parent (`absolute inset-0`), so the parent sets the size and aspect ratio and
 * must be `relative`. Used by the clip cards, the clip editor and the highlights player.
 *
 * If the video fails to load, the placeholder goes away either way (#238). With `onFailed` the
 * parent decides what replaces the player (the clip cards show their own message); without it,
 * a short "can't be played" note with a Retry button takes its place, and Retry loads the file
 * again from scratch.
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
  const [attempt, setAttempt] = useState(0);
  const [failed, setFailed] = useState(false);
  const statusRef = useRef<HTMLDivElement | null>(null);

  // After a Retry that fails again, keep keyboard focus on the message instead of <body>.
  useEffect(() => {
    if (failed && !onFailed && attempt > 0) statusRef.current?.focus();
  }, [failed, onFailed, attempt]);

  if (failed && !onFailed) {
    return (
      <div
        ref={statusRef}
        tabIndex={-1}
        role="status"
        className="absolute inset-0 flex flex-col items-center justify-center gap-3 bg-[radial-gradient(80%_60%_at_50%_40%,var(--surface-elevated),var(--surface-media))] p-4 text-center text-sm text-ink-secondary"
      >
        <p className="max-w-52 text-pretty">
          This video can&apos;t be played here. Try again, or download it
          instead.
        </p>
        <Button
          variant="media"
          size="sm"
          onClick={() => {
            setFailed(false);
            setAttempt((n) => n + 1);
          }}
        >
          Retry
        </Button>
      </div>
    );
  }

  return (
    <PosterVideoPlayer
      // A fresh element per attempt: a new load and a new poster capture.
      key={attempt}
      src={src}
      label={label}
      posterWidth={posterWidth}
      failed={failed}
      // Retry removes its own button: move focus to the new player so it isn't lost.
      autoFocus={attempt > 0}
      onError={() => {
        setFailed(true);
        onFailed?.();
      }}
    />
  );
}

function PosterVideoPlayer({
  src,
  label,
  posterWidth,
  failed,
  autoFocus,
  onError,
}: {
  src: string;
  label: string;
  posterWidth?: number;
  failed: boolean;
  autoFocus: boolean;
  onError: () => void;
}) {
  const { showFrame, videoProps } = usePosterFrame(src, {
    width: posterWidth,
  });
  const videoRef = videoProps.ref;

  useEffect(() => {
    if (autoFocus) videoRef.current?.focus();
  }, [autoFocus, videoRef]);

  return (
    <>
      <video
        {...videoProps}
        src={src}
        controls
        playsInline
        onError={onError}
        className="absolute inset-0 size-full object-contain"
        aria-label={label}
      />
      {!showFrame && !failed && (
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
