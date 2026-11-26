"use client";

import { useId, useState } from "react";

import { Button, buttonClasses, ButtonLink } from "@/components/ui/button";
import type { Candidate, Export } from "@/lib/api/types";
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
  const [whyOpen, setWhyOpen] = useState(false);
  const span = `${formatTime(candidate.start_s)}–${formatTime(candidate.end_s)}`;
  const title = titleFor(candidate);
  const reason = reasonFor(candidate);
  const details = signalDetailsFor(candidate);
  const whyPanelId = useId();
  const ready =
    clip.kind === "export" && clip.export.state === "done" && clip.url !== null;
  const filename = `autoclipper_${videoId}_${candidate.start_s}-${candidate.end_s}.mp4`;

  return (
    <li className="flex min-w-0 flex-col overflow-hidden rounded-card border border-border-subtle bg-surface-panel shadow-card transition-colors hover:border-border-strong">
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
          <div className="absolute inset-0 flex flex-col items-center justify-center gap-3 p-5 text-center text-sm text-slate-200">
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

      <div className="flex flex-1 flex-col gap-3 p-3.5 sm:p-4">
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
        <div className="flex flex-col gap-1">
          <h3 className="text-base leading-snug font-semibold text-pretty break-words text-ink-primary">
            {title}
          </h3>
          <p className="text-xs leading-relaxed text-pretty break-words text-ink-secondary">
            {reason}
          </p>
        </div>
        {details.length > 0 && (
          <div className="flex flex-col gap-2">
            <button
              type="button"
              onClick={() => setWhyOpen((open) => !open)}
              aria-expanded={whyOpen}
              aria-controls={whyPanelId}
              className="inline-flex min-h-8 items-center gap-1.5 self-start rounded-pill border border-border-subtle bg-surface-subtle/70 px-2.5 py-1 text-xs font-medium text-ink-secondary transition-colors hover:border-border-strong hover:text-ink-primary focus-visible:ring-2 focus-visible:ring-accent focus-visible:outline-none"
            >
              <span>
                Why
                <span className="sr-only">
                  {" "}
                  clip {candidate.rank} was picked
                </span>
              </span>
              <span aria-hidden="true" className="text-[10px] leading-none">
                {whyOpen ? "▴" : "▾"}
              </span>
            </button>
            {whyOpen && (
              <dl
                id={whyPanelId}
                aria-label={`Contributing signals for clip ${candidate.rank}`}
                className="flex flex-col gap-1.5 rounded-control border border-border-subtle bg-surface-subtle/50 p-2.5 text-xs"
              >
                {details.map((item, idx) => (
                  <div
                    key={`${item.kind}-${idx}`}
                    className="flex flex-col gap-0.5 sm:flex-row sm:items-baseline sm:justify-between sm:gap-2"
                  >
                    <dt className="shrink-0 font-medium text-ink-primary">
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
              className={buttonClasses({ variant: "primary", size: "md" })}
            >
              Download<span className="sr-only"> clip {candidate.rank}</span>
            </a>
          ) : (
            <button
              type="button"
              disabled
              className="inline-flex min-h-10 cursor-not-allowed items-center justify-center rounded-control bg-surface-subtle px-4 py-2 text-sm font-semibold text-ink-tertiary"
            >
              Download
              <span className="sr-only">
                {" "}
                clip {candidate.rank} (not ready yet)
              </span>
            </button>
          )}
          <ButtonLink
            href={`/jobs/${jobId}/results/${candidate.candidate_id}`}
            variant="subtle"
            size="md"
          >
            Edit <span className="sr-only">clip {candidate.rank}</span>
          </ButtonLink>
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
      <p role="status" className="max-w-52 text-pretty text-slate-200">
        The clip is ready but can&apos;t be played here. Download it below
        instead.
      </p>
    );
  }
  if (clip.kind === "unavailable") {
    return (
      <p role="status" className="max-w-52 text-pretty text-slate-200">
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
        <p className="max-w-56 text-pretty text-red-200">{message}</p>
        <Button variant="media" size="sm" onClick={onRetry}>
          Try again
        </Button>
      </div>
    );
  }
  if (clip.kind === "pending" || clip.export.state === "queued") {
    return (
      <div role="status" className="flex flex-col items-center gap-2">
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
        className="size-6 rounded-pill border-2 border-accent border-t-transparent motion-safe:animate-spin"
        aria-hidden="true"
      />
      <p className="max-w-52 text-pretty">
        {progress ? `Rendering… ${progress.message}` : "Rendering…"}
      </p>
    </div>
  );
}
