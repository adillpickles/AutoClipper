"use client";

// The "Make highlights video" dialog (PRD § 4, #72): the top moments, all ticked with their
// current trims, an order toggle and a captions toggle; then the render's progress and a
// download button. The server resolves and validates what is sent (§ 7.1 of the contract).

import { useEffect, useRef, useState } from "react";

import {
  ApiRequestError,
  getApiClient,
  POLL_INTERVAL_MS,
} from "@/lib/api/client";
import { HIGHLIGHTS_STAGES } from "@/lib/api/stages";
import type { Candidate, HighlightsVideo } from "@/lib/api/types";
import { formatTime } from "@/lib/format";
import { rememberedTrims, type SavedTrim } from "@/lib/job-state";

/** The whole video's bound (§ 7.1). */
const MAX_TOTAL_S = 900;

interface Trim {
  checked: boolean;
  start_s: number;
  end_s: number;
}

type Stage =
  | { kind: "picking" }
  | { kind: "making"; highlights: HighlightsVideo | null }
  | { kind: "done"; download: string; duration_s: number }
  | { kind: "failed"; message: string };

function initialTrimFor(
  candidate: Candidate,
  saved: SavedTrim | undefined,
  vodDurationS: number,
): Trim {
  if (
    saved &&
    Number.isFinite(saved.start_s) &&
    Number.isFinite(saved.end_s) &&
    saved.start_s >= 0 &&
    saved.start_s < saved.end_s &&
    saved.end_s <= vodDurationS
  ) {
    return { checked: true, start_s: saved.start_s, end_s: saved.end_s };
  }
  return { checked: true, start_s: candidate.start_s, end_s: candidate.end_s };
}

