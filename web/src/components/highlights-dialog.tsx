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
import type { Candidate } from "@/lib/api/types";
import { formatTime } from "@/lib/format";

/** The whole video's bound (§ 7.1). */
const MAX_TOTAL_S = 900;

interface Trim {
  checked: boolean;
  start_s: number;
  end_s: number;
}

type Stage =
  | { kind: "picking" }
  | { kind: "making" }
  | { kind: "done"; download: string; duration_s: number }
  | { kind: "failed"; message: string };

export function HighlightsDialog({
  jobId,
  videoId,
  vodDurationS,
  candidates,
  onClose,
}: {
  jobId: string;
  videoId: string;
  vodDurationS: number;
  candidates: Candidate[];
  onClose: () => void;
}) {
  const [trims, setTrims] = useState<Record<string, Trim>>(() =>
    Object.fromEntries(
      candidates.map((candidate) => [
        candidate.candidate_id,
        { checked: true, start_s: candidate.start_s, end_s: candidate.end_s },
      ]),
    ),
  );
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
    setStage({ kind: "making" });
    const client = getApiClient();
    try {
      let current = (
        await client.createHighlights({
          job_id: jobId,
          segments,
          order,
          captions,
        })
      ).highlights;
      while (
        running.current &&
        current.state !== "done" &&
        current.state !== "failed"
      ) {
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
                  return (
                    <li
                      key={candidate.candidate_id}
                      className="flex items-center gap-3 rounded-lg bg-zinc-50 px-3 py-2 text-sm dark:bg-zinc-800/60"
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
                        aria-label={`Include moment at ${formatTime(candidate.start_s)}`}
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
                        step="0.5"
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
                        step="0.5"
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
                      <span className="text-zinc-500 dark:text-zinc-400">
                        {formatTime(trim.end_s - trim.start_s)} · ends{" "}
                        {formatTime(trim.end_s)}
                      </span>
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
                  checked={captions}
                  onChange={(event) => setCaptions(event.target.checked)}
                />
                <label htmlFor="highlights-captions">Burn in captions</label>
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
          <div className="flex items-center gap-3" aria-live="polite">
            <span
              className="size-6 animate-spin rounded-full border-2 border-amber-500 border-t-transparent"
              aria-hidden="true"
            />
            <p>Making the highlights video…</p>
          </div>
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
              {captions ? " · captioned" : ""}).
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
