"use client";

// The "Make highlights video" dialog (PRD § 4, #72): the top moments, all ticked with their
// current trims, an order toggle and a captions toggle; then the render's progress and a
// download button. The server resolves and validates what is sent (§ 7.1 of the contract).

import { useEffect, useRef, useState } from "react";

import { ShareMenu } from "@/components/share-menu";
import { Badge } from "@/components/ui/badge";
import { Button, buttonClasses } from "@/components/ui/button";
import { Dialog, DialogHeader } from "@/components/ui/dialog";
import { ErrorState } from "@/components/ui/error-state";
import { inputClasses } from "@/components/ui/input";
import { ProgressBar, Spinner } from "@/components/ui/progress";
import {
  ApiRequestError,
  getApiClient,
  POLL_INTERVAL_MS,
} from "@/lib/api/client";
import { HIGHLIGHTS_STAGES } from "@/lib/api/stages";
import type {
  Candidate,
  ExportAvailability,
  HighlightsVideo,
} from "@/lib/api/types";
import { buildHighlightsFilename } from "@/lib/filenames";
import { formatTime } from "@/lib/format";
import { rememberedTrims, type SavedTrim } from "@/lib/job-state";
import { titleFor } from "@/lib/reason";

/** The whole video's bound (§ 7.1). */
const MAX_TOTAL_S = 900;
/** Default per-segment maximum when export bounds haven't loaded yet (§ 5, § 7.1). */
const DEFAULT_MAX_SEGMENT_S = 90;

const TRIM_INPUT = inputClasses({
  size: "sm",
  mono: true,
  fullWidth: false,
  className: "w-[5.75rem] px-2",
});

interface Trim {
  checked: boolean;
  start_s: number;
  end_s: number;
}

type Stage =
  | { kind: "picking" }
  | { kind: "making"; highlights: HighlightsVideo | null }
  | { kind: "done"; download: string; duration_s: number; filename: string }
  | { kind: "failed"; message: string };

function segmentBoundsFor(
  availability: ExportAvailability | undefined,
  vodDurationS: number,
): { minStartS: number; maxEndS: number; maxDurationS: number } {
  return {
    minStartS: availability?.min_start_s ?? 0,
    maxEndS: availability?.max_end_s ?? vodDurationS,
    maxDurationS: availability?.max_duration_s ?? DEFAULT_MAX_SEGMENT_S,
  };
}

function isWithinSegmentBounds(
  startS: number,
  endS: number,
  bounds: { minStartS: number; maxEndS: number; maxDurationS: number },
): boolean {
  return (
    Number.isFinite(startS) &&
    Number.isFinite(endS) &&
    startS >= bounds.minStartS &&
    endS <= bounds.maxEndS &&
    startS < endS &&
    endS - startS <= bounds.maxDurationS
  );
}

function initialTrimFor(
  candidate: Candidate,
  saved: SavedTrim | undefined,
  bounds: { minStartS: number; maxEndS: number; maxDurationS: number },
): Trim {
  if (saved && isWithinSegmentBounds(saved.start_s, saved.end_s, bounds)) {
    return { checked: true, start_s: saved.start_s, end_s: saved.end_s };
  }
  return { checked: true, start_s: candidate.start_s, end_s: candidate.end_s };
}

