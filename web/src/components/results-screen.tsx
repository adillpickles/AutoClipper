"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useRef, useState } from "react";

import { ClipCard, type ClipExport } from "@/components/clip-card";
import { HighlightsDialog } from "@/components/highlights-dialog";
import { StreamTimeline } from "@/components/stream-timeline";
import {
  ApiRequestError,
  getApiClient,
  POLL_INTERVAL_MS,
} from "@/lib/api/client";
import type {
  AnalysisResult,
  Candidate,
  Export,
  ExportAvailability,
  Layout,
} from "@/lib/api/types";
import { buildClipFilename } from "@/lib/filenames";
import { formatTime } from "@/lib/format";
import {
  isTrimReset,
  rememberExport,
  rememberedExport,
  rememberedTrim,
  rememberedTrims,
  type SavedTrim,
} from "@/lib/job-state";

/** The top 10 are rendered as vertical clips automatically (PRD § 4). */
export const TOP_CLIPS = 10;

// Back off on network errors (2, 4, 8 s, capped at 10 s) and give up after about a minute (§ 9.1).
const FIRST_BACKOFF_MS = 2000;
const MAX_BACKOFF_MS = 10_000;
const GIVE_UP_AFTER_MS = 60_000;
const HEALTH_TIMEOUT_MS = 3000;
// Spacing between the clicks of "Download all", so the browser starts every download.
const DOWNLOAD_SPACING_MS = 400;

const KNOWN_LAYOUTS: readonly Layout[] = [
  "center_crop",
  "configured_region",
  "stacked",
];

function isKnownLayout(value: string): value is Layout {
  return (KNOWN_LAYOUTS as readonly string[]).includes(value);
}

type View =
  | { kind: "loading" }
  | { kind: "result"; result: AnalysisResult }
  | { kind: "problem"; title: string; message: string };

