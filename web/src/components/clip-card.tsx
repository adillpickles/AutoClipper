"use client";

import { useEffect, useId, useRef, useState } from "react";

import { ClipShareMenu } from "@/components/share-menu";
import { Button, buttonClasses, ButtonLink } from "@/components/ui/button";
import type { Candidate, Export, VodSummary } from "@/lib/api/types";
import { clipPoster, posterTime } from "@/lib/clip-poster";
import { buildClipFilename } from "@/lib/filenames";
import { formatTime } from "@/lib/format";
import { reasonFor, signalDetailsFor, titleFor } from "@/lib/reason";

export type ClipExport =
  | { kind: "pending" }
  | { kind: "export"; export: Export; url: string | null }
  | { kind: "unavailable"; message: string }
  | { kind: "error"; message: string };

const SOURCE_LABEL: Record<Candidate["source"], string> = {
  discovered: "Discovered",
  viewer_clips: "Viewer clip",
  both: "Both",
};

const SOURCE_STYLE: Record<Candidate["source"], string> = {
  discovered:
    "border-source-discovered-border bg-source-discovered-soft text-source-discovered-soft-ink",
  viewer_clips:
    "border-source-viewer-border bg-source-viewer-soft text-source-viewer-soft-ink",
  both: "border-source-both-border bg-source-both-soft text-source-both-soft-ink",
};

const SOURCE_DOT: Record<Candidate["source"], string> = {
  discovered: "bg-source-discovered",
  viewer_clips: "bg-source-viewer",
  both: "bg-source-both",
};

