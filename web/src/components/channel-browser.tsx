"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import {
  useCallback,
  useEffect,
  useId,
  useRef,
  useState,
  useSyncExternalStore,
  type FormEvent,
} from "react";

import {
  ChannelQueue,
  type ChannelQueueItem,
} from "@/components/channel-queue";
import { Button, ButtonLink } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/empty-state";
import { ErrorState } from "@/components/ui/error-state";
import { Skeleton } from "@/components/ui/skeleton";
import {
  CHANNEL_FORMAT_EXAMPLES,
  channelInputMessage,
  parseChannel,
} from "@/lib/api/channel";
import {
  ApiRequestError,
  getApiClient,
  POLL_INTERVAL_MS,
} from "@/lib/api/client";
import type { ChannelVod, ChannelVodsResponse } from "@/lib/api/types";
import { parseVod } from "@/lib/api/vod";
import { formatBroadcastDate, formatTime, formatViewCount } from "@/lib/format";

const QUEUE_STORAGE_KEY = "autoclipper.channelQueue.v1";
// Back off on network errors (2, 4, 8 s, capped at 10 s) and give up after about a minute (§ 9.1).
const FIRST_BACKOFF_MS = 2000;
const MAX_BACKOFF_MS = 10_000;
const GIVE_UP_AFTER_MS = 60_000;

// Module-scoped sessionStorage store for `useSyncExternalStore`:
// - SSR / initial hydration always gets EMPTY_QUEUE (zero hydration mismatch).
// - Every write immediately updates sessionStorage, even if a component unmounts mid-batch.
const EMPTY_QUEUE: ChannelQueueItem[] = [];
let cachedRawQueue: string | null | undefined;
let cachedParsedQueue: ChannelQueueItem[] = EMPTY_QUEUE;
const queueListeners = new Set<() => void>();

function readStoredQueueSnapshot(): ChannelQueueItem[] {
  if (typeof window === "undefined") return EMPTY_QUEUE;
  try {
    const raw = window.sessionStorage.getItem(QUEUE_STORAGE_KEY);
    if (raw === cachedRawQueue) return cachedParsedQueue;
    cachedRawQueue = raw;
    if (!raw) {
      cachedParsedQueue = EMPTY_QUEUE;
      return cachedParsedQueue;
    }
    const parsed = JSON.parse(raw) as unknown;
    cachedParsedQueue = Array.isArray(parsed)
      ? (parsed as ChannelQueueItem[])
      : EMPTY_QUEUE;
    return cachedParsedQueue;
  } catch {
    return EMPTY_QUEUE;
  }
}

function writeQueueSnapshot(
  updater: (prev: ChannelQueueItem[]) => ChannelQueueItem[],
): ChannelQueueItem[] {
  const prev = readStoredQueueSnapshot();
  const next = updater(prev);
  cachedParsedQueue = next;
  if (typeof window !== "undefined") {
    try {
      if (next.length === 0) {
        window.sessionStorage.removeItem(QUEUE_STORAGE_KEY);
        cachedRawQueue = null;
      } else {
        const serialized = JSON.stringify(next);
        window.sessionStorage.setItem(QUEUE_STORAGE_KEY, serialized);
        cachedRawQueue = serialized;
      }
    } catch {
      // Ignore storage errors (private browsing, quota).
    }
  }
  for (const listener of queueListeners) {
    listener();
  }
  return next;
}

function subscribeQueue(listener: () => void): () => void {
  queueListeners.add(listener);
  return () => {
    queueListeners.delete(listener);
  };
}

function getServerQueueSnapshot(): ChannelQueueItem[] {
  return EMPTY_QUEUE;
}

type LookupState =
  | { kind: "idle" }
  | { kind: "loading"; query: string }
  | { kind: "loaded"; data: ChannelVodsResponse }
  | {
      kind: "no_credentials";
      message: string;
      missing: string[];
      query: string;
    }
  | { kind: "not_found"; message: string; query: string }
  | { kind: "error"; message: string; query: string };