export function ResultsScreen() {
  const { jobId } = useParams<{ jobId: string }>();
  const [view, setView] = useState<View>({ kind: "loading" });
  const [clips, setClips] = useState<Record<string, ClipExport>>({});
  const [exportBoundsByCandidate, setExportBoundsByCandidate] = useState<
    Record<string, ExportAvailability>
  >({});
  const [viewMode, setViewMode] = useState<"cards" | "timeline">("cards");
  const [selectedCandidateId, setSelectedCandidateId] = useState<string | null>(
    null,
  );
  const [highlightsOpen, setHighlightsOpen] = useState(false);
  const [captionsStatus, setCaptionsStatus] = useState<
    "checking" | "available" | "unavailable" | "unknown"
  >("checking");
  const [captionsFallback, setCaptionsFallback] = useState<
    "none" | "retrying" | "done"
  >("none");

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

    // Check `/health` once (bounded by a timeout so a hung health endpoint never blocks
    // automatic exports) so servers without captioning export cleanly without captions.
    let serverCaptionsAvailable = true;
    let healthUnknown = false;
    const healthReady = new Promise<void>((resolve) => {
      let settled = false;
      const done = () => {
        if (settled) return;
        settled = true;
        clearTimeout(timeout);
        resolve();
      };
      const timeout = setTimeout(() => {
        healthUnknown = true;
        if (!cancelled) setCaptionsStatus("unknown");
        done();
      }, HEALTH_TIMEOUT_MS);
      timers.push(timeout);
      client
        .getHealth()
        .then((health) => {
          if (settled || cancelled) return;
          if (typeof health.captions_available !== "boolean") {
            healthUnknown = true;
            setCaptionsStatus("unknown");
            done();
            return;
          }
          const available = health.captions_available;
          serverCaptionsAvailable = available;
          setCaptionsStatus(available ? "available" : "unavailable");
          done();
        })
        .catch(() => {
          if (settled || cancelled) return;
          healthUnknown = true;
          setCaptionsStatus("unknown");
          done();
        });
    });

    async function start(
      candidate: Candidate,
      fresh: boolean,
      fallbackFrom: Export | null = null,
    ): Promise<Export | null> {
      await healthReady;
      if (cancelled) return null;
      const useCaptions = fallbackFrom ? false : serverCaptionsAvailable;
      let retrySource = fallbackFrom;
      const known =
        fresh || retrySource
          ? null
          : rememberedExport(jobId, candidate.candidate_id);
      if (known) {
        try {
          const existing = await client.getExport(known);
          if (
            !useCaptions &&
            existing.state === "failed" &&
            existing.captions &&
            existing.error?.code === "captioning_failed"
          ) {
            retrySource = existing;
          } else {
            return existing;
          }
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
      setExportBoundsByCandidate((current) => ({
        ...current,
        [candidate.candidate_id]: availability,
      }));
      if (availability.status !== "available") {
        set(candidate.candidate_id, {
          kind: "unavailable",
          message: availability.message ?? "This clip can't be exported.",
        });
        return null;
      }
      const defaultLayout = availability.layouts.includes("center_crop")
        ? "center_crop"
        : availability.layouts.find(
            (l): l is Layout => l === "configured_region",
          );
      const layout =
        retrySource &&
        isKnownLayout(retrySource.layout) &&
        availability.layouts.includes(retrySource.layout)
          ? retrySource.layout
          : defaultLayout;
      if (!layout) {
        set(candidate.candidate_id, {
          kind: "unavailable",
          message:
            "The server can't render a vertical layout for this clip right now.",
        });
        return null;
      }
      const savedTrim = rememberedTrim(jobId, candidate.candidate_id);
      const usableSaved =
        savedTrim &&
        (availability.min_start_s === null ||
          savedTrim.start_s >= availability.min_start_s) &&
        (availability.max_end_s === null ||
          savedTrim.end_s <= availability.max_end_s)
          ? savedTrim
          : null;
      const startS = retrySource
        ? retrySource.start_s
        : (usableSaved?.start_s ?? candidate.start_s);
      const endS = retrySource
        ? retrySource.end_s
        : (usableSaved?.end_s ?? candidate.end_s);
      if (
        availability.max_duration_s !== null &&
        endS - startS > availability.max_duration_s
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
        start_s: startS,
        end_s: endS,
        layout,
        captions: retrySource ? false : useCaptions,
      });
      rememberExport(jobId, candidate.candidate_id, created.export_id);
      return created;
    }

    async function render(candidate: Candidate, fresh: boolean) {
      let failingSince: number | null = null;
      let backoff = FIRST_BACKOFF_MS;
      let current: Export | null = null;
      let forceFresh = fresh;
      let fallbackFrom: Export | null = null;
      let retriedWithoutCaptions = false;
      while (!cancelled) {
        try {
          current =
            current === null
              ? await start(candidate, forceFresh, fallbackFrom)
              : await client.getExport(current.export_id);
          if (current === null) return;
          failingSince = null;
          backoff = FIRST_BACKOFF_MS;
          if (
            current.state === "failed" &&
            current.captions &&
            !retriedWithoutCaptions &&
            (current.error?.code === "captioning_failed" ||
              (healthUnknown && current.error?.code === "render_failed"))
          ) {
            retriedWithoutCaptions = true;
            fallbackFrom = current;
            forceFresh = true;
            if (!cancelled) {
              setCaptionsFallback((prev) =>
                prev === "done" ? "done" : "retrying",
              );
            }
            current = null;
            continue;
          }
          if (
            retriedWithoutCaptions &&
            !current.captions &&
            current.state === "done" &&
            !cancelled
          ) {
            setCaptionsFallback("done");
          }
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
    // Trigger one download per finished clip, spaced out so the browser starts every file;
    // works from both the clip-cards grid and the timeline view.
    ready.forEach((candidate, index) => {
      const clip = clips[candidate.candidate_id];
      if (!clip || clip.kind !== "export" || !clip.url) return;
      const url = clip.url;
      const filename =
        clip.export.filename ??
        buildClipFilename({
          channel: vod.broadcaster_display_name,
          createdAt: vod.created_at,
          title: candidate.title ?? null,
          rank: candidate.rank,
          startS: clip.export.start_s,
          fallbackId: vod.video_id,
        });
      setTimeout(() => {
        const link = document.createElement("a");
        link.href = url;
        link.download = filename;
        link.click();
      }, index * DOWNLOAD_SPACING_MS);
    });
  }

  function selectTimelineCandidate(candidate: Candidate) {
    setSelectedCandidateId(candidate.candidate_id);
    // Candidates beyond the auto-rendered top 10 render on demand when selected from the timeline.
    if (!(candidate.candidate_id in clips)) {
      setClips((current) => ({
        ...current,
        [candidate.candidate_id]: { kind: "pending" },
      }));
      void renderRef.current?.(candidate, false);
    }
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
        {(captionsStatus === "unavailable" || captionsFallback !== "none") && (
          <p
            role="status"
            className="rounded-control border border-border-subtle bg-surface-elevated px-4 py-3 text-sm text-ink-secondary"
          >
            {captionsStatus === "unavailable" ? (
              <>
                Clips are rendered without captions because captioning
                isn&apos;t available on this server. Captions need{" "}
                <code className="rounded bg-surface-subtle px-1 py-0.5 font-mono text-xs text-ink-primary">
                  uv sync --extra captions
                </code>{" "}
                and an FFmpeg build with the libass{" "}
                <code className="rounded bg-surface-subtle px-1 py-0.5 font-mono text-xs text-ink-primary">
                  subtitles
                </code>{" "}
                filter.
              </>
            ) : captionsFallback === "done" ? (
              "Captioning failed on the server for one or more clips, so they were rendered without captions."
            ) : (
              "Captioning failed on the server for one or more clips; retrying without captions…"
            )}
          </p>
        )}
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
            <div
              role="group"
              aria-label="Results view"
              className="inline-flex rounded-lg bg-zinc-100 p-1 dark:bg-zinc-800"
            >
              <button
                type="button"
                aria-pressed={viewMode === "cards"}
                onClick={() => setViewMode("cards")}
                className={`rounded-md px-3 py-1.5 text-sm font-medium transition focus-visible:ring-2 focus-visible:ring-amber-500 focus-visible:outline-none ${
                  viewMode === "cards"
                    ? "bg-white text-zinc-950 shadow-xs dark:bg-zinc-950 dark:text-zinc-50"
                    : "text-zinc-600 hover:text-zinc-900 dark:text-zinc-400 dark:hover:text-zinc-100"
                }`}
              >
                Clip cards
              </button>
              <button
                type="button"
                aria-pressed={viewMode === "timeline"}
                onClick={() => setViewMode("timeline")}
                className={`rounded-md px-3 py-1.5 text-sm font-medium transition focus-visible:ring-2 focus-visible:ring-amber-500 focus-visible:outline-none ${
                  viewMode === "timeline"
                    ? "bg-white text-zinc-950 shadow-xs dark:bg-zinc-950 dark:text-zinc-50"
                    : "text-zinc-600 hover:text-zinc-900 dark:text-zinc-400 dark:hover:text-zinc-100"
                }`}
              >
                Timeline
              </button>
            </div>
            <button
              type="button"
              onClick={() => setHighlightsOpen(true)}
              className="rounded-lg bg-white px-4 py-2 text-sm font-semibold text-zinc-900 ring-1 ring-zinc-300 hover:bg-zinc-50 focus-visible:ring-2 focus-visible:ring-amber-500 focus-visible:outline-none dark:bg-zinc-900 dark:text-zinc-100 dark:ring-zinc-700 dark:hover:bg-zinc-800"
            >
              Make highlights video
            </button>
            <button
              type="button"
              onClick={downloadAll}
              disabled={ready.length === 0}
              className="rounded-lg bg-zinc-900 px-4 py-2 text-sm font-semibold text-white hover:bg-zinc-700 focus-visible:ring-2 focus-visible:ring-amber-500 focus-visible:outline-none disabled:opacity-50 dark:bg-zinc-100 dark:text-zinc-900 dark:hover:bg-zinc-300"
            >
              Download all{ready.length > 0 ? ` (${ready.length})` : ""}
            </button>
          </div>
          {viewMode === "timeline" ? (
            <StreamTimeline
              jobId={jobId}
              videoId={vod.video_id}
              vod={vod}
              durationS={vod.duration_s}
              candidates={view.result.candidates}
              selectedId={selectedCandidateId}
              onSelect={selectTimelineCandidate}
              clips={clips}
              onRetry={retry}
            />
          ) : (
            <div>
              <ol
                role="list"
                className="grid grid-cols-1 gap-5 min-[480px]:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4"
              >
                {top.map((candidate) => (
                  <ClipCard
                    key={candidate.candidate_id}
                    jobId={jobId}
                    candidate={candidate}
                    scoreFraction={scale(candidate.score)}
                    clip={clips[candidate.candidate_id] ?? { kind: "pending" }}
                    onRetry={() => retry(candidate)}
                    videoId={vod.video_id}
                    vod={vod}
                  />
                ))}
              </ol>
            </div>
          )}
        </section>
      )}

      {highlightsOpen && (
        <HighlightsDialog
          jobId={jobId}
          videoId={vod.video_id}
          channelName={vod.broadcaster_display_name}
          vodCreatedAt={vod.created_at}
          vodDurationS={vod.duration_s}
          candidates={top}
          initialTrims={(() => {
            const savedTrims = rememberedTrims(jobId);
            const merged: Record<string, SavedTrim> = {};
            for (const candidate of top) {
              const saved = savedTrims[candidate.candidate_id];
              if (saved) {
                merged[candidate.candidate_id] = saved;
                continue;
              }
              if (isTrimReset(jobId, candidate.candidate_id)) {
                continue;
              }
              const clip = clips[candidate.candidate_id];
              if (clip?.kind === "export") {
                merged[candidate.candidate_id] = {
                  start_s: clip.export.start_s,
                  end_s: clip.export.end_s,
                };
              }
            }
            return merged;
          })()}
          exportBounds={exportBoundsByCandidate}
          captionsStatus={captionsStatus}
          onClose={() => setHighlightsOpen(false)}
        />
      )}
    </div>
  );
}
