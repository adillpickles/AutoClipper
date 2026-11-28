"use client";

// A real frame on a finished video before it plays (#217). A `<video preload="metadata">` with
// no poster shows black until the browser decodes a frame, so the frame is grabbed separately
// (`clipPoster`, from a little way into the video) and the visible video stays
// `preload="none"`. If no poster can be made (say the server sends no CORS headers), the video
// preloads its metadata and seeks to a frame instead, rewinding to the start when it's first
// played. Used by the clip cards, the clip editor's rendered clip and the highlights player.

import {
  useEffect,
  useRef,
  useState,
  type ReactEventHandler,
  type RefObject,
} from "react";

import { clipPoster, posterTime } from "@/lib/clip-poster";

export interface PosterFrame {
  /** True once the video shows a real frame (a poster, a seeked frame or playback). */
  showFrame: boolean;
  /** Spread onto the `<video>` (with its own `src`). */
  videoProps: {
    ref: RefObject<HTMLVideoElement | null>;
    poster: string | undefined;
    preload: "none" | "metadata";
    onLoadedMetadata: ReactEventHandler<HTMLVideoElement>;
    onSeeked: ReactEventHandler<HTMLVideoElement>;
    onPlaying: ReactEventHandler<HTMLVideoElement>;
    onPlay: ReactEventHandler<HTMLVideoElement>;
  };
}

export function usePosterFrame(
  src: string,
  { width }: { width?: number } = {},
): PosterFrame {
  const [poster, setPoster] = useState<{ src: string; url: string } | null>(
    null,
  );
  const [fallbackFor, setFallbackFor] = useState<string | null>(null);
  const [frameFor, setFrameFor] = useState<string | null>(null);
  const seekedTo = useRef<number | null>(null);
  const videoRef = useRef<HTMLVideoElement | null>(null);

  useEffect(() => {
    let cancelled = false;
    clipPoster(src, { width }).then(
      (url) => {
        if (!cancelled) setPoster({ src, url });
      },
      () => {
        if (!cancelled) setFallbackFor(src);
      },
    );
    return () => {
      cancelled = true;
    };
  }, [src, width]);

  const posterUrl = poster?.src === src ? poster.url : undefined;
  const fallback = fallbackFor === src;

  // A `preload="none"` video that hasn't started loading won't always pick up a later
  // `preload="metadata"` by itself.
  useEffect(() => {
    const video = videoRef.current;
    if (fallback && video && video.readyState === 0 && video.paused) {
      video.load();
    }
  }, [fallback]);

  return {
    showFrame: posterUrl !== undefined || frameFor === src,
    videoProps: {
      ref: videoRef,
      poster: posterUrl,
      preload: fallback ? "metadata" : "none",
      onLoadedMetadata: (event) => {
        if (!fallback || posterUrl) return;
        const video = event.currentTarget;
        if (!video.paused || video.currentTime > 0) return;
        const at = posterTime(video.duration);
        seekedTo.current = at;
        video.currentTime = at;
      },
      onSeeked: () => setFrameFor(src),
      onPlaying: () => setFrameFor(src),
      onPlay: (event) => {
        // Start from the beginning, not the frame the fallback seeked to for the thumbnail.
        const video = event.currentTarget;
        const at = seekedTo.current;
        seekedTo.current = null;
        if (at !== null && Math.abs(video.currentTime - at) < 0.05) {
          video.currentTime = 0;
        }
      },
    },
  };
}