export function HighlightsDialog({
  jobId,
  videoId,
  channelName,
  vodCreatedAt,
  vodDurationS,
  candidates,
  initialTrims,
  exportBounds,
  captionsStatus = "available",
  onClose,
}: {
  jobId: string;
  videoId: string;
  channelName?: string | null;
  vodCreatedAt?: string | null;
  vodDurationS: number;
  candidates: Candidate[];
  initialTrims?: Record<string, SavedTrim>;
  exportBounds?: Record<string, ExportAvailability>;
  captionsStatus?: "checking" | "available" | "unavailable" | "unknown";
  onClose: () => void;
}) {
  const captionsAllowed =
    captionsStatus === "available" || captionsStatus === "unknown";
  // Store only in-dialog edits so trims arriving asynchronously from remembered exports
  // apply automatically until the user edits that row in the picker.
  const [overrides, setOverrides] = useState<Record<string, Partial<Trim>>>({});
  const effectiveInitialTrims = initialTrims ?? rememberedTrims(jobId);
  const trims: Record<string, Trim> = Object.fromEntries(
    candidates.map((candidate) => {
      const bounds = segmentBoundsFor(
        exportBounds?.[candidate.candidate_id],
        vodDurationS,
      );
      const base = initialTrimFor(
        candidate,
        effectiveInitialTrims[candidate.candidate_id],
        bounds,
      );
      const override = overrides[candidate.candidate_id];
      return [candidate.candidate_id, { ...base, ...override }];
    }),
  );
  const [order, setOrder] = useState<"chronological" | "rank">("chronological");
  const [captions, setCaptions] = useState(false);
  const [stage, setStage] = useState<Stage>({ kind: "picking" });
  const running = useRef(false);
  const bodyRef = useRef<HTMLDivElement | null>(null);
  const downloadRef = useRef<HTMLAnchorElement | null>(null);
  const retryRef = useRef<HTMLButtonElement | null>(null);
  const previousStage = useRef<Stage["kind"]>(stage.kind);

  // Each stage replaces the controls of the one before, so the focused control (say the
  // "Make highlights video" button) disappears. Move focus to the new stage's main control,
  // or to the dialog body, so keyboard users stay inside the dialog.
  useEffect(() => {
    if (previousStage.current === stage.kind) return;
    previousStage.current = stage.kind;
    const target =
      stage.kind === "done"
        ? downloadRef.current
        : stage.kind === "failed"
          ? retryRef.current
          : bodyRef.current;
    (target ?? bodyRef.current)?.focus();
  }, [stage.kind]);

  // Unmounting cancels any in-flight render's polling. The cleanup runs only when the dialog
  // truly unmounts (not on parent rerenders), or polling would stop early. Escape and focus
  // handling live in the shared Dialog.
  useEffect(() => {
    return () => {
      running.current = false;
    };
  }, []);

  const segments = candidates
    .filter((candidate) => trims[candidate.candidate_id]?.checked)
    .map((candidate) => ({
      candidate_id: candidate.candidate_id,
      start_s: trims[candidate.candidate_id].start_s,
      end_s: trims[candidate.candidate_id].end_s,
    }));
  const invalidSegment = segments.find((segment) => {
    const bounds = segmentBoundsFor(
      exportBounds?.[segment.candidate_id],
      vodDurationS,
    );
    return !isWithinSegmentBounds(segment.start_s, segment.end_s, bounds);
  });
  const totalS = segments.reduce(
    (sum, segment) => sum + (segment.end_s - segment.start_s),
    0,
  );

  async function make() {
    if (running.current) return;
    running.current = true;
    setStage({ kind: "making", highlights: null });
    const client = getApiClient();
    try {
      let current = (
        await client.createHighlights({
          job_id: jobId,
          segments,
          order,
          captions: captionsAllowed ? captions : false,
        })
      ).highlights;
      while (
        running.current &&
        current.state !== "done" &&
        current.state !== "failed"
      ) {
        setStage({ kind: "making", highlights: current });
        await new Promise((resolve) => setTimeout(resolve, POLL_INTERVAL_MS));
        if (!running.current) return;
        current = await client.getHighlights(current.highlights_id);
      }
      if (!running.current) return;
      if (current.state === "failed") {
        setStage({
          kind: "failed",
          message:
            current.error?.message ?? "Rendering the highlights video failed.",
        });
        return;
      }
      setStage({
        kind: "done",
        download: client.resolveUrl(current.download_url ?? ""),
        duration_s: current.duration_s ?? 0,
        filename:
          current.filename ??
          buildHighlightsFilename({
            channel: channelName ?? null,
            createdAt: vodCreatedAt ?? null,
            fallbackId: videoId,
          }),
      });
    } catch (error) {
      if (!running.current) return;
      const message =
        error instanceof ApiRequestError
          ? error.error.message
          : "Can't reach the server to make the highlights video.";
      setStage({ kind: "failed", message });
    } finally {
      running.current = false;
    }
  }

  const sequence = sequenceFor(candidates, trims, order);
  const overBudget = !invalidSegment && totalS > MAX_TOTAL_S;
  const canMake =
    segments.length > 0 &&
    totalS <= MAX_TOTAL_S &&
    invalidSegment === undefined;

  return (
    <Dialog
      labelledBy="highlights-title"
      describedBy="highlights-status"
      onClose={onClose}
      size="lg"
    >
      <DialogHeader
        titleId="highlights-title"
        title="Make highlights video"
        descriptionId="highlights-status"
        description={
          stage.kind === "done"
            ? `Done. ${formatTime(stage.duration_s)} of highlights.`
            : stage.kind === "making" && stage.highlights?.state === "queued"
              ? "Queued to render. It will start automatically."
              : stage.kind === "making" && stage.highlights?.state === "running"
                ? (stage.highlights.progress?.message ??
                  "Rendering your highlights video.")
                : "One 16:9 video from the ticked moments, joined in the chosen order."
        }
        onClose={onClose}
      />

      <div
        ref={bodyRef}
        tabIndex={-1}
        className="flex min-h-0 flex-1 flex-col gap-5 overflow-y-auto px-5 py-5 focus:outline-none sm:px-6"
      >
        {stage.kind === "picking" && (
          <>
            <section
              aria-label="Preview of the video"
              className="flex flex-col gap-3"
            >
              <div className="flex flex-wrap items-center justify-between gap-3">
                <fieldset className="flex min-w-0 flex-col gap-1.5">
                  <legend className="sr-only">Order</legend>
                  <div className="inline-flex rounded-control border border-border-control bg-surface-canvas p-0.5">
                    {(
                      [
                        { value: "chronological", label: "Stream order" },
                        { value: "rank", label: "Best first" },
                      ] as const
                    ).map((option) => (
                      <label
                        key={option.value}
                        htmlFor={`order-${option.value}`}
                        className="inline-flex min-h-8 cursor-pointer items-center rounded-[0.375rem] px-3 text-xs font-semibold text-ink-tertiary transition-colors hover:text-ink-primary has-checked:bg-surface-subtle has-checked:text-ink-primary has-checked:shadow-card has-focus-visible:outline-2 has-focus-visible:outline-offset-2 has-focus-visible:outline-focus-ring"
                      >
                        <input
                          type="radio"
                          id={`order-${option.value}`}
                          name="highlights-order"
                          checked={order === option.value}
                          onChange={() => setOrder(option.value)}
                          className="sr-only"
                        />
                        {option.label}
                      </label>
                    ))}
                  </div>
                </fieldset>
                <p className="font-mono text-xs text-ink-secondary tabular-nums">
                  {formatTime(totalS)}{" "}
                  <span className="text-ink-tertiary">
                    / {formatTime(MAX_TOTAL_S)} max
                  </span>
                </p>
              </div>

              <SequencePreview sequence={sequence} totalS={totalS} />
            </section>

            <fieldset className="flex min-w-0 flex-col gap-2">
              <legend className="mb-2 text-sm font-semibold text-ink-primary">
                Moments
                <span className="ml-2 font-normal text-ink-tertiary">
                  Untick to leave out, edit seconds to trim
                </span>
              </legend>
              <ul
                role="list"
                className="flex flex-col divide-y divide-border-subtle overflow-hidden rounded-card border border-border-subtle"
              >
                {candidates.map((candidate) => {
                  const trim = trims[candidate.candidate_id];
                  const segBounds = segmentBoundsFor(
                    exportBounds?.[candidate.candidate_id],
                    vodDurationS,
                  );
                  const isTrimmed =
                    Math.abs(trim.start_s - candidate.start_s) > 1e-6 ||
                    Math.abs(trim.end_s - candidate.end_s) > 1e-6;
                  const position = sequence.findIndex(
                    (item) => item.candidate_id === candidate.candidate_id,
                  );
                  return (
                    <li
                      key={candidate.candidate_id}
                      className={`grid grid-cols-[auto_minmax(0,1fr)] items-center gap-x-3 gap-y-2 px-3 py-3 transition-colors sm:grid-cols-[auto_minmax(0,1fr)_auto] sm:px-4 ${
                        trim.checked ? "bg-surface-elevated" : "bg-transparent"
                      }`}
                    >
                      <input
                        type="checkbox"
                        checked={trim.checked}
                        onChange={(event) =>
                          setOverrides((current) => ({
                            ...current,
                            [candidate.candidate_id]: {
                              ...current[candidate.candidate_id],
                              checked: event.target.checked,
                            },
                          }))
                        }
                        aria-label={`Include moment at ${formatTime(trim.start_s)}`}
                        className="size-4 cursor-pointer"
                      />
                      <div className="flex min-w-0 items-center gap-2.5">
                        <span
                          className={`flex h-6 min-w-8 shrink-0 items-center justify-center rounded-[0.375rem] px-1 font-mono text-xs font-semibold tabular-nums ${
                            trim.checked
                              ? "bg-accent-soft text-accent-soft-ink"
                              : "bg-surface-subtle text-ink-tertiary"
                          }`}
                        >
                          #{candidate.rank}
                        </span>
                        <div className="flex min-w-0 flex-col">
                          <span
                            className={`truncate text-sm font-semibold ${
                              trim.checked
                                ? "text-ink-primary"
                                : "text-ink-tertiary line-through decoration-ink-tertiary/50"
                            }`}
                          >
                            {titleFor(candidate)}
                          </span>
                          <span className="text-xs text-ink-tertiary">
                            {trim.checked && position >= 0
                              ? `Plays ${ordinal(position + 1)}`
                              : "Left out"}
                            {isTrimmed && (
                              <span className="ml-2 font-semibold text-signal-soft-ink">
                                Trimmed
                              </span>
                            )}
                          </span>
                        </div>
                      </div>
                      <div className="col-span-2 flex flex-wrap items-center gap-2 pl-7 sm:col-span-1 sm:pl-0">
                        <label
                          className="sr-only"
                          htmlFor={`start-${candidate.candidate_id}`}
                        >
                          Start of the moment, seconds
                        </label>
                        <input
                          id={`start-${candidate.candidate_id}`}
                          type="number"
                          step="any"
                          min={segBounds.minStartS}
                          max={segBounds.maxEndS}
                          value={trim.start_s}
                          disabled={!trim.checked}
                          onChange={(event) =>
                            setOverrides((current) => ({
                              ...current,
                              [candidate.candidate_id]: {
                                ...current[candidate.candidate_id],
                                start_s: Number(event.target.value),
                              },
                            }))
                          }
                          className={TRIM_INPUT}
                        />
                        <span aria-hidden="true" className="text-ink-tertiary">
                          –
                        </span>
                        <label
                          className="sr-only"
                          htmlFor={`end-${candidate.candidate_id}`}
                        >
                          End of the moment, seconds
                        </label>
                        <input
                          id={`end-${candidate.candidate_id}`}
                          type="number"
                          step="any"
                          min={segBounds.minStartS}
                          max={segBounds.maxEndS}
                          value={trim.end_s}
                          disabled={!trim.checked}
                          onChange={(event) =>
                            setOverrides((current) => ({
                              ...current,
                              [candidate.candidate_id]: {
                                ...current[candidate.candidate_id],
                                end_s: Number(event.target.value),
                              },
                            }))
                          }
                          className={TRIM_INPUT}
                        />
                        <span
                          className="w-11 text-right font-mono text-xs text-ink-secondary tabular-nums"
                          title={`Ends at ${formatTime(trim.end_s)} in the stream`}
                        >
                          {formatTime(trim.end_s - trim.start_s)}
                        </span>
                      </div>
                    </li>
                  );
                })}
              </ul>
            </fieldset>

            <div className="flex items-start gap-3 rounded-card border border-border-subtle px-3 py-3 sm:px-4">
              <input
                type="checkbox"
                id="highlights-captions"
                checked={captionsAllowed ? captions : false}
                disabled={!captionsAllowed}
                onChange={(event) => setCaptions(event.target.checked)}
                className="mt-0.5 size-4 cursor-pointer disabled:cursor-not-allowed"
              />
              <label
                htmlFor="highlights-captions"
                className="flex flex-col text-sm font-semibold text-ink-primary"
              >
                Burn in captions
                {captionsStatus === "checking" && (
                  <span className="text-xs font-normal text-ink-tertiary">
                    (checking…)
                  </span>
                )}
                {captionsStatus === "unavailable" && (
                  <span className="text-xs font-normal text-ink-tertiary">
                    (needs uv sync --extra captions and FFmpeg libass)
                  </span>
                )}
              </label>
            </div>
          </>
        )}

        {stage.kind === "making" && (
          <HighlightsProgressPanel highlights={stage.highlights} />
        )}

        {stage.kind === "failed" && (
          <div aria-live="polite">
            <ErrorState
              title="Couldn't make the highlights video"
              message={stage.message}
              primaryAction={
                <Button ref={retryRef} size="sm" onClick={make}>
                  Try again
                </Button>
              }
              secondaryAction={
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => setStage({ kind: "picking" })}
                >
                  Adjust the moments
                </Button>
              }
            />
          </div>
        )}

        {stage.kind === "done" && (
          <div
            className="flex flex-col items-start gap-4 rounded-card border border-success-border bg-success-soft p-5"
            aria-live="polite"
          >
            <div className="flex items-center gap-3">
              <span
                aria-hidden="true"
                className="flex size-9 items-center justify-center rounded-pill bg-success text-surface-canvas"
              >
                <svg
                  viewBox="0 0 20 20"
                  fill="none"
                  className="size-4"
                  stroke="currentColor"
                  strokeWidth="2.4"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                >
                  <path d="M4.5 10.5 8 14l7.5-8" />
                </svg>
              </span>
              <p className="text-sm font-semibold text-success-soft-ink">
                The highlights video is ready ({formatTime(stage.duration_s)}
                {captionsAllowed && captions ? " · captioned" : ""}).
              </p>
            </div>
            <div className="flex flex-wrap gap-2">
              <a
                href={stage.download}
                download={stage.filename}
                ref={downloadRef}
                className={buttonClasses({ size: "lg" })}
              >
                Download
              </a>
              <ShareMenu
                fileUrl={stage.download}
                filename={stage.filename}
                title="Stream highlights"
                channel={channelName}
                videoId={videoId}
                subject="the highlights video"
                size="lg"
              />
              <Button variant="ghost" size="lg" onClick={onClose}>
                Back to the results
              </Button>
            </div>
          </div>
        )}
      </div>

      {stage.kind === "picking" && (
        <div className="flex flex-col gap-3 border-t border-border-subtle bg-surface-panel px-5 py-4 sm:flex-row sm:items-center sm:justify-between sm:px-6">
          <p
            className={`text-sm ${
              invalidSegment || overBudget
                ? "font-medium text-signal-soft-ink"
                : "text-ink-secondary"
            }`}
            aria-live="polite"
          >
            {segments.length === 0
              ? "Tick at least one moment."
              : invalidSegment
                ? "Check that each ticked moment stays within its allowed range and max length."
                : `${segments.length} ${
                    segments.length === 1 ? "moment" : "moments"
                  } · ${formatTime(totalS)} together`}
            {overBudget && " · over 15 minutes, untick some"}
          </p>
          <Button
            size="lg"
            onClick={make}
            disabled={!canMake}
            className="w-full sm:w-auto"
          >
            Make highlights video
          </Button>
        </div>
      )}
    </Dialog>
  );
}

