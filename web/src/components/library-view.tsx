"use client";

import {
  useCallback,
  useEffect,
  useId,
  useRef,
  useState,
  type FormEvent,
} from "react";

import { Button, ButtonLink } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/empty-state";
import { ErrorState } from "@/components/ui/error-state";
import { Skeleton } from "@/components/ui/skeleton";
import {
  ApiRequestError,
  getApiClient,
  POLL_INTERVAL_MS,
} from "@/lib/api/client";
import type { JobSummary, State } from "@/lib/api/types";
import { formatDateTime, formatTime, localFileName } from "@/lib/format";

const PAGE_SIZE = 20;
const MAX_POLL_PAGE_SIZE = 100;
// Back off on network errors (2, 4, 8 s, capped at 10 s) and give up after about a minute (§ 9.1).
const FIRST_BACKOFF_MS = 2000;
const MAX_BACKOFF_MS = 10_000;
const GIVE_UP_AFTER_MS = 60_000;

type StateFilter = "all" | State;

const STATE_FILTERS: ReadonlyArray<{ value: StateFilter; label: string }> = [
  { value: "all", label: "All" },
  { value: "running", label: "Running" },
  { value: "queued", label: "Queued" },
  { value: "done", label: "Done" },
  { value: "failed", label: "Failed" },
];

type LoadView =
  { kind: "loading" } | { kind: "loaded" } | { kind: "error"; message: string };

function isJobOrRenderActive(job: JobSummary): boolean {
  if (job.state === "queued" || job.state === "running") return true;
  if (job.exports.some((e) => e.state === "queued" || e.state === "running")) {
    return true;
  }
  if (
    job.highlights.some((h) => h.state === "queued" || h.state === "running")
  ) {
    return true;
  }
  return false;
}

function hasActiveRender(job: JobSummary): boolean {
  return (
    job.exports.some((e) => e.state === "queued" || e.state === "running") ||
    job.highlights.some((h) => h.state === "queued" || h.state === "running")
  );
}

function jobDisplayTitle(job: JobSummary): string {
  if (job.title && job.title.trim() !== "") return job.title;
  const fileName = localFileName(job.video_path);
  if (fileName) return fileName;
  return `VOD ${job.video_id}`;
}

function stateBadge(job: JobSummary): {
  label: string;
  className: string;
  pulse?: boolean;
} {
  if (job.state === "done") {
    return {
      label: "Done",
      className:
        "border-source-both-border bg-source-both-soft text-source-both-soft-ink",
    };
  }
  if (job.state === "failed") {
    return {
      label: job.error?.code === "interrupted" ? "Interrupted" : "Failed",
      className: "border-danger-border bg-danger-soft text-danger-soft-ink",
    };
  }
  if (job.state === "running") {
    const label = job.progress
      ? `Step ${job.progress.stage_index} of ${job.progress.stage_count}`
      : "Running";
    return {
      label,
      className: "border-accent-border bg-accent-soft text-accent-soft-ink",
      pulse: true,
    };
  }
  return {
    label: "Queued",
    className: "border-border-subtle bg-surface-elevated text-ink-secondary",
  };
}