function ChannelVodsSkeleton({ label }: { label: string }) {
  return (
    <div className="flex flex-col gap-5">
      <div role="status" aria-live="polite" className="sr-only">
        {label}
      </div>
      <div aria-busy="true" aria-label={label} className="flex flex-col gap-5">
        <div className="flex flex-wrap items-center justify-between gap-4 rounded-panel border border-border-subtle bg-surface-panel p-5 shadow-card">
          <div className="flex items-center gap-3.5">
            <Skeleton className="size-12 shrink-0 rounded-pill" />
            <div className="flex flex-col gap-2">
              <Skeleton className="h-5 w-36" />
              <Skeleton className="h-3.5 w-48 max-w-full" />
            </div>
          </div>
          <Skeleton className="h-9 w-24" />
        </div>

        <ul
          role="list"
          className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3"
        >
          {Array.from({ length: 6 }, (_, index) => (
            <li
              key={index}
              aria-hidden="true"
              className="flex flex-col overflow-hidden rounded-card border border-border-subtle bg-surface-panel shadow-card"
            >
              <div className="aspect-video w-full bg-surface-media">
                <div className="size-full bg-white/5 motion-safe:animate-pulse" />
              </div>
              <div className="flex flex-1 flex-col gap-3 p-4">
                <Skeleton className="h-4 w-full" />
                <Skeleton className="h-4 w-3/4" />
                <Skeleton className="h-3.5 w-1/2" />
                <div className="mt-auto flex items-center justify-between pt-1">
                  <Skeleton className="h-4 w-24" />
                  <Skeleton className="h-4 w-16" />
                </div>
              </div>
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}

function VodThumbnail({
  vod,
  channelName,
}: {
  vod: ChannelVod;
  channelName: string;
}) {
  const [failed, setFailed] = useState(false);

  if (vod.thumbnail_url && !failed) {
    return (
      /* eslint-disable-next-line @next/next/no-img-element */
      <img
        src={vod.thumbnail_url}
        alt={`Thumbnail for ${vod.title}`}
        loading="lazy"
        decoding="async"
        onError={() => setFailed(true)}
        className="size-full object-cover"
      />
    );
  }

  return (
    <div
      aria-hidden="true"
      className="relative flex size-full flex-col items-center justify-center gap-2 overflow-hidden bg-gradient-to-br from-surface-media via-[#161926] to-[#231942] px-4 text-center"
    >
      <div className="flex size-10 items-center justify-center rounded-pill border border-white/15 bg-white/10 text-white/85">
        <svg viewBox="0 0 24 24" fill="currentColor" className="size-4">
          <path d="M8 6.82v10.36c0 .79.87 1.27 1.54.84l8.14-5.18a1 1 0 0 0 0-1.68L9.54 5.98A1 1 0 0 0 8 6.82z" />
        </svg>
      </div>
      <span className="max-w-full truncate font-mono text-[11px] text-white/70">
        {channelName} · VOD {vod.video_id}
      </span>
    </div>
  );
}

export function ChannelBrowser() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const inputId = useId();
  const inputRef = useRef<HTMLInputElement | null>(null);
  const requestSeq = useRef(0);
  const autoLoadedParam = useRef<string | null>(null);
  const validatedRestoredIds = useRef<Set<string>>(new Set());

  const initialChannelParam = searchParams.get("channel") ?? "";
  const [channelInput, setChannelInput] = useState(initialChannelParam);
  const [inputError, setInputError] = useState<string | null>(null);
  const [lookup, setLookup] = useState<LookupState>({ kind: "idle" });
  const [selectedIds, setSelectedIds] = useState<Set<string>>(() => new Set());
  const queueItems = useSyncExternalStore(
    subscribeQueue,
    readStoredQueueSnapshot,
    getServerQueueSnapshot,
  );
  const [queuing, setQueuing] = useState(false);
  const [queueError, setQueueError] = useState<string | null>(null);
  const [pollOffline, setPollOffline] = useState(false);
  const [pollAttempt, setPollAttempt] = useState(0);

  const parsedVodId = parseVod(channelInput);

  // Revalidate restored finished jobs once after mount so stale IDs from before a server
  // restart are marked expired instead of linking to a 404 results page (§ 9.3).
  useEffect(() => {
    const toCheck = queueItems.filter(
      (item) =>
        !item.missing &&
        item.job?.state === "done" &&
        !validatedRestoredIds.current.has(item.jobId),
    );
    if (toCheck.length === 0) return;
    let cancelled = false;
    const client = getApiClient();

    for (const item of toCheck) {
      validatedRestoredIds.current.add(item.jobId);
    }

    void Promise.all(
      toCheck.map(async (item) => {
        try {
          const fresh = await client.getJob(item.jobId);
          return { jobId: item.jobId, job: fresh, missing: false };
        } catch (error) {
          if (error instanceof ApiRequestError && error.status === 404) {
            return { jobId: item.jobId, missing: true };
          }
          return null;
        }
      }),
    ).then((results) => {
      if (cancelled) return;
      const patches = new Map(
        results
          .filter((entry): entry is NonNullable<typeof entry> => entry !== null)
          .map((entry) => [entry.jobId, entry]),
      );
      if (patches.size === 0) return;
      writeQueueSnapshot((prev) =>
        prev.map((item) => {
          const patch = patches.get(item.jobId);
          return patch ? { ...item, ...patch } : item;
        }),
      );
    });

    return () => {
      cancelled = true;
    };
  }, [queueItems]);

  const loadChannel = useCallback(
    async (
      rawChannel: string,
      { syncUrl = true }: { syncUrl?: boolean } = {},
    ) => {
      const validation = channelInputMessage(rawChannel);
      if (validation !== null) {
        setInputError(validation);
        return;
      }
      setInputError(null);
      setQueueError(null);
      const seq = ++requestSeq.current;
      const trimmed = rawChannel.trim();
      const normalized = parseChannel(trimmed) ?? trimmed;
      setLookup({ kind: "loading", query: trimmed });
      setSelectedIds(new Set());

      if (syncUrl) {
        // Record the normalized URL param before router.replace so the searchParams effect
        // doesn't trigger a duplicate lookup for the same submission.
        autoLoadedParam.current = normalized;
        router.replace(`/channels?channel=${encodeURIComponent(normalized)}`, {
          scroll: false,
        });
      }

      try {
        const data = await getApiClient().getChannelVods(trimmed);
        if (requestSeq.current !== seq) return;
        setLookup({ kind: "loaded", data });
      } catch (error) {
        if (requestSeq.current !== seq) return;
        if (error instanceof ApiRequestError) {
          if (error.error.code === "invalid_channel") {
            setInputError(error.error.message);
            setLookup({ kind: "idle" });
            return;
          }
          if (error.error.code === "twitch_credentials_missing") {
            const rawMissing = error.error.details?.missing;
            const missing = Array.isArray(rawMissing)
              ? rawMissing.map(String)
              : ["TWITCH_CLIENT_ID", "TWITCH_CLIENT_SECRET"];
            setLookup({
              kind: "no_credentials",
              message: error.error.message,
              missing,
              query: trimmed,
            });
            return;
          }
          if (error.error.code === "channel_not_found") {
            setLookup({
              kind: "not_found",
              message: error.error.message,
              query: trimmed,
            });
            return;
          }
          setLookup({
            kind: "error",
            message: error.error.message,
            query: trimmed,
          });
          return;
        }
        setLookup({
          kind: "error",
          message:
            "Can't reach the server. Check that AutoClipper is running and try again.",
          query: trimmed,
        });
      }
    },
    [router],
  );

  // Auto-load when arriving via `/channels?channel=...` (skipping URL changes made by submit).
  useEffect(() => {
    const param = searchParams.get("channel")?.trim() ?? "";
    if (!param) return;
    const normalized = parseChannel(param) ?? param;
    if (
      autoLoadedParam.current === param ||
      autoLoadedParam.current === normalized
    ) {
      return;
    }
    autoLoadedParam.current = normalized;
    setChannelInput(param);
    void loadChannel(param, { syncUrl: false });
  }, [searchParams, loadChannel]);

  // Poll any active queued/running jobs in the queue view, with § 9.1 exponential backoff
  // and an offline + manual retry state after about a minute of unreachable failures.
  const activeJobIdsKey = queueItems
    .filter(
      (item) =>
        !item.missing &&
        (item.job === null ||
          item.job.state === "queued" ||
          item.job.state === "running"),
    )
    .map((item) => item.jobId)
    .join(",");

  useEffect(() => {
    if (!activeJobIdsKey) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let failingSince: number | null = null;
    let backoff = FIRST_BACKOFF_MS;
    const activeIds = activeJobIdsKey.split(",");

    async function pollOnce() {
      const client = getApiClient();
      const updates = new Map<
        string,
        {
          job?: ChannelQueueItem["job"];
          missing?: boolean;
          pollError?: string | null;
        }
      >();
      const newlyDone = new Map<string, string>();
      let anyReachable = false;
      let anyTransportFailure = false;

      await Promise.all(
        activeIds.map(async (jobId) => {
          try {
            const job = await client.getJob(jobId);
            anyReachable = true;
            updates.set(jobId, { job, missing: false, pollError: null });
            if (job.state === "done") {
              validatedRestoredIds.current.add(job.job_id);
              newlyDone.set(job.video_id, job.job_id);
            }
          } catch (error) {
            if (error instanceof ApiRequestError && error.status === 404) {
              anyReachable = true;
              updates.set(jobId, { missing: true, pollError: null });
            } else {
              anyTransportFailure = true;
              updates.set(jobId, {
                pollError:
                  "Temporarily lost connection while checking progress; retrying…",
              });
            }
          }
        }),
      );

      if (cancelled) return;

      writeQueueSnapshot((prev) =>
        prev.map((item) => {
          const patch = updates.get(item.jobId);
          return patch ? { ...item, ...patch } : item;
        }),
      );

      if (newlyDone.size > 0) {
        setLookup((prev) => {
          if (prev.kind !== "loaded") return prev;
          return {
            kind: "loaded",
            data: {
              ...prev.data,
              vods: prev.data.vods.map((vod) => {
                const jobId = newlyDone.get(vod.video_id);
                return jobId
                  ? { ...vod, analyzed: true, last_job_id: jobId }
                  : vod;
              }),
            },
          };
        });
      }

      const stillActive = activeIds.some((id) => {
        const patch = updates.get(id);
        if (!patch) return true;
        if (patch.missing) return false;
        if (!patch.job) return true;
        return patch.job.state === "queued" || patch.job.state === "running";
      });

      if (!stillActive) {
        setPollOffline(false);
        return;
      }

      if (anyTransportFailure && !anyReachable) {
        failingSince ??= Date.now();
        if (Date.now() - failingSince > GIVE_UP_AFTER_MS) {
          setPollOffline(true);
          return;
        }
        const delay = backoff;
        backoff = Math.min(backoff * 2, MAX_BACKOFF_MS);
        timer = setTimeout(() => {
          void pollOnce();
        }, delay);
        return;
      }

      failingSince = null;
      backoff = FIRST_BACKOFF_MS;
      setPollOffline(false);
      timer = setTimeout(() => {
        void pollOnce();
      }, POLL_INTERVAL_MS);
    }

    timer = setTimeout(() => {
      void pollOnce();
    }, POLL_INTERVAL_MS);

    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [activeJobIdsKey, pollAttempt]);

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    void loadChannel(channelInput);
  }

  function toggleVod(videoId: string) {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(videoId)) {
        next.delete(videoId);
      } else {
        next.add(videoId);
      }
      return next;
    });
  }

  // Latest queue item per video_id (for card badges and inline links).
  const queueByVideoId = new Map<string, ChannelQueueItem>();
  for (const item of queueItems) {
    if (!queueByVideoId.has(item.videoId)) {
      queueByVideoId.set(item.videoId, item);
    }
  }

  function selectUnanalyzed() {
    if (lookup.kind !== "loaded") return;
    const next = new Set<string>();
    for (const vod of lookup.data.vods) {
      const queued = queueByVideoId.get(vod.video_id);
      const isInFlight =
        queued !== undefined &&
        !queued.missing &&
        (queued.job === null ||
          queued.job.state === "queued" ||
          queued.job.state === "running");
      if (!vod.analyzed && !isInFlight) {
        next.add(vod.video_id);
      }
    }
    setSelectedIds(next);
  }

  function selectAll() {
    if (lookup.kind !== "loaded") return;
    setSelectedIds(new Set(lookup.data.vods.map((vod) => vod.video_id)));
  }

  function clearSelection() {
    setSelectedIds(new Set());
  }

  async function queueVods(
    vods: Array<{
      video_id: string;
      title: string;
      duration_s: number;
      url: string;
      channelDisplayName: string;
    }>,
  ): Promise<{ succeededIds: Set<string>; failedIds: Set<string> }> {
    const succeededIds = new Set<string>();
    const failedIds = new Set<string>();
    const failedTitles: string[] = [];
    if (vods.length === 0) return { succeededIds, failedIds };

    setQueuing(true);
    setQueueError(null);
    setPollOffline(false);
    const client = getApiClient();

    let batchInserted = 0;

    for (const vod of vods) {
      try {
        const { job } = await client.createJob({
          vod: vod.url,
          video_path: null,
        });
        succeededIds.add(vod.video_id);
        if (job.state === "done") {
          validatedRestoredIds.current.add(job.job_id);
        }
        const entry: ChannelQueueItem = {
          jobId: job.job_id,
          videoId: vod.video_id,
          title: vod.title,
          channelDisplayName: vod.channelDisplayName,
          durationS: vod.duration_s,
          job,
          missing: false,
          pollError: null,
        };
        const insertAt = batchInserted;
        batchInserted += 1;
        // Persist every accepted job immediately so a reload or navigation mid-batch never
        // drops jobs the server has already queued (§ 9.3), preserving FIFO order within the batch.
        writeQueueSnapshot((prev) => {
          const withoutDup = prev.filter((item) => item.jobId !== entry.jobId);
          const pos = Math.min(insertAt, withoutDup.length);
          return [...withoutDup.slice(0, pos), entry, ...withoutDup.slice(pos)];
        });
      } catch {
        failedIds.add(vod.video_id);
        failedTitles.push(vod.title);
      }
    }

    if (failedTitles.length > 0) {
      const names = failedTitles.join(", ");
      setQueueError(
        `Couldn't queue ${failedTitles.length} ${
          failedTitles.length === 1 ? "VOD" : "VODs"
        } (${names}). ${
          succeededIds.size > 0
            ? "Failed VODs stay selected so you can retry only those."
            : "Check that the server is running and try again."
        }`,
      );
    }

    setQueuing(false);
    return { succeededIds, failedIds };
  }

  async function handleAnalyzeSelected() {
    if (lookup.kind !== "loaded" || selectedIds.size === 0) return;
    const chosen = lookup.data.vods
      .filter((vod) => selectedIds.has(vod.video_id))
      .map((vod) => ({
        video_id: vod.video_id,
        title: vod.title,
        duration_s: vod.duration_s,
        url: vod.url,
        channelDisplayName: lookup.data.channel.display_name,
      }));
    const { failedIds } = await queueVods(chosen);
    // Keep only failed submissions selected so retrying does not re-analyze accepted VODs.
    setSelectedIds(failedIds);
  }

  async function handleRetryQueueItem(item: ChannelQueueItem) {
    await queueVods([
      {
        video_id: item.videoId,
        title: item.title,
        duration_s: item.durationS,
        url: `https://www.twitch.tv/videos/${item.videoId}`,
        channelDisplayName: item.channelDisplayName,
      },
    ]);
  }

  function handleClearFinished() {
    writeQueueSnapshot((prev) =>
      prev.filter(
        (item) =>
          !item.missing &&
          (item.job === null ||
            item.job.state === "queued" ||
            item.job.state === "running"),
      ),
    );
  }

  function handleRetryPoll() {
    setPollOffline(false);
    setPollAttempt((n) => n + 1);
  }

  const parsedLogin = parseChannel(channelInput);
  const selectedCount = selectedIds.size;

  return (
    <div className="flex flex-col gap-8">
      <section className="flex flex-col gap-3">
        <p className="text-xs font-semibold tracking-[0.14em] text-accent uppercase">
          Channel browser
        </p>
        <h1 className="font-display text-3xl font-bold tracking-tight text-balance text-ink-primary sm:text-4xl">
          Analyze a channel&apos;s recent streams.
        </h1>
        <p className="max-w-2xl text-base/7 text-pretty text-ink-secondary">
          Paste a Twitch channel link or login to browse its recent past
          broadcasts, pick the streams you want, and queue them all at once.
          Jobs run one at a time in order.
        </p>
      </section>

      <form
        onSubmit={handleSubmit}
        noValidate
        aria-labelledby="channel-lookup-heading"
        className="flex flex-col gap-4 rounded-panel border border-border-subtle bg-surface-panel p-5 shadow-panel sm:p-6"
      >
        <div className="flex flex-col gap-1">
          <h2
            id="channel-lookup-heading"
            className="font-display text-base font-semibold text-ink-primary"
          >
            Look up a Twitch channel
          </h2>
          <p className="text-sm text-ink-secondary">
            Paste a channel URL (like{" "}
            <code className="font-mono text-xs">twitch.tv/user_001</code>) or
            just the login name.
          </p>
        </div>

        <div className="flex flex-col gap-2">
          <label
            htmlFor={inputId}
            className="text-sm font-semibold text-ink-primary"
          >
            Twitch channel link or login
          </label>
          <div className="flex flex-col gap-2.5 sm:flex-row">
            <input
              ref={inputRef}
              id={inputId}
              name="channel"
              type="text"
              autoComplete="off"
              spellCheck={false}
              placeholder="https://www.twitch.tv/user_001 or user_001"
              value={channelInput}
              onChange={(event) => {
                setChannelInput(event.target.value);
                setInputError(null);
              }}
              aria-invalid={inputError !== null}
              aria-describedby={
                inputError
                  ? `${inputId}-error`
                  : `${inputId}-hint ${inputId}-examples`
              }
              className="min-w-0 flex-1 rounded-control border border-border-strong bg-surface-elevated px-4 py-3 text-base text-ink-primary transition-colors placeholder:text-ink-tertiary focus-visible:border-accent focus-visible:ring-2 focus-visible:ring-focus-ring focus-visible:outline-none aria-invalid:border-danger"
            />
            <Button
              type="submit"
              variant="primary"
              size="lg"
              disabled={lookup.kind === "loading"}
              className="shrink-0 sm:px-6"
            >
              {lookup.kind === "loading" ? "Loading…" : "Load VODs"}
            </Button>
          </div>

          {inputError ? (
            <div className="flex flex-col gap-1">
              <p
                id={`${inputId}-error`}
                role="alert"
                className="text-sm font-medium text-danger"
              >
                {inputError}
              </p>
              {parsedVodId && (
                <p className="text-sm text-ink-secondary">
                  Have a single VOD?{" "}
                  <Link
                    href="/"
                    className="font-semibold text-accent underline underline-offset-2 hover:text-accent-hover"
                  >
                    Analyze a single VOD on the home page
                  </Link>
                </p>
              )}
            </div>
          ) : (
            <p id={`${inputId}-hint`} className="text-sm text-ink-secondary">
              {parsedLogin
                ? `Ready to load recent past broadcasts for @${parsedLogin}.`
                : "Accepts twitch.tv/<name>, twitch.tv/<name>/videos, @<name>, or a channel login."}
            </p>
          )}

          <div className="flex flex-wrap items-center gap-2 pt-1">
            <span
              id={`${inputId}-examples`}
              className="text-xs font-medium text-ink-tertiary"
            >
              Try a format:
            </span>
            {CHANNEL_FORMAT_EXAMPLES.map((example) => (
              <button
                key={example.label}
                type="button"
                onClick={() => {
                  setChannelInput(example.value);
                  setInputError(null);
                }}
                className="rounded-pill border border-border-subtle bg-surface-elevated px-3 py-1 text-xs font-medium text-ink-secondary transition-colors hover:border-accent-border hover:text-ink-primary focus-visible:ring-2 focus-visible:ring-focus-ring focus-visible:outline-none"
              >
                {example.label}
              </button>
            ))}
          </div>
        </div>
      </form>

      {queueError && (
        <p
          role="alert"
          className="rounded-control border border-danger-border bg-danger-soft px-4 py-3 text-sm font-medium text-danger-soft-ink"
        >
          {queueError}
        </p>
      )}

      <ChannelQueue
        items={queueItems}
        pollOffline={pollOffline}
        onRetryPoll={handleRetryPoll}
        onRetry={(item) => {
          void handleRetryQueueItem(item);
        }}
        onClearFinished={handleClearFinished}
      />

      {lookup.kind === "idle" && (
        <EmptyState
          title="No channel loaded yet"
          description="Paste a Twitch channel URL or login above to see its recent past broadcasts, check which ones are already analyzed, and queue several at once."
          action={
            <Button
              variant="secondary"
              size="md"
              onClick={() => {
                inputRef.current?.focus();
              }}
            >
              Enter a channel
            </Button>
          }
          secondaryAction={
            <ButtonLink href="/" variant="ghost" size="md">
              Analyze a single VOD instead
            </ButtonLink>
          }
        />
      )}

      {lookup.kind === "loading" && (
        <ChannelVodsSkeleton
          label={`Loading past broadcasts for ${lookup.query}…`}
        />
      )}

      {lookup.kind === "no_credentials" && (
        <ErrorState
          tone="signal"
          title="Twitch API credentials needed"
          message={lookup.message}
          details={`Missing in .env: ${lookup.missing.join(", ")} — add your Twitch app Client ID and Secret, restart autoclipper serve, then try again.`}
          primaryAction={
            <Button
              variant="primary"
              size="md"
              onClick={() => {
                void loadChannel(lookup.query, { syncUrl: false });
              }}
            >
              Try again
            </Button>
          }
          secondaryAction={
            <ButtonLink href="/" variant="secondary" size="md">
              Analyze a single VOD
            </ButtonLink>
          }
        />
      )}

      {lookup.kind === "not_found" && (
        <ErrorState
          tone="signal"
          title="Channel not found"
          message={lookup.message}
          primaryAction={
            <Button
              variant="primary"
              size="md"
              onClick={() => {
                inputRef.current?.focus();
                inputRef.current?.select();
              }}
            >
              Try another channel
            </Button>
          }
        />
      )}

      {lookup.kind === "error" && (
        <ErrorState
          tone="danger"
          alert
          title="Couldn't load channel VODs"
          message={lookup.message}
          primaryAction={
            <Button
              variant="primary"
              size="md"
              onClick={() => {
                void loadChannel(lookup.query, { syncUrl: false });
              }}
            >
              Try again
            </Button>
          }
        />
      )}

      {lookup.kind === "loaded" && (
        <section
          aria-labelledby="channel-results-heading"
          className="flex flex-col gap-5"
        >
          <div className="flex flex-wrap items-center justify-between gap-4 rounded-panel border border-border-subtle bg-surface-panel p-5 shadow-card">
            <div className="flex min-w-0 items-center gap-3.5">
              {lookup.data.channel.profile_image_url ? (
                /* eslint-disable-next-line @next/next/no-img-element */
                <img
                  src={lookup.data.channel.profile_image_url}
                  alt={`${lookup.data.channel.display_name} avatar`}
                  className="size-12 shrink-0 rounded-pill border border-border-subtle object-cover"
                />
              ) : (
                <span
                  aria-hidden="true"
                  className="flex size-12 shrink-0 items-center justify-center rounded-pill border border-accent-border bg-accent-soft font-display text-lg font-bold text-accent-soft-ink uppercase"
                >
                  {lookup.data.channel.display_name.slice(0, 1) || "?"}
                </span>
              )}
              <div className="min-w-0">
                <h2
                  id="channel-results-heading"
                  className="truncate font-display text-lg font-bold text-ink-primary sm:text-xl"
                >
                  {lookup.data.channel.display_name}
                </h2>
                <p className="truncate text-sm text-ink-secondary">
                  @{lookup.data.channel.login} ·{" "}
                  {lookup.data.vods.length === 0
                    ? "No past broadcasts"
                    : lookup.data.truncated
                      ? `Showing ${lookup.data.vods.length} most recent broadcasts`
                      : `${lookup.data.vods.length} recent ${
                          lookup.data.vods.length === 1
                            ? "broadcast"
                            : "broadcasts"
                        }`}
                </p>
              </div>
            </div>

            <Button
              variant="subtle"
              size="sm"
              onClick={() => {
                void loadChannel(lookup.data.channel.login, { syncUrl: false });
              }}
            >
              Refresh
            </Button>
          </div>

          {lookup.data.vods.length === 0 ? (
            <EmptyState
              title={`No past broadcasts found for ${lookup.data.channel.display_name}`}
              description="This channel doesn't have any saved past broadcasts on Twitch right now, or its VOD archives are turned off."
              action={
                <Button
                  variant="secondary"
                  size="md"
                  onClick={() => {
                    inputRef.current?.focus();
                    inputRef.current?.select();
                  }}
                >
                  Try another channel
                </Button>
              }
            />
          ) : (
            <>
              <div className="flex flex-wrap items-center justify-between gap-3 rounded-card border border-border-subtle bg-surface-panel px-4 py-3 shadow-card">
                <div className="flex flex-wrap items-center gap-2">
                  <Button variant="subtle" size="sm" onClick={selectUnanalyzed}>
                    Select unanalyzed
                  </Button>
                  <Button variant="subtle" size="sm" onClick={selectAll}>
                    Select all
                  </Button>
                  {selectedCount > 0 && (
                    <Button variant="ghost" size="sm" onClick={clearSelection}>
                      Clear
                    </Button>
                  )}
                </div>

                <div className="flex flex-wrap items-center gap-3">
                  <span
                    className="font-mono text-xs font-medium text-ink-secondary"
                    aria-live="polite"
                  >
                    {selectedCount} selected
                  </span>
                  <Button
                    variant="primary"
                    size="md"
                    disabled={selectedCount === 0 || queuing}
                    onClick={() => {
                      void handleAnalyzeSelected();
                    }}
                  >
                    {queuing
                      ? "Queuing…"
                      : selectedCount > 0
                        ? `Analyze all selected (${selectedCount})`
                        : "Analyze all selected"}
                  </Button>
                </div>
              </div>

              <ul
                role="list"
                className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3"
              >
                {lookup.data.vods.map((vod) => {
                  const selected = selectedIds.has(vod.video_id);
                  const queueItem = queueByVideoId.get(vod.video_id);
                  const queueState =
                    queueItem && !queueItem.missing
                      ? (queueItem.job?.state ?? "queued")
                      : null;
                  // Prefer the backend's current-session `last_job_id` so a freshly loaded
                  // server result supersedes any older job ID restored from sessionStorage.
                  const resultJobId =
                    vod.last_job_id ??
                    (queueState === "done" && queueItem
                      ? queueItem.jobId
                      : null);
                  const isAnalyzed = vod.analyzed || queueState === "done";
                  const checkboxId = `vod-select-${vod.video_id}`;

                  return (
                    <li
                      key={vod.video_id}
                      className={`flex flex-col overflow-hidden rounded-card border bg-surface-panel shadow-card transition-colors ${
                        selected
                          ? "border-accent ring-2 ring-accent/25"
                          : "border-border-subtle hover:border-border-strong"
                      }`}
                    >
                      <div className="relative aspect-video w-full overflow-hidden bg-surface-media">
                        <label
                          htmlFor={checkboxId}
                          className="block size-full cursor-pointer"
                        >
                          <VodThumbnail
                            vod={vod}
                            channelName={lookup.data.channel.display_name}
                          />
                        </label>

                        <label
                          htmlFor={checkboxId}
                          className="absolute top-2.5 left-2.5 flex min-h-8 min-w-8 cursor-pointer items-center justify-center rounded-control border border-white/25 bg-black/65 backdrop-blur-xs"
                        >
                          <input
                            id={checkboxId}
                            type="checkbox"
                            checked={selected}
                            onChange={() => toggleVod(vod.video_id)}
                            aria-label={`Select ${vod.title}`}
                            className="size-4 cursor-pointer accent-accent"
                          />
                        </label>

                        <div className="pointer-events-none absolute top-2.5 right-2.5 flex flex-wrap justify-end gap-1.5">
                          {queueState === "running" && (
                            <span className="inline-flex items-center gap-1 rounded-pill border border-accent-border bg-accent px-2.5 py-0.5 font-mono text-[11px] font-semibold text-accent-ink shadow-card">
                              Analyzing
                            </span>
                          )}
                          {queueState === "queued" && (
                            <span className="inline-flex items-center rounded-pill border border-white/20 bg-black/75 px-2.5 py-0.5 font-mono text-[11px] font-medium text-white">
                              Queued
                            </span>
                          )}
                          {queueState !== "running" &&
                            queueState !== "queued" &&
                            isAnalyzed && (
                              <span className="inline-flex items-center gap-1 rounded-pill border border-source-both-border bg-source-both-soft px-2.5 py-0.5 font-mono text-[11px] font-semibold text-source-both-soft-ink shadow-card">
                                Already analyzed
                              </span>
                            )}
                        </div>

                        <span className="pointer-events-none absolute right-2.5 bottom-2.5 rounded-control bg-black/80 px-2 py-0.5 font-mono text-xs font-medium text-white">
                          {formatTime(vod.duration_s)}
                        </span>
                      </div>

                      <div className="flex flex-1 flex-col gap-2.5 p-4">
                        <h3
                          title={vod.title}
                          className="line-clamp-2 text-sm leading-5 font-semibold text-ink-primary"
                        >
                          {vod.title}
                        </h3>

                        <p className="text-xs text-ink-secondary">
                          {formatBroadcastDate(vod.created_at)} ·{" "}
                          {formatViewCount(vod.view_count)}
                        </p>

                        <div className="mt-auto flex flex-wrap items-center justify-between gap-2 border-t border-border-subtle pt-3">
                          {resultJobId ? (
                            <Link
                              href={`/jobs/${resultJobId}/results`}
                              className="text-xs font-semibold text-accent hover:text-accent-hover hover:underline focus-visible:ring-2 focus-visible:ring-focus-ring focus-visible:outline-none"
                            >
                              View results &rarr;
                            </Link>
                          ) : queueItem &&
                            (queueState === "queued" ||
                              queueState === "running") ? (
                            <Link
                              href={`/jobs/${queueItem.jobId}`}
                              className="text-xs font-semibold text-accent hover:text-accent-hover hover:underline focus-visible:ring-2 focus-visible:ring-focus-ring focus-visible:outline-none"
                            >
                              View progress &rarr;
                            </Link>
                          ) : isAnalyzed ? (
                            <span className="text-xs text-ink-tertiary">
                              Saved on disk
                            </span>
                          ) : (
                            <span className="font-mono text-xs text-ink-tertiary">
                              VOD {vod.video_id}
                            </span>
                          )}

                          <a
                            href={vod.url}
                            target="_blank"
                            rel="noopener noreferrer"
                            className="text-xs font-medium text-ink-secondary transition-colors hover:text-ink-primary focus-visible:ring-2 focus-visible:ring-focus-ring focus-visible:outline-none"
                          >
                            Twitch ↗
                          </a>
                        </div>
                      </div>
                    </li>
                  );
                })}
              </ul>
            </>
          )}
        </section>
      )}
    </div>
  );
}
