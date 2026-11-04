"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useRef, useState } from "react";

import { ClipCard, type ClipExport } from "@/components/clip-card";
import {
  ApiRequestError,
  getApiClient,
  POLL_INTERVAL_MS,
} from "@/lib/api/client";
import type {
  AnalysisResult,
  Candidate,
  Export,
  Layout,
} from "@/lib/api/types";
import { formatTime } from "@/lib/format";

/** The top 10 are rendered as vertical clips automatically (PRD § 4). */
export const TOP_CLIPS = 10;

// Back off on network errors (2, 4, 8 s, capped at 10 s) and give up after about a minute (§ 9.1).
const FIRST_BACKOFF_MS = 2000;
const MAX_BACKOFF_MS = 10_000;
const GIVE_UP_AFTER_MS = 60_000;
// Spacing between the clicks of "Download all", so the browser starts every download.
const DOWNLOAD_SPACING_MS = 400;

const exportsKey = (jobId: string) => `autoclipper.exports.${jobId}`;

function rememberedExport(jobId: string, candidateId: string): string | null {
  try {
    const saved = JSON.parse(
      window.localStorage.getItem(exportsKey(jobId)) ?? "{}",
    ) as Record<string, string>;
    return saved[candidateId] ?? null;
  } catch {
    return null;
  }
}

function rememberExport(
  jobId: string,
  candidateId: string,
  exportId: string,
): void {
  try {
    const saved = JSON.parse(
      window.localStorage.getItem(exportsKey(jobId)) ?? "{}",
    ) as Record<string, string>;
    window.localStorage.setItem(
      exportsKey(jobId),
      JSON.stringify({ ...saved, [candidateId]: exportId }),
    );
  } catch {
    // Without storage the clips are rendered again on the next visit; nothing breaks.
  }
}

type View =
  | { kind: "loading" }
  | { kind: "result"; result: AnalysisResult }
  | { kind: "problem"; title: string; message: string };