function LibrarySkeleton({ label = "Loading library…" }: { label?: string }) {
  return (
    <div className="flex flex-col gap-4">
      <div role="status" aria-live="polite" className="sr-only">
        {label}
      </div>
      <div aria-busy="true" aria-label={label} className="flex flex-col gap-3">
        {Array.from({ length: 4 }, (_, index) => (
          <div
            key={index}
            aria-hidden="true"
            className="flex flex-col gap-3 rounded-card border border-border-subtle bg-surface-panel p-5 shadow-card sm:flex-row sm:items-center sm:justify-between"
          >
            <div className="flex flex-1 flex-col gap-2">
              <div className="flex items-center gap-2">
                <Skeleton className="h-5 w-20 rounded-pill" />
                <Skeleton className="h-4 w-32" />
              </div>
              <Skeleton className="h-5 w-3/4 max-w-md" />
              <Skeleton className="h-4 w-1/2 max-w-xs" />
            </div>
            <div className="flex items-center gap-2">
              <Skeleton className="h-9 w-28" />
              <Skeleton className="h-9 w-20" />
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

export function LibraryView() {
  const searchInputId = useId();
  const [searchInput, setSearchInput] = useState("");
  const [appliedQuery, setAppliedQuery] = useState("");
  const [stateFilter, setStateFilter] = useState<StateFilter>("all");
  const [refreshNonce, setRefreshNonce] = useState(0);
  const [view, setView] = useState<LoadView>({ kind: "loading" });
  const [items, setItems] = useState<JobSummary[]>([]);
  const [total, setTotal] = useState(0);
  const [hasMore, setHasMore] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [loadMoreError, setLoadMoreError] = useState<string | null>(null);
  const [pollOffline, setPollOffline] = useState(false);
  const [pollAttempt, setPollAttempt] = useState(0);

  const [confirmDeleteId, setConfirmDeleteId] = useState<string | null>(null);
  const [deleteExportsChecked, setDeleteExportsChecked] = useState(false);
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [deleteError, setDeleteError] = useState<{
    jobId: string;
    message: string;
  } | null>(null);

  const requestSeq = useRef(0);

  const triggerReload = useCallback(
    (
      nextQuery: string,
      nextFilter: StateFilter,
      { showSkeleton = true } = {},
    ) => {
      if (showSkeleton) {
        setView({ kind: "loading" });
      }
      setLoadMoreError(null);
      setPollOffline(false);
      setAppliedQuery(nextQuery);
      setStateFilter(nextFilter);
      setRefreshNonce((n) => n + 1);
    },
    [],
  );

  useEffect(() => {
    let cancelled = false;
    const seq = ++requestSeq.current;

    getApiClient()
      .listJobs({
        limit: PAGE_SIZE,
        offset: 0,
        q: appliedQuery.trim() || undefined,
        state: stateFilter === "all" ? undefined : stateFilter,
      })
      .then((res) => {
        if (cancelled || requestSeq.current !== seq) return;
        setItems(res.items);
        setTotal(res.total);
        setHasMore(res.has_more);
        setView({ kind: "loaded" });
      })
      .catch((error: unknown) => {
        if (cancelled || requestSeq.current !== seq) return;
        const message =
          error instanceof ApiRequestError
            ? error.error.message
            : "Can't reach the server. Check that AutoClipper is running, then try again.";
        setView({ kind: "error", message });
      });

    return () => {
      cancelled = true;
    };
  }, [appliedQuery, stateFilter, refreshNonce]);

  const activeItemsCount = items.length;
  const hasActive = view.kind === "loaded" && items.some(isJobOrRenderActive);

  useEffect(() => {
    if (!hasActive) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let failingSince: number | null = null;
    let backoff = FIRST_BACKOFF_MS;
    const pollLimit = Math.min(
      MAX_POLL_PAGE_SIZE,
      Math.max(PAGE_SIZE, activeItemsCount),
    );

    async function pollOnce() {
      try {
        const res = await getApiClient().listJobs({
          limit: pollLimit,
          offset: 0,
          q: appliedQuery.trim() || undefined,
          state: stateFilter === "all" ? undefined : stateFilter,
        });
        if (cancelled) return;
        failingSince = null;
        backoff = FIRST_BACKOFF_MS;
        setPollOffline(false);
        setTotal(res.total);
        setItems((prev) => {
          if (prev.length <= pollLimit) {
            setHasMore(res.has_more);
            return res.items;
          }
          // Keep older pages already loaded beyond pollLimit while refreshing the newest window.
          const freshIds = new Set(res.items.map((item) => item.job_id));
          const tail = prev.filter((item) => !freshIds.has(item.job_id));
          const merged = [...res.items, ...tail];
          setHasMore(merged.length < res.total);
          return merged;
        });

        const stillActive = res.items.some(isJobOrRenderActive);
        if (stillActive) {
          timer = setTimeout(() => {
            void pollOnce();
          }, POLL_INTERVAL_MS);
        }
      } catch {
        if (cancelled) return;
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
      }
    }

    timer = setTimeout(() => {
      void pollOnce();
    }, POLL_INTERVAL_MS);

    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [hasActive, activeItemsCount, appliedQuery, stateFilter, pollAttempt]);

  async function handleLoadMore() {
    if (loadingMore || !hasMore) return;
    setLoadingMore(true);
    setLoadMoreError(null);
    const seq = requestSeq.current;
    try {
      const res = await getApiClient().listJobs({
        limit: PAGE_SIZE,
        offset: items.length,
        q: appliedQuery.trim() || undefined,
        state: stateFilter === "all" ? undefined : stateFilter,
      });
      if (requestSeq.current !== seq) return;
      setItems((prev) => {
        const seen = new Set(prev.map((item) => item.job_id));
        const appended = res.items.filter((item) => !seen.has(item.job_id));
        return [...prev, ...appended];
      });
      setTotal(res.total);
      setHasMore(res.has_more);
    } catch (error) {
      if (requestSeq.current !== seq) return;
      setLoadMoreError(
        error instanceof ApiRequestError
          ? error.error.message
          : "Couldn't load more analyses. Try again.",
      );
    } finally {
      setLoadingMore(false);
    }
  }

  function handleSearchSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    triggerReload(searchInput.trim(), stateFilter);
  }

  function handleClearFilters() {
    setSearchInput("");
    triggerReload("", "all");
  }

  function openDeleteConfirm(job: JobSummary) {
    setConfirmDeleteId(job.job_id);
    setDeleteExportsChecked(false);
    setDeleteError(null);
  }

  function cancelDeleteConfirm() {
    setConfirmDeleteId(null);
    setDeleteExportsChecked(false);
    setDeleteError(null);
  }

  async function confirmDelete(job: JobSummary) {
    setDeletingId(job.job_id);
    setDeleteError(null);
    try {
      await getApiClient().deleteJob(job.job_id, {
        deleteExports: deleteExportsChecked,
      });
      setConfirmDeleteId(null);
      setDeleteExportsChecked(false);
      const nextRemaining = items.filter((item) => item.job_id !== job.job_id);
      const nextTotal = Math.max(0, total - 1);
      setItems(nextRemaining);
      setTotal(nextTotal);
      if (nextRemaining.length === 0 && nextTotal > 0) {
        triggerReload(appliedQuery, stateFilter, { showSkeleton: false });
      }
    } catch (error) {
      const message =
        error instanceof ApiRequestError
          ? error.error.message
          : "Couldn't delete this analysis. Check that the server is running and try again.";
      setDeleteError({ jobId: job.job_id, message });
    } finally {
      setDeletingId(null);
    }
  }

  const hasFilters = appliedQuery !== "" || stateFilter !== "all";

  return (
    <div className="flex flex-col gap-6 sm:gap-8">
      <section className="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
        <div className="flex flex-col gap-2">
          <p className="text-xs font-semibold tracking-[0.14em] text-accent uppercase">
            Library
          </p>
          <h1 className="font-display text-3xl font-bold tracking-tight text-balance text-ink-primary sm:text-4xl">
            Past and running analyses
          </h1>
          <p className="max-w-2xl text-base/7 text-pretty text-ink-secondary">
            Reopen finished results, check running jobs, or remove old analyses
            from your local library.
          </p>
        </div>
        <div className="flex shrink-0 flex-wrap items-center gap-2">
          <ButtonLink href="/" variant="primary" size="md">
            Analyze a VOD
          </ButtonLink>
        </div>
      </section>

      <section
        aria-label="Filter library"
        className="flex flex-col gap-4 rounded-panel border border-border-subtle bg-surface-panel p-4 shadow-card sm:p-5"
      >
        <form
          onSubmit={handleSearchSubmit}
          className="flex flex-col gap-2.5 sm:flex-row"
        >
          <label htmlFor={searchInputId} className="sr-only">
            Search by VOD title, channel, or VOD ID
          </label>
          <input
            id={searchInputId}
            type="search"
            value={searchInput}
            onChange={(event) => setSearchInput(event.target.value)}
            placeholder="Search title, channel, or VOD ID"
            className="min-w-0 flex-1 rounded-control border border-border-strong bg-surface-elevated px-3.5 py-2.5 text-sm text-ink-primary transition-colors placeholder:text-ink-tertiary focus-visible:border-accent focus-visible:ring-2 focus-visible:ring-focus-ring focus-visible:outline-none"
          />
          <div className="flex items-center gap-2">
            <Button type="submit" variant="secondary" size="md">
              Search
            </Button>
            {hasFilters && (
              <Button
                type="button"
                variant="ghost"
                size="md"
                onClick={handleClearFilters}
              >
                Clear
              </Button>
            )}
          </div>
        </form>

        <div className="flex flex-wrap items-center justify-between gap-3">
          <div
            role="group"
            aria-label="Filter by status"
            className="flex flex-wrap items-center gap-1.5"
          >
            {STATE_FILTERS.map((option) => {
              const active = stateFilter === option.value;
              return (
                <button
                  key={option.value}
                  type="button"
                  aria-pressed={active}
                  onClick={() => triggerReload(appliedQuery, option.value)}
                  className={`inline-flex min-h-8 items-center rounded-pill border px-3 py-1 text-xs font-semibold transition-colors focus-visible:ring-2 focus-visible:ring-focus-ring focus-visible:outline-none ${
                    active
                      ? "border-accent bg-accent text-accent-ink"
                      : "border-border-subtle bg-surface-elevated text-ink-secondary hover:border-border-strong hover:text-ink-primary"
                  }`}
                >
                  {option.label}
                </button>
              );
            })}
          </div>

          <div className="flex items-center gap-3">
            {view.kind === "loaded" && (
              <p className="font-mono text-xs text-ink-tertiary tabular-nums">
                {total} {total === 1 ? "analysis" : "analyses"}
              </p>
            )}
            <Button
              variant="subtle"
              size="sm"
              disabled={view.kind === "loading"}
              onClick={() => triggerReload(appliedQuery, stateFilter)}
            >
              Refresh
            </Button>
          </div>
        </div>
      </section>

      {pollOffline && (
        <div
          role="alert"
          className="flex flex-wrap items-center justify-between gap-3 rounded-card border border-danger-border bg-danger-soft px-4 py-3 text-sm text-danger-soft-ink"
        >
          <p className="font-medium">
            Can&apos;t reach the server to refresh running jobs. Check that
            AutoClipper is running, then try again.
          </p>
          <Button
            variant="secondary"
            size="sm"
            onClick={() => {
              setPollOffline(false);
              setPollAttempt((n) => n + 1);
            }}
          >
            Retry
          </Button>
        </div>
      )}

      {view.kind === "loading" && <LibrarySkeleton />}

      {view.kind === "error" && (
        <ErrorState
          alert
          title="Couldn't load the library"
          message={view.message}
          primaryAction={
            <Button
              variant="primary"
              size="md"
              onClick={() => triggerReload(appliedQuery, stateFilter)}
            >
              Retry
            </Button>
          }
        />
      )}

      {view.kind === "loaded" && items.length === 0 && (
        <EmptyState
          title={hasFilters ? "No matching analyses" : "No analyses yet"}
          description={
            hasFilters
              ? "No saved or running jobs matched your search or status filter."
              : "Analyze a Twitch VOD or browse a channel's broadcasts to build your library."
          }
          action={
            hasFilters ? (
              <Button
                variant="secondary"
                size="md"
                onClick={handleClearFilters}
              >
                Clear filters
              </Button>
            ) : (
              <ButtonLink href="/" variant="primary" size="md">
                Analyze a VOD
              </ButtonLink>
            )
          }
          secondaryAction={
            hasFilters ? undefined : (
              <ButtonLink href="/channels" variant="secondary" size="md">
                Browse a channel
              </ButtonLink>
            )
          }
        />
      )}

      {view.kind === "loaded" && items.length > 0 && (
        <div className="flex flex-col gap-4">
          <ol
            role="list"
            aria-label="Analysis jobs"
            className="flex flex-col gap-3"
          >
            {items.map((job) => {
              const badge = stateBadge(job);
              const title = jobDisplayTitle(job);
              const fileName = localFileName(job.video_path);
              const isConfirming = confirmDeleteId === job.job_id;
              const isDeleting = deletingId === job.job_id;
              const renderFilesCount = job.export_count + job.highlights_count;
              const activeChildRender = hasActiveRender(job);
              const canDelete =
                (job.state === "done" || job.state === "failed") &&
                !activeChildRender;
              const fraction =
                job.state === "running" && job.progress?.stage_fraction != null
                  ? Math.round(job.progress.stage_fraction * 100)
                  : null;
              const rowDeleteError =
                deleteError?.jobId === job.job_id ? deleteError.message : null;

              return (
                <li
                  key={job.job_id}
                  className="flex flex-col gap-4 rounded-card border border-border-subtle bg-surface-panel p-4 shadow-card sm:p-5"
                >
                  <div className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
                    <div className="flex min-w-0 flex-1 flex-col gap-1.5">
                      <div className="flex flex-wrap items-center gap-2">
                        <span
                          className={`inline-flex shrink-0 items-center gap-1.5 rounded-pill border px-2.5 py-0.5 font-mono text-xs font-medium ${badge.className}`}
                        >
                          {badge.pulse && (
                            <span
                              aria-hidden="true"
                              className="size-1.5 rounded-full bg-accent motion-safe:animate-pulse"
                            />
                          )}
                          {badge.label}
                        </span>

                        {job.warning_count > 0 && job.state === "done" && (
                          <span className="inline-flex items-center rounded-pill border border-signal-border bg-signal-soft px-2.5 py-0.5 font-mono text-xs font-medium text-signal-soft-ink">
                            {job.warning_count}{" "}
                            {job.warning_count === 1 ? "warning" : "warnings"}
                          </span>
                        )}

                        <span className="font-mono text-xs text-ink-tertiary">
                          {formatDateTime(job.created_at)}
                        </span>
                      </div>

                      <h2 className="font-display text-base font-semibold break-words text-ink-primary sm:text-lg">
                        {title}
                      </h2>

                      {(job.broadcaster_display_name ||
                        job.title ||
                        fileName ||
                        job.duration_s !== null) && (
                        <p className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-ink-secondary">
                          {job.broadcaster_display_name && (
                            <span className="font-medium text-ink-primary">
                              {job.broadcaster_display_name}
                            </span>
                          )}
                          {(job.title || fileName) && (
                            <span className="font-mono text-ink-tertiary">
                              VOD {job.video_id}
                            </span>
                          )}
                          {fileName && job.title && (
                            <span className="font-mono break-all text-ink-tertiary">
                              · {fileName}
                            </span>
                          )}
                          {job.duration_s !== null && (
                            <span className="font-mono text-ink-tertiary">
                              · {formatTime(job.duration_s)}
                            </span>
                          )}
                        </p>
                      )}

                      {job.state === "queued" && (
                        <p className="text-xs text-ink-secondary">
                          Waiting to start — runs when the analysis worker is
                          free.
                        </p>
                      )}

                      {job.state === "running" && job.progress && (
                        <div className="mt-1 flex flex-col gap-1.5">
                          <div className="flex items-center justify-between gap-2 text-xs text-ink-secondary">
                            <span>{job.progress.message}</span>
                            {fraction !== null && (
                              <span className="font-mono tabular-nums">
                                {fraction}%
                              </span>
                            )}
                          </div>
                          {fraction !== null && (
                            <div
                              role="progressbar"
                              aria-label={`${title}: ${job.progress.message}`}
                              aria-valuemin={0}
                              aria-valuemax={100}
                              aria-valuenow={fraction}
                              className="h-1.5 w-full overflow-hidden rounded-pill bg-surface-subtle"
                            >
                              <div
                                className="h-full rounded-pill bg-accent transition-[width] duration-300"
                                style={{ width: `${fraction}%` }}
                              />
                            </div>
                          )}
                        </div>
                      )}

                      {job.state === "failed" && (
                        <p
                          role="alert"
                          className="text-xs font-medium text-danger"
                        >
                          {job.error?.message ??
                            "This analysis stopped before finishing."}
                        </p>
                      )}

                      {job.state === "done" && (
                        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 pt-0.5 text-xs text-ink-secondary">
                          {job.candidate_count !== null && (
                            <span>
                              <strong className="font-semibold text-ink-primary tabular-nums">
                                {job.candidate_count}
                              </strong>{" "}
                              {job.candidate_count === 1 ? "moment" : "moments"}
                            </span>
                          )}
                          {(job.export_count > 0 ||
                            job.missing_export_count > 0) && (
                            <span>
                              <strong className="font-semibold text-ink-primary tabular-nums">
                                {job.export_count}
                              </strong>{" "}
                              {job.export_count === 1
                                ? "exported clip"
                                : "exported clips"}
                              {job.missing_export_count > 0
                                ? ` (${job.missing_export_count} missing on disk)`
                                : ""}
                            </span>
                          )}
                          {(job.highlights_count > 0 ||
                            job.missing_highlights_count > 0) && (
                            <span>
                              <strong className="font-semibold text-ink-primary tabular-nums">
                                {job.highlights_count}
                              </strong>{" "}
                              {job.highlights_count === 1
                                ? "highlights video"
                                : "highlights videos"}
                              {job.missing_highlights_count > 0
                                ? ` (${job.missing_highlights_count} missing on disk)`
                                : ""}
                            </span>
                          )}
                          {!job.result_available && (
                            <span className="text-signal-soft-ink">
                              Saved result file is missing on disk.
                            </span>
                          )}
                        </div>
                      )}
                    </div>

                    <div className="flex shrink-0 flex-wrap items-center gap-2">
                      {job.state === "done" && job.result_available && (
                        <ButtonLink
                          href={`/jobs/${job.job_id}/results`}
                          variant="primary"
                          size="sm"
                        >
                          Open results
                        </ButtonLink>
                      )}
                      {job.state === "done" && !job.result_available && (
                        <ButtonLink
                          href={`/jobs/${job.job_id}`}
                          variant="secondary"
                          size="sm"
                        >
                          View status
                        </ButtonLink>
                      )}
                      {(job.state === "queued" || job.state === "running") && (
                        <ButtonLink
                          href={`/jobs/${job.job_id}`}
                          variant="secondary"
                          size="sm"
                        >
                          View progress
                        </ButtonLink>
                      )}
                      {job.state === "failed" && (
                        <ButtonLink
                          href={`/jobs/${job.job_id}`}
                          variant="secondary"
                          size="sm"
                        >
                          View details
                        </ButtonLink>
                      )}

                      {(job.state === "done" || job.state === "failed") &&
                        !isConfirming && (
                          <Button
                            variant="subtle"
                            size="sm"
                            disabled={!canDelete}
                            title={
                              activeChildRender
                                ? "Wait for active clip or highlights renders to finish before deleting."
                                : undefined
                            }
                            onClick={() => openDeleteConfirm(job)}
                          >
                            Delete
                          </Button>
                        )}
                    </div>
                  </div>

                  {isConfirming && (
                    <div
                      role="region"
                      aria-label={`Confirm deleting ${title}`}
                      className="flex flex-col gap-3 rounded-control border border-danger-border bg-danger-soft p-4 text-xs text-danger-soft-ink"
                    >
                      <p className="text-sm font-semibold">
                        Delete this analysis?
                      </p>
                      <p className="leading-5">
                        This removes the job from the library index and deletes
                        its saved analysis result JSON on disk.{" "}
                        {renderFilesCount === 0
                          ? "Any original local video file you provided is never deleted."
                          : fileName
                            ? `Your original local video file (${fileName}) is never deleted.`
                            : "Any original local video file is never deleted."}
                      </p>

                      {renderFilesCount > 0 && (
                        <label className="flex cursor-pointer items-start gap-2.5 rounded-control border border-danger-border/70 bg-surface-panel px-3 py-2.5 text-xs text-ink-primary">
                          <input
                            type="checkbox"
                            checked={deleteExportsChecked}
                            disabled={isDeleting}
                            onChange={(event) =>
                              setDeleteExportsChecked(event.target.checked)
                            }
                            className="mt-0.5 size-4 shrink-0 rounded accent-danger"
                          />
                          <span className="leading-5">
                            Also delete {renderFilesCount} rendered{" "}
                            {renderFilesCount === 1 ? "MP4 file" : "MP4 files"}{" "}
                            from disk (
                            {[
                              job.export_count > 0
                                ? `${job.export_count} ${job.export_count === 1 ? "clip" : "clips"}`
                                : null,
                              job.highlights_count > 0
                                ? `${job.highlights_count} highlights`
                                : null,
                            ]
                              .filter(Boolean)
                              .join(", ")}
                            ). Leave unchecked to keep rendered MP4s on disk.
                          </span>
                        </label>
                      )}

                      {rowDeleteError && (
                        <p role="alert" className="font-medium text-danger">
                          {rowDeleteError}
                        </p>
                      )}

                      <div className="flex flex-wrap items-center gap-2">
                        <Button
                          variant="danger"
                          size="sm"
                          disabled={isDeleting}
                          onClick={() => {
                            void confirmDelete(job);
                          }}
                        >
                          {isDeleting ? "Deleting…" : "Confirm delete"}
                        </Button>
                        <Button
                          variant="secondary"
                          size="sm"
                          disabled={isDeleting}
                          onClick={cancelDeleteConfirm}
                        >
                          Cancel
                        </Button>
                      </div>
                    </div>
                  )}
                </li>
              );
            })}
          </ol>

          {loadMoreError && (
            <p
              role="alert"
              className="rounded-control border border-danger-border bg-danger-soft px-4 py-3 text-xs font-medium text-danger-soft-ink"
            >
              {loadMoreError}
            </p>
          )}

          {hasMore && (
            <div className="flex flex-wrap items-center justify-between gap-3 pt-1">
              <p className="font-mono text-xs text-ink-tertiary tabular-nums">
                Showing {items.length} of {total}
              </p>
              <Button
                variant="secondary"
                size="md"
                disabled={loadingMore}
                onClick={() => {
                  void handleLoadMore();
                }}
              >
                {loadingMore ? "Loading more…" : "Load more"}
              </Button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