export function ClipCard({
  jobId,
  candidate,
  scoreFraction,
  clip,
  videoId,
  vod,
  onRetry,
}: {
  jobId: string;
  candidate: Candidate;
  scoreFraction: number;
  clip: ClipExport;
  videoId: string;
  vod?: Pick<VodSummary, "broadcaster_display_name" | "created_at">;
  onRetry: () => void;
}) {
  const [videoFailed, setVideoFailed] = useState(false);
  const [whyOpen, setWhyOpen] = useState(false);
  const span = `${formatTime(candidate.start_s)}–${formatTime(candidate.end_s)}`;
  const length = formatTime(
    clip.kind === "export"
      ? clip.export.end_s - clip.export.start_s
      : candidate.end_s - candidate.start_s,
  );
  const title = titleFor(candidate);
  const reason = reasonFor(candidate);
  const details = signalDetailsFor(candidate);
  const whyPanelId = useId();
  const ready =
    clip.kind === "export" && clip.export.state === "done" && clip.url !== null;
  const filename =
    (clip.kind === "export" ? clip.export.filename : null) ??
    buildClipFilename({
      channel: vod?.broadcaster_display_name ?? null,
      createdAt: vod?.created_at ?? null,
      title: candidate.title ?? null,
      rank: candidate.rank,
      startS: clip.kind === "export" ? clip.export.start_s : candidate.start_s,
      fallbackId: videoId,
    });
  const percent = Math.round(scoreFraction * 100);

  return (
    <li className="flex min-w-0 flex-col overflow-hidden rounded-card border border-border-subtle bg-surface-panel shadow-card transition-[border-color,box-shadow] duration-200 hover:border-border-strong hover:shadow-panel">
      <div className="relative aspect-[9/16] w-full overflow-hidden bg-surface-media">
        {/* The card plays only the finished 1080x1920 export, never the 16:9 preview. It must
            use `object-contain`: an author `object-fit` overrides the browser's fullscreen
            default, and `object-cover` would crop the vertical clip to a landscape band of the
            screen in fullscreen (#185). In the 9:16 box, contain and cover look the same. */}
        {ready && !videoFailed ? (
          <ClipVideo
            src={clip.url ?? ""}
            label={`Clip ${candidate.rank}, ${span} in the stream`}
            onFailed={() => setVideoFailed(true)}
          />
        ) : (
          <div className="absolute inset-0 flex flex-col items-center justify-center gap-3 bg-[radial-gradient(80%_60%_at_50%_40%,var(--surface-elevated),var(--surface-media))] p-5 text-center text-sm text-ink-secondary">
            <ClipPlaceholder
              clip={clip}
              videoFailed={videoFailed}
              onRetry={onRetry}
            />
          </div>
        )}
        <span
          className={`pointer-events-none absolute top-3 left-3 flex h-8 min-w-8 items-center justify-center rounded-pill px-2 font-mono text-sm font-bold tabular-nums shadow-card ${
            candidate.rank === 1
              ? "bg-signal text-signal-ink"
              : "bg-black/70 text-white ring-1 ring-white/20 backdrop-blur-sm"
          }`}
        >
          <span className="sr-only">Rank </span>
          <span aria-hidden="true" className="opacity-60">
            #
          </span>
          {candidate.rank}
        </span>
        <span className="pointer-events-none absolute top-3 right-3 rounded-pill bg-black/70 px-2 py-0.5 font-mono text-xs font-medium text-white tabular-nums ring-1 ring-white/15 backdrop-blur-sm">
          <span className="sr-only">Length </span>
          {length}
        </span>
      </div>

      <div className="flex flex-1 flex-col gap-3 p-4">
        <div className="flex flex-wrap items-center justify-between gap-2 text-xs">
          <span
            className={`inline-flex items-center gap-1.5 rounded-pill border px-2.5 py-0.5 font-semibold ${SOURCE_STYLE[candidate.source]}`}
          >
            <span
              aria-hidden="true"
              className={`size-1.5 rounded-full ${SOURCE_DOT[candidate.source]}`}
            />
            {SOURCE_LABEL[candidate.source]}
          </span>
          <span className="font-mono text-ink-secondary tabular-nums">
            <span className="sr-only">In the stream at </span>
            {span}
          </span>
        </div>
        <div className="flex flex-col gap-1.5">
          <h3 className="font-display text-[1.0625rem] leading-snug font-semibold tracking-[-0.01em] text-pretty break-words text-ink-primary">
            {title}
          </h3>
          <p className="text-[0.8125rem] leading-5 text-pretty break-words text-ink-secondary">
            {reason}
          </p>
        </div>
        <div className="flex items-center gap-2.5">
          <span className="text-xs font-medium text-ink-tertiary">Score</span>
          <div
            role="meter"
            aria-label="Score within this stream"
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={percent}
            aria-valuetext={`Rank ${candidate.rank} by score in this stream`}
            className="h-1.5 flex-1 overflow-hidden rounded-pill bg-surface-subtle"
          >
            <div
              className="h-full rounded-pill bg-[linear-gradient(90deg,var(--signal-border),var(--signal))]"
              style={{ width: `${Math.max(4, percent)}%` }}
            />
          </div>
        </div>
        {details.length > 0 && (
          <div className="flex flex-col gap-2">
            <button
              type="button"
              onClick={() => setWhyOpen((open) => !open)}
              aria-expanded={whyOpen}
              aria-controls={whyPanelId}
              className="-ml-1.5 inline-flex min-h-8 items-center gap-1.5 self-start rounded-control px-1.5 py-1 text-xs font-semibold text-accent transition-colors hover:bg-accent-soft hover:text-accent-hover focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus-ring"
            >
              <span>
                Why
                <span className="sr-only">
                  {" "}
                  clip {candidate.rank} was picked
                </span>
              </span>
              <svg
                viewBox="0 0 12 12"
                fill="none"
                aria-hidden="true"
                className={`size-3 transition-transform duration-200 ${whyOpen ? "rotate-180" : ""}`}
                stroke="currentColor"
                strokeWidth="1.8"
                strokeLinecap="round"
                strokeLinejoin="round"
              >
                <polyline points="3,4.5 6,7.5 9,4.5" />
              </svg>
            </button>
            {whyOpen && (
              <dl
                id={whyPanelId}
                aria-label={`Contributing signals for clip ${candidate.rank}`}
                className="flex flex-col divide-y divide-border-subtle rounded-control border border-border-subtle bg-surface-canvas/60 px-3 text-xs"
              >
                {details.map((item, idx) => (
                  <div
                    key={`${item.kind}-${idx}`}
                    className="flex flex-col gap-0.5 py-2 sm:flex-row sm:items-baseline sm:justify-between sm:gap-3"
                  >
                    <dt className="shrink-0 font-semibold text-ink-primary">
                      {item.label}
                    </dt>
                    <dd className="text-pretty break-words text-ink-secondary sm:text-right">
                      {item.detail}
                    </dd>
                  </div>
                ))}
              </dl>
            )}
          </div>
        )}
        <div className="mt-auto flex flex-wrap items-center gap-2 pt-1">
          {ready ? (
            <a
              href={clip.url ?? undefined}
              download={filename}
              className={buttonClasses({
                variant: "secondary",
                size: "md",
                className: "flex-1",
              })}
            >
              <DownloadGlyph />
              Download<span className="sr-only"> clip {candidate.rank}</span>
            </a>
          ) : (
            <Button variant="secondary" size="md" disabled className="flex-1">
              <DownloadGlyph />
              Download
              <span className="sr-only">
                {" "}
                clip {candidate.rank} (not ready yet)
              </span>
            </Button>
          )}
          <ButtonLink
            href={`/jobs/${jobId}/results/${candidate.candidate_id}`}
            variant="subtle"
            size="md"
          >
            Edit <span className="sr-only">clip {candidate.rank}</span>
          </ButtonLink>
          <ClipShareMenu {...{ clip, candidate, filename, vod, videoId }} />
        </div>
      </div>
    </li>
  );
}