const ORDINALS = ["first", "second", "third", "fourth", "fifth"];

function ordinal(position: number): string {
  return ORDINALS[position - 1] ?? `#${position}`;
}

/**
 * The ticked moments in the order the video will play them: stream order sorts by start
 * time, best first by rank. Durations are clamped at zero so a half-typed trim never draws
 * a negative block.
 */
export function sequenceFor(
  candidates: Candidate[],
  trims: Record<string, { checked: boolean; start_s: number; end_s: number }>,
  order: "chronological" | "rank",
): { candidate_id: string; rank: number; duration_s: number }[] {
  const ticked = candidates.filter((c) => trims[c.candidate_id]?.checked);
  const sorted = [...ticked].sort((a, b) =>
    order === "rank"
      ? a.rank - b.rank
      : trims[a.candidate_id].start_s - trims[b.candidate_id].start_s,
  );
  return sorted.map((candidate) => {
    const trim = trims[candidate.candidate_id];
    const duration = trim.end_s - trim.start_s;
    return {
      candidate_id: candidate.candidate_id,
      rank: candidate.rank,
      duration_s: Number.isFinite(duration) ? Math.max(0, duration) : 0,
    };
  });
}

/** The finished video as a strip: one block per moment, as wide as it is long. */
function SequencePreview({
  sequence,
  totalS,
}: {
  sequence: { candidate_id: string; rank: number; duration_s: number }[];
  totalS: number;
}) {
  if (sequence.length === 0) {
    return (
      <div className="flex h-14 items-center justify-center rounded-card border border-dashed border-border-strong text-xs text-ink-tertiary">
        No moments ticked
      </div>
    );
  }
  const fill = Math.min(1, totalS / MAX_TOTAL_S);
  return (
    <div className="flex flex-col gap-1.5">
      <ol
        aria-label="Play order"
        className="flex h-14 gap-[3px] overflow-hidden rounded-card bg-surface-media p-[3px]"
      >
        {sequence.map((item, index) => (
          <li
            key={item.candidate_id}
            aria-label={`${ordinal(index + 1)}: moment #${item.rank}, ${formatTime(item.duration_s)}`}
            style={{ flexGrow: Math.max(item.duration_s, 1) }}
            className={`flex min-w-7 basis-0 flex-col justify-between overflow-hidden rounded-[0.4375rem] px-1.5 py-1 ${
              index % 2 === 0
                ? "bg-accent text-accent-ink"
                : "bg-accent/80 text-accent-ink"
            }`}
          >
            <span className="font-mono text-[0.6875rem] leading-4 font-bold">
              #{item.rank}
            </span>
            <span className="truncate font-mono text-[0.625rem] leading-3 tabular-nums max-sm:hidden">
              {formatTime(item.duration_s)}
            </span>
          </li>
        ))}
      </ol>
      <div
        aria-hidden="true"
        className="h-1 overflow-hidden rounded-pill bg-surface-subtle"
      >
        <div
          className={`h-full rounded-pill ${fill >= 1 ? "bg-signal" : "bg-ink-tertiary/60"}`}
          style={{ width: `${Math.round(fill * 100)}%` }}
        />
      </div>
    </div>
  );
}

