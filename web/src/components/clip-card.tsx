"use client";

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
  discovered: "bg-sky-100 text-sky-900 dark:bg-sky-400/15 dark:text-sky-200",
  viewer_clips:
    "bg-violet-100 text-violet-900 dark:bg-violet-400/15 dark:text-violet-200",
  both: "bg-emerald-100 text-emerald-900 dark:bg-emerald-400/15 dark:text-emerald-200",
};

export function ClipCard({
  candidate,
  scoreFraction,
  clip,
  videoId,
  onRetry,
}: {
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
    <li className="flex flex-col overflow-hidden rounded-2xl border border-zinc-200 bg-white dark:border-zinc-800 dark:bg-zinc-900">
      <div className="relative aspect-[9/16] w-full bg-zinc-100 dark:bg-zinc-950">
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
          <div className="absolute inset-0 flex flex-col items-center justify-center gap-2 p-4 text-center text-sm text-zinc-600 dark:text-zinc-400">
            <ClipPlaceholder
              clip={clip}
              videoFailed={videoFailed}
              onRetry={onRetry}
            />
          </div>
        )}
        <span className="absolute top-3 left-3 flex size-9 items-center justify-center rounded-full bg-zinc-950/80 font-semibold text-white ring-1 ring-white/40">
          <span className="sr-only">Rank </span>
          {candidate.rank}
        </span>
      </div>

      <div className="flex flex-1 flex-col gap-3 p-4">
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <span
            className={`rounded-full px-2.5 py-0.5 font-medium ${SOURCE_STYLE[candidate.source]}`}
          >
            {SOURCE_LABEL[candidate.source]}
          </span>
          <span className="text-zinc-600 tabular-nums dark:text-zinc-400">
            <span className="sr-only">In the stream at </span>
            {span}
          </span>
        </div>
        <p className="font-medium text-pretty">{reasonFor(candidate)}</p>
        <div className="flex items-center gap-2">
          <span className="text-xs text-zinc-600 dark:text-zinc-400">
            Score
          </span>
          <div
            role="meter"
            aria-label="Score within this stream"
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={Math.round(scoreFraction * 100)}
            aria-valuetext={`Rank ${candidate.rank} by score in this stream`}
            className="h-1.5 flex-1 overflow-hidden rounded-full bg-zinc-200 dark:bg-zinc-800"
          >
            <div
              className="h-full rounded-full bg-amber-500"
              style={{
                width: `${Math.max(4, Math.round(scoreFraction * 100))}%`,
              }}
            />
          </div>
        </div>
        <div className="mt-auto pt-1">
          {ready ? (
            <a
              href={clip.url ?? undefined}
              download={filename}
              className="inline-flex rounded-lg bg-amber-500 px-4 py-2 text-sm font-semibold text-zinc-950 hover:bg-amber-400 focus-visible:ring-2 focus-visible:ring-amber-700 focus-visible:outline-none"
            >
              Download<span className="sr-only"> clip {candidate.rank}</span>
            </a>
          ) : (
            <button
              type="button"
              disabled
              className="inline-flex rounded-lg bg-zinc-100 px-4 py-2 text-sm font-semibold text-zinc-600 dark:bg-zinc-800 dark:text-zinc-400"
            >
              Download
              <span className="sr-only">
                {" "}
                clip {candidate.rank} (not ready yet)
              </span>
            </button>
          )}
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
        <p className="text-red-700 dark:text-red-300">{message}</p>
        <button
          type="button"
          onClick={onRetry}
          className="rounded-lg bg-white px-3 py-1.5 text-sm font-medium text-zinc-900 ring-1 ring-zinc-300 hover:bg-zinc-50 focus-visible:ring-2 focus-visible:ring-amber-500 focus-visible:outline-none dark:bg-zinc-900 dark:text-zinc-100 dark:ring-zinc-700"
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
        className="size-6 animate-spin rounded-full border-2 border-amber-500 border-t-transparent"
        aria-hidden="true"
      />
      <p>{progress ? `Rendering… ${progress.message}` : "Rendering…"}</p>
    </>
  );
}
