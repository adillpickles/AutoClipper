"use client";

// Clip editor (#31, api-contract.md § 5-6): preview a candidate's video, nudge start/end
// within the allowed export bounds (with reset), pick layout and captions, and re-render.

import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useId, useRef, useState } from "react";

import {
  ApiRequestError,
  getApiClient,
  POLL_INTERVAL_MS,
} from "@/lib/api/client";
import { EXPORT_STAGES } from "@/lib/api/stages";
import type {
  AnalysisResult,
  Candidate,
  CandidateMedia,
  Export,
  Layout,
} from "@/lib/api/types";
import { buildClipFilename } from "@/lib/filenames";
import { formatTime } from "@/lib/format";
import {
  clearRememberedTrim,
  rememberExport,
  rememberTrim,
  rememberedTrim,
} from "@/lib/job-state";
import { reasonFor, signalDetailsFor, titleFor } from "@/lib/reason";
import {
  clampTrim,
  MIN_TRIM_DURATION_S,
  nudgeEnd,
  nudgeStart,
  trimError,
  unavailableReasonLabel,
  type TrimBounds,
  type TrimInterval,
} from "@/lib/trim";

const FIRST_BACKOFF_MS = 2000;
const MAX_BACKOFF_MS = 10_000;
const GIVE_UP_AFTER_MS = 60_000;
const HEALTH_TIMEOUT_MS = 3000;

const KNOWN_LAYOUTS: readonly Layout[] = [
  "center_crop",
  "configured_region",
  "stacked",
];

const LAYOUT_LABEL: Record<Layout, string> = {
  center_crop: "Center crop (9:16)",
  configured_region: "Configured region (9:16)",
  stacked: "Stacked (facecam + gameplay)",
};

type LoadState =
  | { kind: "loading" }
  | { kind: "ready"; result: AnalysisResult; candidate: Candidate }
  | { kind: "problem"; title: string; message: string };

type RenderState =
  | { kind: "idle" }
  | { kind: "active"; export: Export }
  | { kind: "done"; export: Export; url: string }
  | { kind: "failed"; message: string };

function supportedLayouts(layouts: string[]): Layout[] {
  return layouts.filter((layout): layout is Layout =>
    (KNOWN_LAYOUTS as readonly string[]).includes(layout),
  );
}