export function ResultsScreen() {
  const { jobId } = useParams<{ jobId: string }>();
  const [view, setView] = useState<View>({ kind: "loading" });
  const [clips, setClips] = useState<Record<string, ClipExport>>({});
  const downloads = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let cancelled = false;
    getApiClient()
      .getResult(jobId)
      .then((result) => {
        if (!cancelled) setView({ kind: "result", result });
      })
      .catch((error: unknown) => {
        if (cancelled) return;
        if (
          error instanceof ApiRequestError &&
          error.error.code === "job_not_done"
        ) {
          setView({
            kind: "problem",
            title: "Still working",
            message:
              "This analysis hasn't finished yet. Its progress page shows where it is.",
          });
        } else if (error instanceof ApiRequestError) {
          setView({
            kind: "problem",
            title: "Can't show these results",
            message: error.error.message,
          });
        } else {
          setView({
            kind: "problem",
            title: "Can't reach the server",
            message: "Check that AutoClipper is running, then reload the page.",
          });
        }
      });
    return () => {
      cancelled = true;
    };
  }, [jobId]);

  const result = view.kind === "result" ? view.result : null;

  // Request the top 10 exports once the result is in, then poll each until it finishes.
  // Export IDs are remembered per job, so coming back to the page shows the same clips instead of
  // rendering them all again. `renderRef` lets a card's Retry button run the same flow.
  const renderRef = useRef<
    ((candidate: Candidate, fresh: boolean) => Promise<void>) | null
  >(null);
  useEffect(() => {
    if (!result) return;
    let cancelled = false;
    const timers: ReturnType<typeof setTimeout>[] = [];
    const client = getApiClient();
    const set = (candidateId: string, clip: ClipExport) => {
      if (!cancelled)
        setClips((current) => ({ ...current, [candidateId]: clip }));
    };
    const wait = (ms: number) =>
      new Promise<void>((resolve) => timers.push(setTimeout(resolve, ms)));

    async function start(
      candidate: Candidate,
      fresh: boolean,
    ): Promise<Export | null> {
      const known = fresh
        ? null
        : rememberedExport(jobId, candidate.candidate_id);
      if (known) {
        try {
          return await client.getExport(known);
        } catch (error) {
          if (!(error instanceof ApiRequestError && error.status === 404))
            throw error;
          // The server forgot it (e.g. a restart): render it again.
        }
      }
      const media = await client.getCandidateMedia(
        jobId,
        candidate.candidate_id,
      );
      if (cancelled) return null;
      const availability = media.export;
      if (availability.status !== "available") {
        set(candidate.candidate_id, {
          kind: "unavailable",
          message: availability.message ?? "This clip can't be exported.",
        });
        return null;
      }
      const layout = availability.layouts.includes("center_crop")
        ? "center_crop"
        : availability.layouts.find(
            (l): l is Layout => l === "configured_region",
          );
      if (!layout) {
        set(candidate.candidate_id, {
          kind: "unavailable",
          message:
            "The server can't render a vertical layout for this clip right now.",
        });
        return null;
      }
      if (
        availability.max_duration_s !== null &&
        candidate.end_s - candidate.start_s > availability.max_duration_s
      ) {
        set(candidate.candidate_id, {
          kind: "unavailable",
          message: `This moment is longer than ${availability.max_duration_s} seconds. Shorten it in the editor to export it.`,
        });
        return null;
      }
      const { export: created } = await client.createExport({
        job_id: jobId,
        candidate_id: candidate.candidate_id,
        start_s: candidate.start_s,
        end_s: candidate.end_s,
        layout,
        captions: true,
      });
      rememberExport(jobId, candidate.candidate_id, created.export_id);
      return created;
    }

    async function render(candidate: Candidate, fresh: boolean) {
      let failingSince: number | null = null;
      let backoff = FIRST_BACKOFF_MS;
      let current: Export | null = null;
      while (!cancelled) {
        try {
          current =
            current === null
              ? await start(candidate, fresh)
              : await client.getExport(current.export_id);
          if (current === null) return;
          failingSince = null;
          backoff = FIRST_BACKOFF_MS;
          const url = current.download_url
            ? client.resolveUrl(current.download_url)
            : null;
          set(candidate.candidate_id, { kind: "export", export: current, url });
          if (current.state === "done" || current.state === "failed") return;
          await wait(POLL_INTERVAL_MS);
        } catch (error) {
          if (cancelled) return;
          if (error instanceof ApiRequestError) {
            set(candidate.candidate_id, {
              kind: "error",
              message: error.error.message,
            });
            return;
          }
          // Network trouble: back off (§ 9.1), and offer a retry after about a minute.
          failingSince ??= Date.now();
          if (Date.now() - failingSince > GIVE_UP_AFTER_MS) {
            set(candidate.candidate_id, {
              kind: "error",
              message: "Can't reach the server to render this clip.",
            });
            return;
          }
          await wait(backoff);
          backoff = Math.min(backoff * 2, MAX_BACKOFF_MS);
        }
      }
    }

    renderRef.current = render;
    for (const candidate of result.candidates.slice(0, TOP_CLIPS))
      void render(candidate, false);
    return () => {
      cancelled = true;
      renderRef.current = null;
      timers.forEach(clearTimeout);
    };
  }, [result, jobId]);

  const retry = (candidate: Candidate) => {
    setClips((current) => ({
      ...current,
      [candidate.candidate_id]: { kind: "pending" },
    }));
    void renderRef.current?.(candidate, true);
  };

  if (view.kind === "loading") {
    return (
      <h1 className="text-2xl font-semibold tracking-tight">
        Loading the results…
      </h1>
    );
  }
  if (view.kind === "problem") {
    return (
      <div className="flex flex-col gap-4">
        <h1 className="text-2xl font-semibold tracking-tight">{view.title}</h1>
        <p className="text-zinc-700 dark:text-zinc-300">{view.message}</p>
        <Link
          href={`/jobs/${jobId}`}
          className="self-start font-medium text-amber-800 underline dark:text-amber-300"
        >
          Back to the analysis
        </Link>
      </div>
    );
  }

  const top = view.result.candidates.slice(0, TOP_CLIPS);
  const scores = top.map((c) => c.score);
  const low = Math.min(...scores);
  const high = Math.max(...scores);
  // Score bars are scaled within this result (§ 4.3), never shown as a percentage chance.
  const scale = (score: number) =>
    high > low ? 0.15 + (0.85 * (score - low)) / (high - low) : 1;
  const finished = top.filter((c) => {
    const clip = clips[c.candidate_id];
    if (!clip || clip.kind === "pending") return false;
    if (clip.kind !== "export") return true;
    return clip.export.state !== "queued" && clip.export.state !== "running";
  }).length;
  const ready = top.filter((c) => {
    const clip = clips[c.candidate_id];
    return clip?.kind === "export" && clip.export.state === "done" && clip.url;
  });
  const { vod } = view.result;

  function downloadAll() {
    // One click per finished clip, spaced out: each link has its own `download` file name, and
    // clicks in quick succession can cancel each other.
    const links =
      downloads.current?.querySelectorAll<HTMLAnchorElement>("a[download]") ??
      [];
    links.forEach((link, index) =>
      setTimeout(() => link.click(), index * DOWNLOAD_SPACING_MS),
    );
  }

  return (
    <div className="flex flex-col gap-8">
      <div className="flex flex-col gap-3">
        <p className="text-sm font-medium text-zinc-600 dark:text-zinc-400">
          {vod.broadcaster_display_name} · {formatTime(vod.duration_s)} · VOD{" "}
          {vod.video_id}
        </p>
        <h1 className="text-2xl font-semibold tracking-tight text-balance sm:text-3xl">
          {vod.title}
        </h1>
        {view.result.warnings.map((warning) => (
          <p
            key={warning.code}
            className="rounded-lg bg-amber-50 px-4 py-3 text-sm text-amber-900 dark:bg-amber-400/10 dark:text-amber-100"
          >
            {warning.message}
          </p>
        ))}
      </div>

      {top.length === 0 ? (
        <div className="flex flex-col gap-3 rounded-2xl border border-dashed border-zinc-300 p-8 text-center dark:border-zinc-700">
          <h2 className="text-lg font-semibold">No moments stood out</h2>
          <p className="text-zinc-600 dark:text-zinc-400">
            {view.result.warnings.length > 0
              ? "Nothing could be ranked from the signals that were available for this VOD."
              : view.result.n_viewer_clips_total === 0
                ? "Chat and audio stayed steady through this VOD and nobody clipped it, so there's nothing to rank."
                : "Chat and audio stayed steady through this VOD, so there's nothing to rank."}
          </p>
          <Link
            href="/"
            className="font-medium text-amber-800 underline dark:text-amber-300"
          >
            Try another VOD
          </Link>
        </div>
      ) : (
        <section
          aria-labelledby="clips-heading"
          className="flex flex-col gap-4"
        >
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div>
              <h2 id="clips-heading" className="text-lg font-semibold">
                Top {top.length} {top.length === 1 ? "moment" : "moments"}
              </h2>
              <p
                className="text-sm text-zinc-600 dark:text-zinc-400"
                aria-live="polite"
              >
                {finished < top.length
                  ? `Rendering clips: ${finished} of ${top.length} done`
                  : `${ready.length} of ${top.length} clips ready`}
                {view.result.n_viewer_clips_total === 0 &&
                  " · Nobody clipped this VOD, so every moment was discovered."}
              </p>
            </div>
            <button
              type="button"
              onClick={downloadAll}
              disabled={ready.length === 0}
              className="rounded-lg bg-zinc-900 px-4 py-2 text-sm font-semibold text-white hover:bg-zinc-700 focus-visible:ring-2 focus-visible:ring-amber-500 focus-visible:outline-none disabled:opacity-50 dark:bg-zinc-100 dark:text-zinc-900 dark:hover:bg-zinc-300"
            >
              Download all{ready.length > 0 ? ` (${ready.length})` : ""}
            </button>
          </div>
          <div ref={downloads}>
            <ol
              role="list"
              className="grid grid-cols-1 gap-5 min-[480px]:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4"
            >
              {top.map((candidate) => (
                <ClipCard
                  key={candidate.candidate_id}
                  candidate={candidate}
                  scoreFraction={scale(candidate.score)}
                  clip={clips[candidate.candidate_id] ?? { kind: "pending" }}
                  onRetry={() => retry(candidate)}
                  videoId={vod.video_id}
                />
              ))}
            </ol>
          </div>
        </section>
      )}
    </div>
  );
}