export function HighlightsDialog({
  jobId,
  videoId,
  vodDurationS,
  candidates,
  initialTrims,
  captionsStatus = "available",
  onClose,
}: {
  jobId: string;
  videoId: string;
  vodDurationS: number;
  candidates: Candidate[];
  initialTrims?: Record<string, SavedTrim>;
  captionsStatus?: "checking" | "available" | "unavailable" | "unknown";
  onClose: () => void;
}) {
  const captionsAllowed =
    captionsStatus === "available" || captionsStatus === "unknown";
  const [trims, setTrims] = useState<Record<string, Trim>>(() => {
    const saved = initialTrims ?? rememberedTrims(jobId);
    return Object.fromEntries(
      candidates.map((candidate) => [
        candidate.candidate_id,
        initialTrimFor(candidate, saved[candidate.candidate_id], vodDurationS),
      ]),
    );
  });
  const [order, setOrder] = useState<"chronological" | "rank">("chronological");
  const [captions, setCaptions] = useState(false);
  const [stage, setStage] = useState<Stage>({ kind: "picking" });
  const running = useRef(false);

  // The callback that closes: kept in a ref so rerenders of the parent (every clip that
  // finishes re-renders the results screen) never restart this dialog's effects.
  const closeRef = useRef(onClose);
  useEffect(() => {
    closeRef.current = onClose;
  }, [onClose]);

  // Escape closes; unmounting cancels any in-flight render's polling. The cleanup runs only
  // when the dialog truly unmounts (not on parent rerenders), or polling would stop early.
  useEffect(() => {
    const keydown = (event: KeyboardEvent) => {
      if (event.key === "Escape") closeRef.current();
    };
    window.addEventListener("keydown", keydown);
    return () => {
      running.current = false;
      window.removeEventListener("keydown", keydown);
    };
  }, []);

  const segments = candidates
    .filter((candidate) => trims[candidate.candidate_id]?.checked)
    .map((candidate) => ({
      candidate_id: candidate.candidate_id,
      start_s: trims[candidate.candidate_id].start_s,
      end_s: trims[candidate.candidate_id].end_s,
    }));
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

  const inputClass =
    "w-20 rounded-md border-0 bg-zinc-100 px-2 py-1 text-sm tabular-nums ring-1 ring-inset ring-zinc-300 focus-visible:ring-2 focus-visible:ring-amber-500 focus-visible:outline-none dark:bg-zinc-800 dark:ring-zinc-700 disabled:opacity-50";

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-zinc-900/60 p-4"
      role="dialog"
      aria-modal="true"
      aria-labelledby="highlights-title"
    >
      <div className="flex max-h-full w-full max-w-lg flex-col gap-4 overflow-y-auto rounded-2xl bg-white p-6 shadow-xl dark:bg-zinc-900 dark:ring-1 dark:ring-zinc-700">
        <div className="flex items-start justify-between gap-4">
          <div>
            <h2 id="highlights-title" className="text-lg font-semibold">
              Make highlights video
            </h2>
            <p className="text-sm text-zinc-600 dark:text-zinc-400">
              {stage.kind === "done"
                ? `Done. ${formatTime(stage.duration_s)} of highlights.`
                : stage.kind === "making" &&
                    stage.highlights?.state === "queued"
                  ? "Queued behind active renders. It will start automatically."
                  : stage.kind === "making" &&
                      stage.highlights?.state === "running"
                    ? (stage.highlights.progress?.message ??
                      "Rendering your highlights video.")
                    : "One 16:9 video from the ticked moments, joined in the chosen order."}
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close"
            className="rounded-lg px-2 py-1 text-sm font-medium text-zinc-500 hover:bg-zinc-100 hover:text-zinc-900 focus-visible:ring-2 focus-visible:ring-amber-500 focus-visible:outline-none dark:hover:bg-zinc-800 dark:hover:text-zinc-100"
          >
            Close
          </button>
        </div>

        {stage.kind === "picking" && (
          <>
            <fieldset className="flex flex-col gap-2">
              <legend className="text-sm font-medium">
                Moments (tick, trim and order)
              </legend>
              <ul role="list" className="flex flex-col gap-2">
                {candidates.map((candidate) => {
                  const trim = trims[candidate.candidate_id];
                  const isTrimmed =
                    Math.abs(trim.start_s - candidate.start_s) > 1e-6 ||
                    Math.abs(trim.end_s - candidate.end_s) > 1e-6;
                  return (
                    <li
                      key={candidate.candidate_id}
                      className="flex flex-wrap items-center gap-x-3 gap-y-1.5 rounded-lg bg-zinc-50 px-3 py-2 text-sm dark:bg-zinc-800/60"
                    >
                      <input
                        type="checkbox"
                        checked={trim.checked}
                        onChange={(event) =>
                          setTrims((current) => ({
                            ...current,
                            [candidate.candidate_id]: {
                              ...trim,
                              checked: event.target.checked,
                            },
                          }))
                        }
                        aria-label={`Include moment at ${formatTime(trim.start_s)}`}
                      />
                      <span className="w-8 shrink-0 text-zinc-500 tabular-nums dark:text-zinc-400">
                        #{candidate.rank}
                      </span>
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
                        min={0}
                        max={vodDurationS}
                        value={trim.start_s}
                        disabled={!trim.checked}
                        onChange={(event) =>
                          setTrims((current) => ({
                            ...current,
                            [candidate.candidate_id]: {
                              ...trim,
                              start_s: Number(event.target.value),
                            },
                          }))
                        }
                        className={inputClass}
                      />
                      <span aria-hidden="true" className="text-zinc-400">
                        to
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
                        min={0}
                        max={vodDurationS}
                        value={trim.end_s}
                        disabled={!trim.checked}
                        onChange={(event) =>
                          setTrims((current) => ({
                            ...current,
                            [candidate.candidate_id]: {
                              ...trim,
                              end_s: Number(event.target.value),
                            },
                          }))
                        }
                        className={inputClass}
                      />
                      <span className="text-zinc-500 tabular-nums dark:text-zinc-400">
                        {formatTime(trim.end_s - trim.start_s)} · ends{" "}
                        {formatTime(trim.end_s)}
                      </span>
                      {isTrimmed && (
                        <span className="rounded-full bg-amber-100 px-2 py-0.5 text-xs font-medium text-amber-900 dark:bg-amber-400/15 dark:text-amber-200">
                          Trimmed
                        </span>
                      )}
                    </li>
                  );
                })}
              </ul>
            </fieldset>

            <fieldset className="flex flex-wrap items-center gap-4 text-sm">
              <legend className="sr-only">Order and captions</legend>
              <div className="flex items-center gap-2">
                <input
                  type="radio"
                  id="order-chronological"
                  name="highlights-order"
                  checked={order === "chronological"}
                  onChange={() => setOrder("chronological")}
                />
                <label htmlFor="order-chronological">Stream order</label>
                <input
                  type="radio"
                  id="order-rank"
                  name="highlights-order"
                  checked={order === "rank"}
                  onChange={() => setOrder("rank")}
                />
                <label htmlFor="order-rank">Best first</label>
              </div>
              <div className="flex items-center gap-2">
                <input
                  type="checkbox"
                  id="highlights-captions"
                  checked={captionsAllowed ? captions : false}
                  disabled={!captionsAllowed}
                  onChange={(event) => setCaptions(event.target.checked)}
                />
                <label htmlFor="highlights-captions">
                  Burn in captions
                  {captionsStatus === "checking" && (
                    <span className="ml-1 text-xs text-ink-tertiary">
                      (checking…)
                    </span>
                  )}
                  {captionsStatus === "unavailable" && (
                    <span className="ml-1 text-xs text-ink-tertiary">
                      (needs uv sync --extra captions and FFmpeg libass)
                    </span>
                  )}
                </label>
              </div>
            </fieldset>

            <div className="flex items-center justify-between gap-3">
              <p
                className="text-sm text-zinc-600 dark:text-zinc-400"
                aria-live="polite"
              >
                {segments.length === 0
                  ? "Tick at least one moment."
                  : `${segments.length} ${
                      segments.length === 1 ? "moment" : "moments"
                    } · ${formatTime(totalS)} together`}
                {totalS > MAX_TOTAL_S && " · over 15 minutes, untick some"}
              </p>
              <button
                type="button"
                onClick={make}
                disabled={segments.length === 0 || totalS > MAX_TOTAL_S}
                className="rounded-lg bg-zinc-900 px-4 py-2 text-sm font-semibold text-white hover:bg-zinc-700 focus-visible:ring-2 focus-visible:ring-amber-500 focus-visible:outline-none disabled:opacity-50 dark:bg-zinc-100 dark:text-zinc-900 dark:hover:bg-zinc-300"
              >
                Make highlights video
              </button>
            </div>
          </>
        )}

        {stage.kind === "making" && (
          <HighlightsProgressPanel highlights={stage.highlights} />
        )}

        {stage.kind === "failed" && (
          <div className="flex flex-col gap-3" aria-live="polite">
            <p className="text-red-700 dark:text-red-300">{stage.message}</p>
            <div className="flex gap-3">
              <button
                type="button"
                onClick={() => setStage({ kind: "picking" })}
                className="rounded-lg bg-white px-3 py-1.5 text-sm font-medium text-zinc-900 ring-1 ring-zinc-300 hover:bg-zinc-50 focus-visible:ring-2 focus-visible:ring-amber-500 focus-visible:outline-none dark:bg-zinc-900 dark:text-zinc-100 dark:ring-zinc-700"
              >
                Adjust the moments
              </button>
              <button
                type="button"
                onClick={make}
                className="rounded-lg bg-zinc-900 px-3 py-1.5 text-sm font-medium text-white hover:bg-zinc-700 focus-visible:ring-2 focus-visible:ring-amber-500 focus-visible:outline-none dark:bg-zinc-100 dark:text-zinc-900 dark:hover:bg-zinc-300"
              >
                Try again
              </button>
            </div>
          </div>
        )}

        {stage.kind === "done" && (
          <div className="flex flex-col gap-3" aria-live="polite">
            <p>
              The highlights video is ready ({formatTime(stage.duration_s)}
              {captionsAllowed && captions ? " · captioned" : ""}).
            </p>
            <div className="flex gap-3">
              <a
                href={stage.download}
                download={`autoclipper_${videoId}_highlights.mp4`}
                className="rounded-lg bg-zinc-900 px-4 py-2 text-sm font-semibold text-white hover:bg-zinc-700 focus-visible:ring-2 focus-visible:ring-amber-500 focus-visible:outline-none dark:bg-zinc-100 dark:text-zinc-900 dark:hover:bg-zinc-300"
              >
                Download
              </a>
              <button
                type="button"
                onClick={onClose}
                className="rounded-lg px-3 py-2 text-sm font-medium text-zinc-500 hover:text-zinc-900 focus-visible:ring-2 focus-visible:ring-amber-500 focus-visible:outline-none dark:hover:text-zinc-100"
              >
                Back to the results
              </button>
            </div>
          </div>
        )}
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
        className="flex items-center gap-3 rounded-xl border border-zinc-200 bg-zinc-50 p-4 text-sm dark:border-zinc-800 dark:bg-zinc-950"
        aria-live="polite"
      >
        <span
          className="size-5 shrink-0 animate-spin rounded-full border-2 border-amber-500 border-t-transparent"
          aria-hidden="true"
        />
        <p className="font-medium">Starting highlights render…</p>
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
      className="flex flex-col gap-3 rounded-xl border border-zinc-200 bg-zinc-50 p-4 dark:border-zinc-800 dark:bg-zinc-950"
    >
      <div className="flex items-center gap-3">
        {isQueued ? (
          <span
            aria-hidden="true"
            className="size-3 shrink-0 rounded-full bg-amber-500 motion-safe:animate-pulse"
          />
        ) : (
          <span
            aria-hidden="true"
            className="size-5 shrink-0 animate-spin rounded-full border-2 border-amber-500 border-t-transparent"
          />
        )}
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <span
              className={`rounded-full px-2 py-0.5 text-xs font-semibold ${
                isQueued
                  ? "bg-zinc-200 text-zinc-700 dark:bg-zinc-800 dark:text-zinc-300"
                  : "bg-amber-100 text-amber-900 dark:bg-amber-400/15 dark:text-amber-200"
              }`}
            >
              {isQueued ? "Queued" : "Rendering"}
            </span>
            <p className="text-sm font-medium">
              {isQueued
                ? "Waiting in render queue…"
                : progress
                  ? `Step ${progress.stage_index} of ${progress.stage_count}: ${progress.message}`
                  : "Rendering the highlights video…"}
            </p>
          </div>
          {isQueued && (
            <p className="mt-1 text-xs text-zinc-500 dark:text-zinc-400">
              Another clip or highlights render is ahead in the queue.
            </p>
          )}
        </div>
      </div>

      {!isQueued && fraction !== null && progress && (
        <div
          role="progressbar"
          aria-label={progress.message}
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={Math.round(fraction * 100)}
          className="h-2 overflow-hidden rounded-full bg-zinc-200 dark:bg-zinc-800"
        >
          <div
            className="h-full rounded-full bg-amber-500 transition-[width] duration-300"
            style={{ width: `${Math.round(fraction * 100)}%` }}
          />
        </div>
      )}

      <ol role="list" className="flex flex-col gap-1.5 text-xs">
        {HIGHLIGHTS_STAGES.map((stage, index) => {
          const stepNumber = index + 1;
          const skipped =
            stage.stage === "transcribing" && !highlights.captions;
          const done = !isQueued && stepNumber < current;
          const active = !isQueued && stepNumber === current;
          return (
            <li
              key={stage.stage}
              className={
                skipped
                  ? "text-zinc-400 line-through dark:text-zinc-600"
                  : done
                    ? "text-emerald-700 dark:text-emerald-300"
                    : active
                      ? "font-semibold text-zinc-900 dark:text-zinc-100"
                      : "text-zinc-500 dark:text-zinc-400"
              }
            >
              {skipped ? "–" : done ? "✓" : active ? "•" : "○"} {stage.message}
              {skipped ? " (off)" : ""}
            </li>
          );
        })}
      </ol>
    </div>
  );
}