function HighlightsProgressPanel({
  highlights,
}: {
  highlights: HighlightsVideo | null;
}) {
  if (highlights === null) {
    return (
      <div
        className="flex items-center gap-3 rounded-card border border-border-subtle bg-surface-elevated p-4 text-sm"
        aria-live="polite"
      >
        <Spinner />
        <p className="font-medium text-ink-primary">
          Starting highlights render…
        </p>
      </div>
    );
  }

  const isQueued = highlights.state === "queued";
  const progress = highlights.progress;
  const current = progress?.stage_index ?? 0;
  const fraction = progress?.stage_fraction ?? null;

  return (
    <div
      aria-live="polite"
      className="flex flex-col gap-4 rounded-card border border-border-subtle bg-surface-elevated p-4 sm:p-5"
    >
      <div className="flex items-start gap-3">
        {isQueued ? (
          <span
            aria-hidden="true"
            className="mt-1.5 size-2.5 shrink-0 rounded-full bg-signal motion-safe:animate-pulse"
          />
        ) : (
          <Spinner className="mt-0.5" />
        )}
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <Badge tone={isQueued ? "neutral" : "accent"}>
              {isQueued ? "Queued" : "Rendering"}
            </Badge>
            <p className="text-sm font-medium text-ink-primary">
              {isQueued
                ? "Waiting in render queue…"
                : progress
                  ? `Step ${progress.stage_index} of ${progress.stage_count}: ${progress.message}`
                  : "Rendering the highlights video…"}
            </p>
          </div>
          {isQueued && (
            <p className="mt-1 text-xs text-ink-secondary">
              Waiting for the render worker to start this job.
            </p>
          )}
        </div>
      </div>

      {!isQueued && fraction !== null && progress && (
        <ProgressBar value={fraction * 100} label={progress.message} />
      )}

      <ol role="list" className="flex flex-col gap-2 text-xs">
        {HIGHLIGHTS_STAGES.map((stage, index) => {
          const stepNumber = index + 1;
          const skipped =
            stage.stage === "transcribing" && !highlights.captions;
          const done = !isQueued && stepNumber < current;
          const active = !isQueued && stepNumber === current;
          return (
            <li
              key={stage.stage}
              className={`flex items-center gap-2.5 ${
                skipped
                  ? "text-ink-tertiary"
                  : done
                    ? "text-success-soft-ink"
                    : active
                      ? "font-semibold text-ink-primary"
                      : "text-ink-tertiary"
              }`}
            >
              <span
                aria-hidden="true"
                className={`flex size-4 shrink-0 items-center justify-center rounded-pill font-mono text-[0.625rem] ${
                  done
                    ? "bg-success text-surface-canvas"
                    : active
                      ? "border border-accent text-accent"
                      : "border border-border-strong"
                }`}
              >
                {skipped ? "–" : done ? "✓" : active ? "•" : ""}
              </span>
              <span>
                {stage.message}
                {skipped ? " (off)" : ""}
              </span>
            </li>
          );
        })}
      </ol>
    </div>
  );
}
