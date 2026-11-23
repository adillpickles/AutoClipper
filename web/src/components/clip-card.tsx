"use client";

import Link from "next/link";
import { useState } from "react";

import type { Candidate, Export } from "@/lib/api/types";
import { formatTime } from "@/lib/format";
import { reasonFor } from "@/lib/reason";

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

export function ClipCard({
  jobId,
  candidate,
  scoreFraction,
  clip,
  videoId,
  onRetry,
}: {
  jobId: string;
  candidate: Candidate;
  scoreFraction: number;
  clip: ClipExport;
  videoId: string;
  onRetry: () => void;
}) {
  const [videoFailed, setVideoFailed] = useState(false);
  const span = `${formatTime(candidate.start_s)}–${formatTime(candidate.end_s)}`;
  const ready =
    clip.kind === "export" && clip.export.state === "done" && clip.url !== null;
  const filename = `autoclipper_${videoId}_${candidate.start_s}-${candidate.end_s}.mp4`;

  return (
    <li className="flex flex-col overflow-hidden rounded-card border border-border-subtle bg-surface-panel shadow-card transition-colors hover:border-border-strong">
      <div className="relative aspect-[9/16] w-full bg-surface-media">
        {ready && !videoFailed ? (
          <video
            src={clip.url ?? undefined}
            controls
            playsInline
            preload="metadata"
            onError={() => setVideoFailed(true)}
            className="absolute inset-0 size-full object-cover"
            aria-label={`Clip ${candidate.rank}, ${span} in the stream`}
          />
        ) : (
          <div className="absolute inset-0 flex flex-col items-center justify-center gap-2.5 p-5 text-center text-sm text-zinc-300">
            <ClipPlaceholder
              clip={clip}
              videoFailed={videoFailed}
              onRetry={onRetry}
            />
          </div>
        )}
        <span className="absolute top-3 left-3 flex size-9 items-center justify-center rounded-pill bg-surface-media/85 font-mono text-sm font-semibold text-white ring-1 ring-white/30 backdrop-blur-xs">
          <span className="sr-only">Rank </span>
          {candidate.rank}
        </span>
      </div>

      <div className="flex flex-1 flex-col gap-3 p-4">
        <div className="flex flex-wrap items-center justify-between gap-2 text-xs">
          <span
            className={`rounded-pill border px-2.5 py-0.5 font-medium ${SOURCE_STYLE[candidate.source]}`}
          >
            {SOURCE_LABEL[candidate.source]}
          </span>
          <span className="font-mono text-ink-secondary tabular-nums">
            <span className="sr-only">In the stream at </span>
            {span}
          </span>
        </div>
        <p className="text-sm leading-snug font-medium text-pretty text-ink-primary">
          {reasonFor(candidate)}
        </p>
        <div className="flex items-center gap-2.5">
          <span className="text-xs font-medium text-ink-tertiary">Score</span>
          <div
            role="meter"
            aria-label="Score within this stream"
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={Math.round(scoreFraction * 100)}
            aria-valuetext={`Rank ${candidate.rank} by score in this stream`}
            className="h-1.5 flex-1 overflow-hidden rounded-pill bg-surface-subtle"
          >
            <div
              className="h-full rounded-pill bg-signal"
              style={{
                width: `${Math.max(4, Math.round(scoreFraction * 100))}%`,
              }}
            />
          </div>
        </div>
        <div className="mt-auto flex flex-wrap items-center gap-2 pt-1.5">
          {ready ? (
            <a
              href={clip.url ?? undefined}
              download={filename}
              className="inline-flex rounded-control bg-accent px-4 py-2 text-sm font-semibold text-accent-ink shadow-card transition-colors hover:bg-accent-hover focus-visible:ring-2 focus-visible:ring-focus-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface-panel focus-visible:outline-none"
            >
              Download<span className="sr-only"> clip {candidate.rank}</span>
            </a>
          ) : (
            <button
              type="button"
              disabled
              className="inline-flex cursor-not-allowed rounded-control bg-surface-subtle px-4 py-2 text-sm font-semibold text-ink-tertiary"
            >
              Download
              <span className="sr-only">
                {" "}
                clip {candidate.rank} (not ready yet)
              </span>
            </button>
          )}
          <Link
            href={`/jobs/${jobId}/results/${candidate.candidate_id}`}
            className="inline-flex rounded-control border border-border-strong bg-surface-elevated px-3.5 py-2 text-sm font-medium text-ink-primary transition-colors hover:bg-surface-subtle focus-visible:ring-2 focus-visible:ring-focus-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface-panel focus-visible:outline-none"
          >
            Edit <span className="sr-only">clip {candidate.rank}</span>
          </Link>
        </div>
      </div>
    </li>
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
      <p>
        The clip is ready but can&apos;t be played here. Download it instead.
      </p>
    );
  }
  if (clip.kind === "unavailable") return <p>{clip.message}</p>;
  if (
    clip.kind === "error" ||
    (clip.kind === "export" && clip.export.state === "failed")
  ) {
    const message =
      clip.kind === "error"
        ? clip.message
        : (clip.export.error?.message ?? "Rendering failed.");
    return (
      <>
        <p className="text-red-300">{message}</p>
        <button
          type="button"
          onClick={onRetry}
          className="rounded-control border border-white/25 bg-white/10 px-3 py-1.5 text-sm font-medium text-white transition-colors hover:bg-white/20 focus-visible:ring-2 focus-visible:ring-focus-ring-media focus-visible:ring-offset-2 focus-visible:ring-offset-surface-media focus-visible:outline-none"
        >
          Try again
        </button>
      </>
    );
  }
  if (clip.kind === "pending" || clip.export.state === "queued") {
    return <p>Waiting to render…</p>;
  }
  const progress = clip.export.progress;
  return (
    <>
      <span
        className="size-6 animate-spin rounded-pill border-2 border-accent border-t-transparent"
        aria-hidden="true"
      />
      <p>{progress ? `Rendering… ${progress.message}` : "Rendering…"}</p>
    </>
  );
}
