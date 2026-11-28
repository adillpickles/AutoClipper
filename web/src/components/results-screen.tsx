"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useRef, useState } from "react";

import { ArchiveDownloads } from "@/components/archive-downloads";
import { ClipCard, type ClipExport } from "@/components/clip-card";
import { HighlightsDialog } from "@/components/highlights-dialog";
import { ReanalyzeAction } from "@/components/reanalyze-action";
import { StreamTimeline } from "@/components/stream-timeline";
import { buttonClasses } from "@/components/ui/button";
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
  // The server already queued these exports when the analysis finished (§ 6.1a, #187), so the
  // default request below (the candidate's own start/end, center crop, captions as `/health`
  // says) must match that one field for field: the server then returns its queued, running or
  // finished render instead of a new one (§ 6.1, #186). Export IDs are also remembered per job.
  // `renderRef` lets a card's Retry button run the same flow.
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
      <h1 className="font-display text-2xl font-semibold tracking-tight text-ink-primary">
        Loading the results…
      </h1>
    );
  }
  if (view.kind === "problem") {
    return (
      <div className="flex flex-col gap-4">
        <h1 className="font-display text-2xl font-semibold tracking-tight text-ink-primary">
          {view.title}
        </h1>
        <p className="text-ink-secondary">{view.message}</p>
        <Link
          href={`/jobs/${jobId}`}
          className="self-start rounded-[3px] font-semibold text-accent underline decoration-accent/40 underline-offset-[3px] hover:text-accent-hover focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus-ring"
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
        <p className="text-sm font-medium text-ink-tertiary">
          {vod.broadcaster_display_name} · {formatTime(vod.duration_s)} · VOD{" "}
          {vod.video_id}
        </p>
        <h1 className="font-display text-2xl font-semibold tracking-tight text-balance break-words text-ink-primary sm:text-3xl">
          {vod.title}
        </h1>
        {view.result.warnings.map((warning) => (
          <p
            key={warning.code}
            className="rounded-control border border-signal-border bg-signal-soft px-4 py-3 text-sm text-signal-soft-ink"
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
        <ReanalyzeAction key={jobId} jobId={jobId} />
      </div>

      {top.length === 0 ? (
        <div className="flex flex-col gap-3 rounded-panel border border-dashed border-border-strong bg-surface-panel/60 p-8 text-center">
          <h2 className="font-display text-lg font-semibold text-ink-primary">
            No moments stood out
          </h2>
          <p className="text-ink-secondary">
            {view.result.warnings.length > 0
              ? "Nothing could be ranked from the signals that were available for this VOD."
              : view.result.n_viewer_clips_total === 0
                ? "Chat and audio stayed steady through this VOD and nobody clipped it, so there's nothing to rank."
                : "Chat and audio stayed steady through this VOD, so there's nothing to rank."}
          </p>
          <Link
            href="/"
            className="self-center rounded-[3px] font-semibold text-accent underline decoration-accent/40 underline-offset-[3px] hover:text-accent-hover focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus-ring"
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
              <h2
                id="clips-heading"
                className="font-display text-lg font-semibold text-ink-primary"
              >
                Top {top.length} {top.length === 1 ? "moment" : "moments"}
              </h2>
              <p className="text-sm text-ink-secondary" aria-live="polite">
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
              className="inline-flex rounded-control border border-border-control bg-surface-canvas p-0.5"
            >
              <button
                type="button"
                aria-pressed={viewMode === "cards"}
                onClick={() => setViewMode("cards")}
                className={`min-h-8 rounded-[0.375rem] px-3 text-sm font-semibold transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus-ring ${
                  viewMode === "cards"
                    ? "bg-surface-subtle text-ink-primary shadow-card"
                    : "text-ink-tertiary hover:text-ink-primary"
                }`}
              >
                Clip cards
              </button>
              <button
                type="button"
                aria-pressed={viewMode === "timeline"}
                onClick={() => setViewMode("timeline")}
                className={`min-h-8 rounded-[0.375rem] px-3 text-sm font-semibold transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus-ring ${
                  viewMode === "timeline"
                    ? "bg-surface-subtle text-ink-primary shadow-card"
                    : "text-ink-tertiary hover:text-ink-primary"
                }`}
              >
                Timeline
              </button>
            </div>
            <button
              type="button"
              onClick={() => setHighlightsOpen(true)}
              className={buttonClasses()}
            >
              Make highlights video
            </button>
          </div>
          <ArchiveDownloads key={jobId} jobId={jobId} />
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
              activity={view.result.activity}
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