/**
 * The finished clip with a real frame before it plays (#217). The video itself never preloads
 * (`preload="none"`), so ten cards never queue up behind the browser's per-host connection
 * limit; a poster frame is grabbed separately (`clipPoster`). If that can't be done (say the
 * server sends no CORS headers), the video preloads its metadata and seeks to a frame instead,
 * rewinding to the start when it's first played.
 */
function ClipVideo({
  src,
  label,
  onFailed,
}: {
  src: string;
  label: string;
  onFailed: () => void;
}) {
  const [poster, setPoster] = useState<{ src: string; url: string } | null>(
    null,
  );
  const [fallbackFor, setFallbackFor] = useState<string | null>(null);
  const [frameFor, setFrameFor] = useState<string | null>(null);
  const seekedTo = useRef<number | null>(null);
  const videoRef = useRef<HTMLVideoElement | null>(null);

  useEffect(() => {
    let cancelled = false;
    clipPoster(src).then(
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
  }, [src]);

  const posterUrl = poster?.src === src ? poster.url : undefined;
  const fallback = fallbackFor === src;
  const showFrame = posterUrl !== undefined || frameFor === src;

  // A `preload="none"` video that hasn't started loading won't always pick up a later
  // `preload="metadata"` by itself.
  useEffect(() => {
    const video = videoRef.current;
    if (fallback && video && video.readyState === 0 && video.paused) {
      video.load();
    }
  }, [fallback]);

  return (
    <>
      <video
        ref={videoRef}
        src={src}
        poster={posterUrl}
        controls
        playsInline
        preload={fallback ? "metadata" : "none"}
        onError={onFailed}
        onLoadedMetadata={(event) => {
          if (!fallback || posterUrl) return;
          const video = event.currentTarget;
          if (!video.paused || video.currentTime > 0) return;
          const at = posterTime(video.duration);
          seekedTo.current = at;
          video.currentTime = at;
        }}
        onSeeked={() => setFrameFor(src)}
        onPlaying={() => setFrameFor(src)}
        onPlay={(event) => {
          // Start from the beginning, not the frame the fallback seeked to for the thumbnail.
          const video = event.currentTarget;
          const at = seekedTo.current;
          seekedTo.current = null;
          if (at !== null && Math.abs(video.currentTime - at) < 0.05) {
            video.currentTime = 0;
          }
        }}
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

// Card glyphs use lines and polylines, not <path>: the timeline view embeds this card and
// checks that it draws no activity curve (a <path>) while there's no data for one.
function DownloadGlyph() {
  return (
    <svg
      viewBox="0 0 16 16"
      fill="none"
      aria-hidden="true"
      className="size-4 shrink-0"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <line x1="8" y1="2.5" x2="8" y2="10.5" />
      <polyline points="4.5,7.5 8,11 11.5,7.5" />
      <line x1="3" y1="13.5" x2="13" y2="13.5" />
    </svg>
  );
}

function ClipPlaceholder({
  clip,
  videoFailed,
  onRetry,
}: {
  clip: ClipExport;
  videoFailed: boolean;
  onRetry: () => void;
}) {
  if (videoFailed) {
    return (
      <p role="status" className="max-w-52 text-pretty text-ink-secondary">
        The clip is ready but can&apos;t be played here. Download it below
        instead.
      </p>
    );
  }
  if (clip.kind === "unavailable") {
    return (
      <p role="status" className="max-w-52 text-pretty text-ink-secondary">
        {clip.message}
      </p>
    );
  }
  if (
    clip.kind === "error" ||
    (clip.kind === "export" && clip.export.state === "failed")
  ) {
    const message =
      clip.kind === "error"
        ? clip.message
        : (clip.export.error?.message ?? "Rendering failed.");
    return (
      <div role="alert" className="flex flex-col items-center gap-3">
        <span
          aria-hidden="true"
          className="flex size-9 items-center justify-center rounded-pill border border-danger-border bg-danger-soft font-bold text-danger"
        >
          !
        </span>
        <p className="max-w-56 text-pretty text-danger-soft-ink">{message}</p>
        <Button variant="media" size="sm" onClick={onRetry}>
          Try again
        </Button>
      </div>
    );
  }
  if (clip.kind === "pending" || clip.export.state === "queued") {
    return (
      <div role="status" className="flex flex-col items-center gap-2.5">
        <span
          aria-hidden="true"
          className="size-2.5 rounded-pill bg-accent motion-safe:animate-pulse"
        />
        <p>Waiting to render…</p>
      </div>
    );
  }
  const progress = clip.export.progress;
  return (
    <div role="status" className="flex flex-col items-center gap-2.5">
      <span
        aria-hidden="true"
        className="size-6 rounded-full border-2 border-accent border-t-transparent motion-safe:animate-spin"
      />
      <p className="max-w-52 text-pretty text-ink-primary">
        {progress ? `Rendering… ${progress.message}` : "Rendering…"}
      </p>
    </div>
  );
}
