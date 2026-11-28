"use client";

// The floating label over the scrub track (#189): the VOD time under the playhead, the moment
// there (if any) and, when a preview file covers that time, a frame from it.
//
// One muted <video> stays mounted for as long as the timeline is on screen (hidden while the label
// is), so hovering again reuses the loaded file instead of fetching it anew. Its source changes only
// when a different preview file is needed, and it is unloaded on unmount. Seeks are throttled to
// one in flight and at most one per `SEEK_INTERVAL_MS`, so scrubbing never queues up seeks.

import { useEffect, useRef, useState } from "react";

import type { PreviewSource } from "./use-preview-media";

export const SEEK_INTERVAL_MS = 120;

/** Release a media element's file: removing `src` alone keeps the old file loaded. */
export function unloadVideo(video: HTMLVideoElement): void {
  try {
    video.pause();
    video.removeAttribute("src");
    video.load();
  } catch {
    // jsdom and some browsers don't implement every media method; nothing else to free.
  }
}

export function ScrubPreview({
  visible,
  leftPct,
  align,
  timeLabel,
  momentLabel,
  hint,
  source,
  timeS,
  onSourceError,
}: {
  /** Whether the label is shown; the video element stays mounted either way. */
  visible: boolean;
  leftPct: number;
  align: "start" | "center" | "end";
  timeLabel: string;
  momentLabel: string | null;
  hint: string | null;
  /** Preview covering `timeS`, or `null` for the text-only label. */
  source: PreviewSource | null;
  timeS: number;
  onSourceError: (url: string) => void;
}) {
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const pendingRef = useRef<number | null>(null);
  const lastSeekRef = useRef(Number.NEGATIVE_INFINITY);
  const timerRef = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  // The file the mounted video holds; it only changes when another preview is needed.
  const [videoUrl, setVideoUrl] = useState<string | null>(null);
  if (source && source.url !== videoUrl) {
    setVideoUrl(source.url);
  }

  const showFrame = visible && source !== null && source.url === videoUrl;
  const offset = showFrame ? Math.max(0, timeS - source.startS) : null;

  function flush() {
    const video = videoRef.current;
    const target = pendingRef.current;
    if (!video || target === null) return;
    if (video.readyState < 1 || video.seeking) return;
    const wait = SEEK_INTERVAL_MS - (Date.now() - lastSeekRef.current);
    if (wait > 0) {
      clearTimeout(timerRef.current);
      timerRef.current = setTimeout(flush, wait);
      return;
    }
    pendingRef.current = null;
    if (Math.abs(video.currentTime - target) < 0.05) return;
    lastSeekRef.current = Date.now();
    video.currentTime = target;
  }

  // The effect owns `src` (not JSX), so setting it and unloading stay paired: on a file change,
  // on unmount, and through React's dev-mode mount, cleanup and mount again.
  useEffect(() => {
    const video = videoRef.current;
    if (!video || videoUrl === null) return;
    video.src = videoUrl;
    return () => unloadVideo(video);
  }, [videoUrl]);

  useEffect(() => {
    if (offset === null) return;
    pendingRef.current = offset;
    flush();
    // `flush` reads refs only; re-running on `offset` is the throttle's input.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [offset, videoUrl]);

  useEffect(() => () => clearTimeout(timerRef.current), []);

  const translate =
    align === "start"
      ? "translate-x-0"
      : align === "end"
        ? "-translate-x-full"
        : "-translate-x-1/2";

  return (
    <div
      data-testid="scrub-preview"
      data-visible={visible ? "true" : "false"}
      aria-hidden="true"
      style={{ left: `${leftPct}%` }}
      className={`pointer-events-none absolute bottom-full z-30 mb-2 w-40 flex-col overflow-hidden rounded-card border border-border-subtle bg-surface-panel text-xs shadow-card sm:w-48 ${translate} ${
        visible ? "flex" : "hidden"
      }`}
    >
      {videoUrl && (
        <video
          ref={videoRef}
          data-testid="scrub-frame"
          data-showing={showFrame ? "true" : "false"}
          muted
          playsInline
          preload="auto"
          disablePictureInPicture
          onLoadedMetadata={flush}
          onSeeked={flush}
          onError={() => onSourceError(videoUrl)}
          className={`aspect-video w-full bg-surface-media object-cover ${
            showFrame ? "block" : "hidden"
          }`}
        />
      )}
      <div className="flex flex-col gap-0.5 px-2 py-1.5">
        <span className="font-mono font-semibold text-ink-primary tabular-nums">
          {timeLabel}
        </span>
        {momentLabel && (
          <span className="line-clamp-2 font-medium text-ink-primary">
            {momentLabel}
          </span>
        )}
        {hint && <span className="text-ink-tertiary">{hint}</span>}
      </div>
    </div>
  );
}