export function ClipEditor() {
  const { jobId, candidateId } = useParams<{
    jobId: string;
    candidateId: string;
  }>();
  const [loaded, setLoaded] = useState<LoadState>({ kind: "loading" });
  const [media, setMedia] = useState<CandidateMedia | null>(null);
  const [mediaError, setMediaError] = useState<string | null>(null);
  const [trim, setTrim] = useState<TrimInterval>({ startS: 0, endS: 0 });
  const [startInput, setStartInput] = useState("0");
  const [endInput, setEndInput] = useState("0");
  const [layout, setLayout] = useState<Layout>("center_crop");
  const [captions, setCaptions] = useState(false);
  const [captionsStatus, setCaptionsStatus] = useState<
    "checking" | "available" | "unavailable" | "unknown"
  >("checking");
  const [render, setRender] = useState<RenderState>({ kind: "idle" });
  const [videoFailed, setVideoFailed] = useState(false);
  const [mediaRetryCount, setMediaRetryCount] = useState(0);
  const [retryingPreview, setRetryingPreview] = useState(false);
  const [whyOpen, setWhyOpen] = useState(false);
  const whyPanelId = useId();
  const trimStatusId = useId();

  const videoRef = useRef<HTMLVideoElement>(null);
  const pollRunRef = useRef(0);
  const captionsTouchedRef = useRef(false);

  // Check whether the server has captioning installed; keep captions off while checking so
  // manual uncaptioned renders stay usable without submitting `captions: true` prematurely.
  useEffect(() => {
    let cancelled = false;
    let settled = false;
    const timeout = setTimeout(() => {
      if (settled || cancelled) return;
      settled = true;
      setCaptionsStatus("unknown");
    }, HEALTH_TIMEOUT_MS);
    getApiClient()
      .getHealth()
      .then((health) => {
        if (settled || cancelled) return;
        settled = true;
        clearTimeout(timeout);
        const available = health.captions_available !== false;
        setCaptionsStatus(available ? "available" : "unavailable");
        if (!captionsTouchedRef.current) {
          setCaptions(available);
        }
      })
      .catch(() => {
        if (settled || cancelled) return;
        settled = true;
        clearTimeout(timeout);
        setCaptionsStatus("unknown");
      });
    return () => {
      cancelled = true;
      clearTimeout(timeout);
    };
  }, []);

  // Load the analysis result and locate this candidate.
  useEffect(() => {
    let cancelled = false;
    getApiClient()
      .getResult(jobId)
      .then((result) => {
        if (cancelled) return;
        setMedia(null);
        setMediaError(null);
        setVideoFailed(false);
        setRetryingPreview(false);
        setWhyOpen(false);
        const candidate = result.candidates.find(
          (item) => item.candidate_id === candidateId,
        );
        if (!candidate) {
          setLoaded({
            kind: "problem",
            title: "Moment not found",
            message: "This moment isn't part of the analysis.",
          });
          return;
        }
        const saved = rememberedTrim(jobId, candidate.candidate_id);
        const initialStart =
          saved && saved.end_s <= result.vod.duration_s
            ? saved.start_s
            : candidate.start_s;
        const initialEnd =
          saved && saved.end_s <= result.vod.duration_s
            ? saved.end_s
            : candidate.end_s;
        setTrim({ startS: initialStart, endS: initialEnd });
        setStartInput(String(initialStart));
        setEndInput(String(initialEnd));
        setLoaded({ kind: "ready", result, candidate });
      })
      .catch((error: unknown) => {
        if (cancelled) return;
        if (error instanceof ApiRequestError) {
          setLoaded({
            kind: "problem",
            title: "Can't open this clip",
            message: error.error.message,
          });
        } else {
          setLoaded({
            kind: "problem",
            title: "Can't reach the server",
            message: "Check that AutoClipper is running, then reload the page.",
          });
        }
      });
    return () => {
      cancelled = true;
    };
  }, [jobId, candidateId]);

  const readyData = loaded.kind === "ready" ? loaded : null;

  // Fetch candidate media and keep polling while `preview.status` is `preparing` (§ 9.1).
  useEffect(() => {
    if (!readyData) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let failingSince: number | null = null;
    let backoff = FIRST_BACKOFF_MS;
    const client = getApiClient();

    async function pollMedia() {
      try {
        const next = await client.getCandidateMedia(jobId, candidateId);
        if (cancelled) return;
        failingSince = null;
        backoff = FIRST_BACKOFF_MS;
        setRetryingPreview(false);
        setMediaError(null);
        setMedia(next);
        const available = supportedLayouts(next.export.layouts);
        if (available.length > 0) {
          setLayout((current) =>
            available.includes(current) ? current : available[0],
          );
        }
        if (next.preview.status === "preparing") {
          timer = setTimeout(pollMedia, POLL_INTERVAL_MS);
        }
      } catch (error) {
        if (cancelled) return;
        if (error instanceof ApiRequestError) {
          setRetryingPreview(false);
          setMediaError(error.error.message);
          return;
        }
        failingSince ??= Date.now();
        if (Date.now() - failingSince > GIVE_UP_AFTER_MS) {
          setRetryingPreview(false);
          setMediaError("Can't reach the server to load media details.");
          return;
        }
        timer = setTimeout(pollMedia, backoff);
        backoff = Math.min(backoff * 2, MAX_BACKOFF_MS);
      }
    }

    void pollMedia();
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [readyData, jobId, candidateId, mediaRetryCount]);

  // Cancel any in-flight export polling on unmount.
  useEffect(() => {
    return () => {
      pollRunRef.current += 1;
    };
  }, []);

  if (loaded.kind === "loading") {
    return (
      <h1 className="text-2xl font-semibold tracking-tight">
        Loading the clip editor…
      </h1>
    );
  }

  if (loaded.kind === "problem") {
    return (
      <div className="flex flex-col gap-4">
        <h1 className="text-2xl font-semibold tracking-tight">
          {loaded.title}
        </h1>
        <p className="text-zinc-700 dark:text-zinc-300">{loaded.message}</p>
        <Link
          href={`/jobs/${jobId}/results`}
          className="self-start font-medium text-amber-800 underline dark:text-amber-300"
        >
          Back to clips
        </Link>
      </div>
    );
  }

  const { result, candidate } = loaded;
  const client = getApiClient();
  const exportInfo = media?.export ?? null;
  const previewInfo = media?.preview ?? null;

  const bounds: TrimBounds = {
    minStartS: exportInfo?.min_start_s ?? 0,
    maxEndS: exportInfo?.max_end_s ?? result.vod.duration_s,
    maxDurationS: exportInfo?.max_duration_s ?? 90,
  };

  // `bounds` is only the whole-VOD fallback until export is `available` with real limits: before
  // the first media response, after a failed request, and while export is unavailable (the
  // contract then sends null limits). An edit there could leave, and save to job state, a trim
  // outside the real export limits, so trim edits stay locked and the status line says why.
  const limitsKnown =
    exportInfo?.status === "available" &&
    exportInfo.min_start_s !== null &&
    exportInfo.max_end_s !== null;
  const trimLocked = !limitsKnown;
  const trimLockProps = {
    disabled: trimLocked,
    "aria-describedby": trimLocked ? trimStatusId : undefined,
  };

  const layouts = exportInfo ? supportedLayouts(exportInfo.layouts) : [];
  const validationError = trimError(trim, bounds);
  const canExport =
    exportInfo?.status === "available" &&
    layouts.length > 0 &&
    validationError === null &&
    render.kind !== "active";

  const previewPlayable =
    previewInfo?.status === "available" &&
    previewInfo.kind === "video_file" &&
    previewInfo.url !== null &&
    previewInfo.media_start_s !== null &&
    previewInfo.media_end_s !== null;

  const previewUrl =
    previewPlayable && previewInfo?.url
      ? client.resolveUrl(previewInfo.url)
      : null;
  const mediaStartS = previewInfo?.media_start_s ?? 0;
  const mediaEndS = previewInfo?.media_end_s ?? result.vod.duration_s;

  const canRetryPreview =
    videoFailed ||
    mediaError !== null ||
    previewInfo?.reason === "fetch_failed";
  const isPermanentUnavailable =
    !videoFailed &&
    (previewInfo?.reason === "source_unavailable" ||
      previewInfo?.reason === "local_file_missing");

  function retryPreview() {
    setMediaError(null);
    setVideoFailed(false);
    setRetryingPreview(true);
    setMediaRetryCount((count) => count + 1);
  }

  const isModified =
    Math.abs(trim.startS - candidate.start_s) > 1e-6 ||
    Math.abs(trim.endS - candidate.end_s) > 1e-6;

  function seekToVodTime(vodTimeS: number) {
    const element = videoRef.current;
    if (!element || !previewPlayable) return;
    const clamped = Math.min(mediaEndS, Math.max(mediaStartS, vodTimeS));
    element.currentTime = Math.max(0, clamped - mediaStartS);
  }

  function playSelection() {
    const element = videoRef.current;
    if (!element || !previewPlayable) return;
    seekToVodTime(trim.startS);
    void element.play().catch(() => {
      // Autoplay policies only apply without user gesture; ignore rejected promises.
    });
  }

  function handleTimeUpdate() {
    const element = videoRef.current;
    if (!element || !previewPlayable) return;
    const stopAt = Math.max(0, trim.endS - mediaStartS);
    if (!element.paused && element.currentTime >= stopAt) {
      element.pause();
      element.currentTime = stopAt;
    }
  }

  function clearStaleRender() {
    // Invalidate any in-flight poll too so an earlier trim's export never publishes under a
    // newer selection.
    pollRunRef.current += 1;
    setRender((current) =>
      current.kind === "idle" ? current : { kind: "idle" },
    );
  }

  function persistTrim(next: TrimInterval) {
    if (
      Math.abs(next.startS - candidate.start_s) <= 1e-6 &&
      Math.abs(next.endS - candidate.end_s) <= 1e-6
    ) {
      clearRememberedTrim(jobId, candidate.candidate_id, { recordReset: true });
    } else {
      rememberTrim(jobId, candidate.candidate_id, {
        start_s: next.startS,
        end_s: next.endS,
      });
    }
  }

  function applyTrim(next: TrimInterval, seekToStart = false) {
    const changed = next.startS !== trim.startS || next.endS !== trim.endS;
    setStartInput(String(next.startS));
    setEndInput(String(next.endS));
    if (!changed) return;
    setTrim(next);
    persistTrim(next);
    clearStaleRender();
    if (seekToStart) seekToVodTime(next.startS);
  }

  function commitStartInput() {
    if (startInput === String(trim.startS)) return;
    const parsed = startInput.trim() === "" ? NaN : Number(startInput);
    const next = Number.isFinite(parsed)
      ? clampTrim(parsed, trim.endS, bounds, "end")
      : trim;
    applyTrim(next, true);
  }

  function commitEndInput() {
    if (endInput === String(trim.endS)) return;
    const parsed = endInput.trim() === "" ? NaN : Number(endInput);
    const next = Number.isFinite(parsed)
      ? clampTrim(trim.startS, parsed, bounds, "start")
      : trim;
    applyTrim(next);
  }

  function setFromPlayhead(edge: "start" | "end") {
    const element = videoRef.current;
    if (!element || !previewPlayable) return;
    const vodTime = mediaStartS + element.currentTime;
    const next =
      edge === "start"
        ? clampTrim(vodTime, Math.max(vodTime + 0.5, trim.endS), bounds, "end")
        : clampTrim(
            Math.min(trim.startS, vodTime - 0.5),
            vodTime,
            bounds,
            "start",
          );
    applyTrim(next);
  }

  function resetTrim() {
    applyTrim({ startS: candidate.start_s, endS: candidate.end_s }, true);
  }

  async function reRender() {
    const startEdited = startInput !== String(trim.startS);
    const endEdited = endInput !== String(trim.endS);
    let effective = trim;
    if (startEdited || endEdited) {
      const parsedStart = startEdited
        ? startInput.trim() === ""
          ? NaN
          : Number(startInput)
        : trim.startS;
      const parsedEnd = endEdited
        ? endInput.trim() === ""
          ? NaN
          : Number(endInput)
        : trim.endS;
      if (Number.isFinite(parsedStart) && Number.isFinite(parsedEnd)) {
        effective = clampTrim(
          parsedStart,
          parsedEnd,
          bounds,
          startEdited && !endEdited ? "end" : "start",
        );
      }
      setTrim(effective);
      setStartInput(String(effective.startS));
      setEndInput(String(effective.endS));
      persistTrim(effective);
    }
    if (
      exportInfo?.status !== "available" ||
      layouts.length === 0 ||
      trimError(effective, bounds) !== null ||
      render.kind === "active"
    ) {
      return;
    }
    persistTrim(effective);
    const runId = ++pollRunRef.current;
    captionsTouchedRef.current = true;
    const wait = (ms: number) =>
      new Promise<void>((resolve) => setTimeout(resolve, ms));

    try {
      const captionsAllowed =
        captionsStatus === "available" || captionsStatus === "unknown";
      const { export: started } = await client.createExport({
        job_id: jobId,
        candidate_id: candidate.candidate_id,
        start_s: effective.startS,
        end_s: effective.endS,
        layout,
        captions: captionsAllowed ? captions : false,
      });
      if (pollRunRef.current !== runId) return;
      rememberExport(jobId, candidate.candidate_id, started.export_id);

      let current = started;
      let failingSince: number | null = null;
      let backoff = FIRST_BACKOFF_MS;

      while (pollRunRef.current === runId) {
        if (current.state === "done" && current.download_url) {
          setRender({
            kind: "done",
            export: current,
            url: client.resolveUrl(current.download_url),
          });
          return;
        }
        if (current.state === "failed") {
          setRender({
            kind: "failed",
            message: current.error?.message ?? "The clip couldn't be rendered.",
          });
          return;
        }
        setRender({ kind: "active", export: current });
        await wait(POLL_INTERVAL_MS);
        if (pollRunRef.current !== runId) return;

        try {
          current = await client.getExport(current.export_id);
          failingSince = null;
          backoff = FIRST_BACKOFF_MS;
        } catch (error) {
          if (pollRunRef.current !== runId) return;
          if (error instanceof ApiRequestError) {
            setRender({ kind: "failed", message: error.error.message });
            return;
          }
          failingSince ??= Date.now();
          if (Date.now() - failingSince > GIVE_UP_AFTER_MS) {
            setRender({
              kind: "failed",
              message: "Can't reach the server while rendering this clip.",
            });
            return;
          }
          await wait(backoff);
          backoff = Math.min(backoff * 2, MAX_BACKOFF_MS);
        }
      }
    } catch (error) {
      if (pollRunRef.current !== runId) return;
      if (error instanceof ApiRequestError) {
        setRender({ kind: "failed", message: error.error.message });
      } else {
        setRender({
          kind: "failed",
          message:
            "Can't reach the server. Check that it's running and try again.",
        });
      }
    }
  }

  const selectionDurationS = Math.max(0, trim.endS - trim.startS);
  const downloadFilename =
    (render.kind === "done" ? render.export.filename : null) ??
    buildClipFilename({
      channel: result.vod.broadcaster_display_name,
      createdAt: result.vod.created_at,
      title: candidate.title ?? null,
      rank: candidate.rank,
      startS: trim.startS,
      fallbackId: result.vod.video_id,
    });
  const clipTitle = titleFor(candidate);
  const clipReason = reasonFor(candidate);
  const signalDetails = signalDetailsFor(candidate);

  return (
    <div className="flex flex-col gap-8">
      <div className="flex flex-col gap-2">
        <Link
          href={`/jobs/${jobId}/results`}
          className="self-start text-sm font-medium text-amber-800 hover:underline dark:text-amber-300"
        >
          ← Back to clips
        </Link>
        <p className="text-sm font-medium text-zinc-600 dark:text-zinc-400">
          Clip #{candidate.rank} · {result.vod.broadcaster_display_name} · VOD{" "}
          {result.vod.video_id}
        </p>
        <h1 className="text-2xl font-semibold tracking-tight text-balance sm:text-3xl">
          {clipTitle}
        </h1>
        <p className="max-w-3xl text-sm leading-relaxed text-pretty text-ink-secondary">
          {clipReason}
        </p>
        {signalDetails.length > 0 && (
          <div className="mt-1 flex flex-col gap-2">
            <button
              type="button"
              onClick={() => setWhyOpen((open) => !open)}
              aria-expanded={whyOpen}
              aria-controls={whyPanelId}
              className="inline-flex min-h-8 items-center gap-1.5 self-start rounded-pill border border-border-subtle bg-surface-subtle/70 px-3 py-1 text-xs font-medium text-ink-secondary transition-colors hover:border-border-strong hover:text-ink-primary focus-visible:ring-2 focus-visible:ring-accent focus-visible:outline-none"
            >
              <span>Why this moment</span>
              <span aria-hidden="true" className="text-[10px] leading-none">
                {whyOpen ? "▴" : "▾"}
              </span>
            </button>
            {whyOpen && (
              <dl
                id={whyPanelId}
                aria-label="Contributing signals"
                className="grid max-w-2xl grid-cols-1 gap-2 rounded-control border border-border-subtle bg-surface-panel p-3.5 text-xs sm:grid-cols-2"
              >
                {signalDetails.map((item, idx) => (
                  <div
                    key={`${item.kind}-${idx}`}
                    className="flex flex-col gap-0.5 rounded-md bg-surface-subtle/50 px-2.5 py-2"
                  >
                    <dt className="font-medium text-ink-primary">
                      {item.label}
                    </dt>
                    <dd className="text-pretty break-words text-ink-secondary">
                      {item.detail}
                    </dd>
                  </div>
                ))}
              </dl>
            )}
          </div>
        )}
      </div>

      {mediaError && previewPlayable && !videoFailed && !retryingPreview && (
        <div
          role="alert"
          className="flex flex-wrap items-center justify-between gap-3 rounded-lg bg-red-50 px-4 py-3 text-sm text-red-800 dark:bg-red-950/50 dark:text-red-200"
        >
          <p>{mediaError}</p>
          <button
            type="button"
            onClick={retryPreview}
            className="rounded-md bg-red-100 px-3 py-1 text-xs font-semibold text-red-900 hover:bg-red-200 dark:bg-red-900/60 dark:text-red-100 dark:hover:bg-red-900"
          >
            Retry preview
          </button>
        </div>
      )}

      <div className="grid grid-cols-1 gap-8 lg:grid-cols-12">
        <section
          aria-label="Clip preview"
          className="flex flex-col gap-4 lg:col-span-7"
        >
          <div
            className={`relative w-full overflow-hidden rounded-2xl border border-zinc-200 bg-zinc-950 dark:border-zinc-800 ${
              previewPlayable && previewUrl && !videoFailed && !retryingPreview
                ? "aspect-video"
                : "flex min-h-64 items-center justify-center sm:aspect-video"
            }`}
          >
            {previewPlayable &&
            previewUrl &&
            !videoFailed &&
            !retryingPreview ? (
              <video
                ref={videoRef}
                src={previewUrl}
                controls
                playsInline
                preload="metadata"
                onLoadedMetadata={() => seekToVodTime(trim.startS)}
                onTimeUpdate={handleTimeUpdate}
                onError={() => setVideoFailed(true)}
                className="size-full object-contain"
                aria-label={`Preview of clip ${candidate.rank}`}
              />
            ) : retryingPreview ||
              (mediaError === null &&
                (previewInfo?.status === "preparing" || media === null)) ? (
              <div className="flex size-full flex-col items-center justify-center gap-3 p-6 text-center text-sm text-zinc-300">
                <span
                  className="size-7 animate-spin rounded-full border-2 border-amber-500 border-t-transparent"
                  aria-hidden="true"
                />
                <p>
                  {retryingPreview
                    ? "Retrying video preview…"
                    : (previewInfo?.message ?? "Loading video preview…")}
                </p>
              </div>
            ) : (
              <div
                role={mediaError !== null ? "alert" : "status"}
                className="flex size-full flex-col items-center justify-center gap-3 p-6 text-center"
              >
                <span
                  className={`rounded-full px-3 py-1 text-xs font-semibold ${
                    isPermanentUnavailable
                      ? "bg-red-500/20 text-red-200"
                      : "bg-amber-500/20 text-amber-200"
                  }`}
                >
                  {videoFailed
                    ? "Preview playback failed"
                    : mediaError !== null &&
                        previewInfo?.status !== "unavailable"
                      ? "Connection failed"
                      : unavailableReasonLabel(previewInfo?.reason ?? null)}
                </span>
                <h2 className="text-lg font-semibold text-white">
                  Video unavailable
                </h2>
                <p className="max-w-md text-sm text-zinc-300">
                  {videoFailed
                    ? "The preview video couldn't be played in this browser."
                    : mediaError !== null &&
                        previewInfo?.status !== "unavailable"
                      ? mediaError
                      : (previewInfo?.message ??
                        "Preview isn't available for this moment.")}
                </p>
                {canRetryPreview && (
                  <>
                    {exportInfo?.status === "available" && (
                      <p className="max-w-md text-xs text-zinc-400">
                        Your trim and export settings are kept. You can retry
                        the preview or export directly.
                      </p>
                    )}
                    {exportInfo === null && (
                      <p className="max-w-md text-xs text-zinc-400">
                        Trimming and export are paused until the media limits
                        load. Retry to load them.
                      </p>
                    )}
                    {exportInfo !== null &&
                      exportInfo.status !== "available" && (
                        <p className="max-w-md text-xs text-zinc-400">
                          Trimming and export are paused while this clip
                          can&apos;t be exported. Retry to check again.
                        </p>
                      )}
                    <button
                      type="button"
                      onClick={retryPreview}
                      className="mt-1 rounded-lg bg-amber-500 px-4 py-2 text-sm font-semibold text-zinc-950 hover:bg-amber-400 focus-visible:ring-2 focus-visible:ring-amber-300 focus-visible:outline-none"
                    >
                      Retry preview
                    </button>
                  </>
                )}
                {previewInfo?.reason === "source_unavailable" &&
                  !videoFailed && (
                    <Link
                      href="/"
                      className="mt-1 rounded-lg bg-zinc-800 px-4 py-2 text-sm font-medium text-zinc-100 ring-1 ring-zinc-700 hover:bg-zinc-700 focus-visible:ring-2 focus-visible:ring-amber-400 focus-visible:outline-none"
                    >
                      Analyze with a local file
                    </Link>
                  )}
                {previewInfo?.reason && !videoFailed && (
                  <p className="font-mono text-xs text-zinc-400">
                    Reason: {previewInfo.reason}
                  </p>
                )}
              </div>
            )}
          </div>

          {previewPlayable && !videoFailed && !retryingPreview && (
            <div className="flex flex-wrap items-center gap-2">
              <button
                type="button"
                onClick={playSelection}
                className="rounded-lg bg-zinc-900 px-3.5 py-2 text-sm font-medium text-white hover:bg-zinc-700 focus-visible:ring-2 focus-visible:ring-amber-500 focus-visible:outline-none dark:bg-zinc-100 dark:text-zinc-900 dark:hover:bg-zinc-300"
              >
                Play selection
              </button>
              <button
                type="button"
                onClick={() => setFromPlayhead("start")}
                className={PLAYHEAD_BUTTON}
                {...trimLockProps}
              >
                Set start to playhead
              </button>
              <button
                type="button"
                onClick={() => setFromPlayhead("end")}
                className={PLAYHEAD_BUTTON}
                {...trimLockProps}
              >
                Set end to playhead
              </button>
              <span className="ml-auto text-xs text-zinc-600 tabular-nums dark:text-zinc-400">
                Preview window: {formatTime(mediaStartS)}–
                {formatTime(mediaEndS)}
              </span>
            </div>
          )}
        </section>

        <section
          aria-label="Trim and export settings"
          className="flex flex-col gap-6 rounded-2xl border border-zinc-200 bg-white p-6 lg:col-span-5 dark:border-zinc-800 dark:bg-zinc-900"
        >
          <div className="flex items-center justify-between gap-2">
            <div>
              <h2 className="text-lg font-semibold">Trim &amp; export</h2>
              <p className="text-sm text-zinc-600 tabular-nums dark:text-zinc-400">
                {formatTime(trim.startS)}–{formatTime(trim.endS)} (
                {selectionDurationS.toFixed(1)}s)
              </p>
            </div>
            <button
              type="button"
              onClick={resetTrim}
              disabled={!isModified || trimLocked}
              aria-describedby={trimLockProps["aria-describedby"]}
              className="rounded-lg bg-white px-3 py-1.5 text-sm font-medium text-zinc-800 ring-1 ring-zinc-300 hover:bg-zinc-50 disabled:opacity-45 dark:bg-zinc-900 dark:text-zinc-200 dark:ring-zinc-700 dark:hover:bg-zinc-800"
            >
              Reset
            </button>
          </div>

          <div className="flex flex-col gap-4">
            <div className="flex flex-col gap-2">
              <div className="flex items-center justify-between">
                <label htmlFor="trim-start" className="text-sm font-medium">
                  Start (seconds)
                </label>
                <span className="text-xs text-zinc-500 tabular-nums">
                  {formatTime(trim.startS)}
                </span>
              </div>
              <div className="flex items-center gap-2">
                <button
                  type="button"
                  aria-label="Move start 5 seconds earlier"
                  onClick={() => applyTrim(nudgeStart(trim, -5, bounds), true)}
                  className={NUDGE_BUTTON}
                  {...trimLockProps}
                >
                  -5s
                </button>
                <button
                  type="button"
                  aria-label="Move start 1 second earlier"
                  onClick={() => applyTrim(nudgeStart(trim, -1, bounds), true)}
                  className={NUDGE_BUTTON}
                  {...trimLockProps}
                >
                  -1s
                </button>
                <input
                  id="trim-start"
                  type="number"
                  step="any"
                  min={bounds.minStartS}
                  max={bounds.maxEndS}
                  value={startInput}
                  onChange={(event) => {
                    setStartInput(event.target.value);
                    clearStaleRender();
                  }}
                  onBlur={commitStartInput}
                  onKeyDown={(event) => {
                    if (event.key === "Enter") commitStartInput();
                  }}
                  className={TIME_INPUT}
                  {...trimLockProps}
                />
                <button
                  type="button"
                  aria-label="Move start 1 second later"
                  onClick={() => applyTrim(nudgeStart(trim, 1, bounds), true)}
                  className={NUDGE_BUTTON}
                  {...trimLockProps}
                >
                  +1s
                </button>
                <button
                  type="button"
                  aria-label="Move start 5 seconds later"
                  onClick={() => applyTrim(nudgeStart(trim, 5, bounds), true)}
                  className={NUDGE_BUTTON}
                  {...trimLockProps}
                >
                  +5s
                </button>
              </div>
            </div>

            <div className="flex flex-col gap-2">
              <div className="flex items-center justify-between">
                <label htmlFor="trim-end" className="text-sm font-medium">
                  End (seconds)
                </label>
                <span className="text-xs text-zinc-500 tabular-nums">
                  {formatTime(trim.endS)}
                </span>
              </div>
              <div className="flex items-center gap-2">
                <button
                  type="button"
                  aria-label="Move end 5 seconds earlier"
                  onClick={() => applyTrim(nudgeEnd(trim, -5, bounds))}
                  className={NUDGE_BUTTON}
                  {...trimLockProps}
                >
                  -5s
                </button>
                <button
                  type="button"
                  aria-label="Move end 1 second earlier"
                  onClick={() => applyTrim(nudgeEnd(trim, -1, bounds))}
                  className={NUDGE_BUTTON}
                  {...trimLockProps}
                >
                  -1s
                </button>
                <input
                  id="trim-end"
                  type="number"
                  step="any"
                  min={bounds.minStartS}
                  max={bounds.maxEndS}
                  value={endInput}
                  onChange={(event) => {
                    setEndInput(event.target.value);
                    clearStaleRender();
                  }}
                  onBlur={commitEndInput}
                  onKeyDown={(event) => {
                    if (event.key === "Enter") commitEndInput();
                  }}
                  className={TIME_INPUT}
                  {...trimLockProps}
                />
                <button
                  type="button"
                  aria-label="Move end 1 second later"
                  onClick={() => applyTrim(nudgeEnd(trim, 1, bounds))}
                  className={NUDGE_BUTTON}
                  {...trimLockProps}
                >
                  +1s
                </button>
                <button
                  type="button"
                  aria-label="Move end 5 seconds later"
                  onClick={() => applyTrim(nudgeEnd(trim, 5, bounds))}
                  className={NUDGE_BUTTON}
                  {...trimLockProps}
                >
                  +5s
                </button>
              </div>
            </div>

            {trimLocked ? (
              <p
                id={trimStatusId}
                role="status"
                className="text-xs text-zinc-500 dark:text-zinc-400"
              >
                {exportInfo !== null
                  ? canRetryPreview
                    ? "Trimming is unavailable while this clip can't be exported. Use Retry preview to check again."
                    : "Trimming is unavailable while this clip can't be exported."
                  : mediaError !== null
                    ? "Trimming is unavailable because the media limits couldn't be loaded. Use Retry preview to try again."
                    : "Loading media limits… Trimming unlocks when they arrive."}
              </p>
            ) : (
              <p className="text-xs text-zinc-500 tabular-nums dark:text-zinc-400">
                Allowed range: {formatTime(bounds.minStartS)}–
                {formatTime(bounds.maxEndS)} · min {MIN_TRIM_DURATION_S}s
                {bounds.maxDurationS !== null
                  ? ` · max ${bounds.maxDurationS}s`
                  : ""}
              </p>
            )}
          </div>

          <fieldset className="flex flex-col gap-2">
            <legend className="text-sm font-medium">Layout</legend>
            {layouts.length > 0 ? (
              <div className="flex flex-col gap-2">
                {layouts.map((option) => (
                  <label
                    key={option}
                    className="flex items-center gap-2 text-sm text-zinc-800 dark:text-zinc-200"
                  >
                    <input
                      type="radio"
                      name="export-layout"
                      value={option}
                      checked={layout === option}
                      onChange={() => {
                        setLayout(option);
                        clearStaleRender();
                      }}
                    />
                    {LAYOUT_LABEL[option]}
                  </label>
                ))}
              </div>
            ) : (
              <p className="text-sm text-zinc-500 dark:text-zinc-400">
                No export layouts available right now.
              </p>
            )}
          </fieldset>

          <div className="flex flex-col gap-1">
            <label className="flex items-center gap-2 text-sm font-medium">
              <input
                type="checkbox"
                checked={
                  (captionsStatus === "available" ||
                    captionsStatus === "unknown") &&
                  captions
                }
                disabled={
                  captionsStatus === "checking" ||
                  captionsStatus === "unavailable"
                }
                onChange={(event) => {
                  captionsTouchedRef.current = true;
                  setCaptions(event.target.checked);
                  clearStaleRender();
                }}
              />
              Burn in captions
            </label>
            {captionsStatus === "checking" && (
              <p className="text-xs text-ink-tertiary">
                Checking server caption support…
              </p>
            )}
            {captionsStatus === "unavailable" && (
              <p className="text-xs text-ink-tertiary">
                Unavailable on this server — requires{" "}
                <code className="font-mono text-ink-secondary">
                  uv sync --extra captions
                </code>{" "}
                and FFmpeg with libass{" "}
                <code className="font-mono text-ink-secondary">subtitles</code>{" "}
                support.
              </p>
            )}
          </div>

          {exportInfo?.status === "unavailable" && (
            <div
              role="status"
              className="rounded-xl border border-amber-200 bg-amber-50 p-4 text-sm text-amber-950 dark:border-amber-900/60 dark:bg-amber-950/30 dark:text-amber-100"
            >
              <p className="font-semibold">
                Export unavailable ({unavailableReasonLabel(exportInfo.reason)})
              </p>
              <p className="mt-1">
                {exportInfo.message ?? "This clip can't be exported right now."}
              </p>
            </div>
          )}

          {validationError && exportInfo?.status !== "unavailable" && (
            <p
              role="alert"
              className="rounded-lg bg-amber-50 px-3 py-2 text-sm text-amber-900 dark:bg-amber-400/10 dark:text-amber-100"
            >
              {validationError}
            </p>
          )}

          <button
            type="button"
            onClick={reRender}
            disabled={!canExport}
            className="rounded-lg bg-amber-500 px-5 py-3 text-sm font-semibold text-zinc-950 hover:bg-amber-400 focus-visible:ring-2 focus-visible:ring-amber-700 focus-visible:outline-none disabled:opacity-50"
          >
            {render.kind === "active" ? "Rendering…" : "Re-render clip"}
          </button>

          {render.kind === "active" && (
            <ExportProgressPanel exportRecord={render.export} />
          )}

          {render.kind === "failed" && (
            <p
              role="alert"
              className="rounded-lg bg-red-50 px-4 py-3 text-sm text-red-800 dark:bg-red-950/50 dark:text-red-200"
            >
              {render.message}
            </p>
          )}

          {render.kind === "done" && (
            <div className="flex flex-col gap-3 rounded-xl border border-emerald-200 bg-emerald-50 p-4 dark:border-emerald-900/60 dark:bg-emerald-950/30">
              <p className="text-sm font-semibold text-emerald-900 dark:text-emerald-200">
                Clip ready ({formatTime(render.export.start_s)}–
                {formatTime(render.export.end_s)})
              </p>
              <video
                src={render.url}
                controls
                playsInline
                preload="metadata"
                className="mx-auto aspect-[9/16] max-h-72 rounded-lg bg-black"
                aria-label="Rendered vertical clip"
              />
              <a
                href={render.url}
                download={downloadFilename}
                className="self-start rounded-lg bg-emerald-700 px-4 py-2 text-sm font-semibold text-white hover:bg-emerald-600 focus-visible:ring-2 focus-visible:ring-emerald-500 focus-visible:outline-none"
              >
                Download clip
              </a>
            </div>
          )}
        </section>
      </div>
    </div>
  );
}

function ExportProgressPanel({ exportRecord }: { exportRecord: Export }) {
  const progress = exportRecord.progress;
  const current = progress?.stage_index ?? 0;
  const fraction = progress?.stage_fraction ?? null;

  return (
    <div
      aria-live="polite"
      className="flex flex-col gap-3 rounded-xl border border-zinc-200 bg-zinc-50 p-4 dark:border-zinc-800 dark:bg-zinc-950"
    >
      <p className="text-sm font-medium">
        {exportRecord.state === "queued"
          ? "Waiting in render queue…"
          : progress
            ? `Step ${progress.stage_index} of ${progress.stage_count}: ${progress.message}`
            : "Rendering…"}
      </p>
      {fraction !== null && progress && (
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
        {EXPORT_STAGES.map((stage, index) => {
          const stepNumber = index + 1;
          const done = stepNumber < current;
          const active =
            exportRecord.state === "running" && stepNumber === current;
          return (
            <li
              key={stage.stage}
              className={
                done
                  ? "text-emerald-700 dark:text-emerald-300"
                  : active
                    ? "font-semibold text-zinc-900 dark:text-zinc-100"
                    : "text-zinc-500 dark:text-zinc-400"
              }
            >
              {done ? "✓" : active ? "•" : "○"} {stage.message}
            </li>
          );
        })}
      </ol>
    </div>
  );
}

const PLAYHEAD_BUTTON =
  "rounded-lg bg-white px-3 py-2 text-sm font-medium text-zinc-800 ring-1 ring-zinc-300 hover:bg-zinc-50 disabled:cursor-not-allowed disabled:opacity-45 dark:bg-zinc-900 dark:text-zinc-200 dark:ring-zinc-700 dark:hover:bg-zinc-800";

const NUDGE_BUTTON =
  "rounded-lg bg-zinc-100 px-2.5 py-2 text-xs font-semibold tabular-nums text-zinc-800 hover:bg-zinc-200 focus-visible:ring-2 focus-visible:ring-amber-500 focus-visible:outline-none disabled:cursor-not-allowed disabled:opacity-45 dark:bg-zinc-800 dark:text-zinc-200 dark:hover:bg-zinc-700";

const TIME_INPUT =
  "w-full min-w-0 rounded-lg border border-zinc-300 bg-white px-3 py-2 text-center text-sm tabular-nums outline-none focus-visible:ring-2 focus-visible:ring-amber-500 disabled:cursor-not-allowed disabled:opacity-60 dark:border-zinc-700 dark:bg-zinc-950";
