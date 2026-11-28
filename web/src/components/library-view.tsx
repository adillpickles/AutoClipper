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
import type { JobSummary, ListJobsParams, State } from "@/lib/api/types";
import { formatDateTime, formatTime, localFileName } from "@/lib/format";

const PAGE_SIZE = 20;
const MAX_PAGE_SIZE = 100;
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
  return hasActiveRender(job);
}

function hasActiveRender(job: JobSummary): boolean {
  return (
    job.exports.some((e) => e.state === "queued" || e.state === "running") ||
    job.highlights.some((h) => h.state === "queued" || h.state === "running")
  );
}

/** A render in progress blocks deleting its job; queued ones are cancelled with it (#187). */
function hasRunningRender(job: JobSummary): boolean {
  return (
    job.exports.some((e) => e.state === "running") ||
    job.highlights.some((h) => h.state === "running")
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

type JobList = {
  items: JobSummary[];
  total: number;
  nextCursor: string | null;
};

type DeleteState = {
  deleteExports: boolean;
  pending: boolean;
  error: string | null;
};

function listFilters(query: string, state: StateFilter) {
  return { q: query || undefined, state: state === "all" ? undefined : state };
}

function matchesFilters(job: JobSummary, { q, state }: ListJobsParams) {
  if (state !== undefined && job.state !== state) return false;
  const searchable = [job.title, job.broadcaster_display_name, job.video_id]
    .filter(Boolean)
    .join(" ")
    .toLowerCase();
  return !q || searchable.includes(q.toLowerCase());
}

async function loadPages(
  params: ListJobsParams,
  count: number,
): Promise<JobList> {
  const items: JobSummary[] = [];
  let total: number;
  let nextCursor: string | null = null;
  do {
    const page = await getApiClient().listJobs({
      ...params,
      limit: Math.min(MAX_PAGE_SIZE, count - items.length),
      before: nextCursor ?? undefined,
    });
    items.push(...page.items);
    total = page.total;
    nextCursor = page.next_cursor;
  } while (nextCursor && items.length < count);
  return { items, total, nextCursor };
}

export function LibraryView() {
  const searchInputId = useId();
  const [searchInput, setSearchInput] = useState("");
  const [request, setRequest] = useState({
    query: "",
    filter: "all" as StateFilter,
    generation: 0,
    count: PAGE_SIZE,
  });
  const {
    query: appliedQuery,
    filter: stateFilter,
    generation,
    count,
  } = request;
  const [view, setView] = useState<LoadView>({ kind: "loading" });
  const [list, setList] = useState<JobList & { generation: number }>({
    items: [],
    total: 0,
    nextCursor: null,
    generation: -1,
  });
  const { items, total, nextCursor } = list;
  const [loadingMore, setLoadingMore] = useState(false);
  const [loadMoreError, setLoadMoreError] = useState<string | null>(null);
  const [pollOffline, setPollOffline] = useState(false);
  const [pollAttempt, setPollAttempt] = useState(0);
  const [deletes, setDeletes] = useState<Record<string, DeleteState>>({});

  const requestRef = useRef(request);
  const listRef = useRef(list);
  const listRevision = useRef(0);

  const commitList = useCallback(
    (next: JobList, generation: number, membershipChanged = true) => {
      if (membershipChanged) listRevision.current += 1;
      listRef.current = { ...next, generation };
      setList(listRef.current);
    },
    [],
  );

  function triggerReload(
    query: string,
    filter: StateFilter,
    { showSkeleton = true, count = PAGE_SIZE } = {},
  ) {
    if (showSkeleton) setView({ kind: "loading" });
    setLoadingMore(false);
    setLoadMoreError(null);
    setPollOffline(false);
    requestRef.current = {
      query,
      filter,
      generation: requestRef.current.generation + 1,
      count,
    };
    setRequest(requestRef.current);
  }

  useEffect(() => {
    let cancelled = false;
    loadPages(listFilters(appliedQuery, stateFilter), count)
      .then((next) => {
        if (cancelled || requestRef.current.generation !== generation) return;
        commitList(next, generation);
        setView({ kind: "loaded" });
      })
      .catch((error: unknown) => {
        if (cancelled || requestRef.current.generation !== generation) return;
        const message =
          error instanceof ApiRequestError
            ? error.error.message
            : "Can't reach the server. Check that AutoClipper is running, then try again.";
        setView({ kind: "error", message });
      });

    return () => {
      cancelled = true;
    };
  }, [appliedQuery, stateFilter, generation, count, commitList]);

  const hasActive =
    view.kind === "loaded" &&
    list.generation === generation &&
    items.some(isJobOrRenderActive);

  useEffect(() => {
    if (!hasActive) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let failingSince: number | null = null;
    let backoff = FIRST_BACKOFF_MS;
    const params = listFilters(appliedQuery, stateFilter);

    function schedulePoll(delay = POLL_INTERVAL_MS) {
      timer = setTimeout(() => void pollOnce(), delay);
    }

    async function pollOnce() {
      if (cancelled || requestRef.current.generation !== generation) return;
      const current = listRef.current;
      const ids = current.items
        .filter(isJobOrRenderActive)
        .map((job) => job.job_id);
      if (ids.length === 0) return;
      const revision = listRevision.current;
      function isStale() {
        if (cancelled || requestRef.current.generation !== generation)
          return true;
        if (listRevision.current === revision) return false;
        schedulePoll();
        return true;
      }

      try {
        const updates = await getApiClient().getJobStatuses(ids);
        if (isStale()) return;
        const changedMembership =
          updates.length !== ids.length ||
          updates.some((job) => !matchesFilters(job, params));
        if (changedMembership) {
          // A tracked row disappeared or moved out of the current filter.
          const next = await loadPages(
            params,
            Math.max(PAGE_SIZE, current.items.length),
          );
          if (isStale()) return;
          commitList(next, generation);
        } else {
          const byId = new Map(updates.map((job) => [job.job_id, job]));
          commitList(
            {
              ...current,
              items: current.items.map((job) => byId.get(job.job_id) ?? job),
            },
            generation,
            false,
          );
        }

        failingSince = null;
        backoff = FIRST_BACKOFF_MS;
        setPollOffline(false);
        schedulePoll();
      } catch {
        if (isStale()) return;
        failingSince ??= Date.now();
        if (Date.now() - failingSince > GIVE_UP_AFTER_MS) {
          setPollOffline(true);
          return;
        }
        schedulePoll(backoff);
        backoff = Math.min(backoff * 2, MAX_BACKOFF_MS);
      }
    }

    schedulePoll();
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [
    hasActive,
    appliedQuery,
    stateFilter,
    generation,
    pollAttempt,
    commitList,
  ]);

  async function handleLoadMore() {
    if (loadingMore || !nextCursor || list.generation !== generation) return;
    setLoadingMore(true);
    setLoadMoreError(null);
    const params = listFilters(appliedQuery, stateFilter);
    try {
      while (requestRef.current.generation === generation) {
        const current = listRef.current;
        if (!current.nextCursor) return;
        const revision = listRevision.current;
        const page = await getApiClient().listJobs({
          ...params,
          limit: PAGE_SIZE,
          before: current.nextCursor,
        });
        if (requestRef.current.generation !== generation) return;
        if (listRevision.current !== revision) continue;

        if (page.total !== current.total) {
          // A different tab changed the matching jobs. Replace the loaded prefix
          // as well so deleted rows disappear and new rows sort into place.
          const refreshed = await loadPages(
            params,
            current.items.length + PAGE_SIZE,
          );
          if (requestRef.current.generation !== generation) return;
          if (listRevision.current !== revision) continue;
          commitList(refreshed, generation);
        } else {
          commitList(
            {
              items: [...listRef.current.items, ...page.items],
              total: page.total,
              nextCursor: page.next_cursor,
            },
            generation,
          );
        }
        return;
      }
    } catch (error) {
      if (requestRef.current.generation !== generation) return;
      setLoadMoreError(
        error instanceof ApiRequestError
          ? error.error.message
          : "Couldn't load more analyses. Try again.",
      );
    } finally {
      if (requestRef.current.generation === generation) setLoadingMore(false);
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

  function setDelete(jobId: string, state: DeleteState | null) {
    setDeletes((previous) => {
      const next = { ...previous };
      if (state) next[jobId] = state;
      else delete next[jobId];
      return next;
    });
  }

  async function confirmDelete(jobId: string, deleteExports: boolean) {
    setDelete(jobId, { deleteExports, pending: true, error: null });
    try {
      await getApiClient().deleteJob(jobId, { deleteExports });
      setDelete(jobId, null);

      const current = listRef.current;
      const latest = requestRef.current;
      const remaining = current.items.filter((job) => job.job_id !== jobId);
      const nextTotal = Math.max(0, current.total - 1);
      if (remaining.length !== current.items.length) {
        commitList(
          {
            items: remaining,
            total: nextTotal,
            nextCursor:
              nextTotal > remaining.length ? current.nextCursor : null,
          },
          current.generation,
        );
      }

      if (
        latest.generation !== generation ||
        (remaining.length === 0 && current.nextCursor)
      ) {
        triggerReload(latest.query, latest.filter, {
          showSkeleton: false,
          count: Math.max(
            PAGE_SIZE,
            current.generation === latest.generation
              ? current.items.length
              : latest.count,
          ),
        });
      }
    } catch (error) {
      const message =
        error instanceof ApiRequestError
          ? error.error.message
          : "Couldn't delete this analysis. Check that the server is running and try again.";
      setDelete(jobId, { deleteExports, pending: false, error: message });
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
              const deletion = deletes[job.job_id];
              const isDeleting = deletion?.pending ?? false;
              const renderFilesCount = job.export_count + job.highlights_count;
              const activeChildRender = hasRunningRender(job);
              const canDelete =
                (job.state === "done" || job.state === "failed") &&
                !activeChildRender;
              const fraction =
                job.state === "running" && job.progress?.stage_fraction != null
                  ? Math.round(job.progress.stage_fraction * 100)
                  : null;

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
                        !deletion && (
                          <Button
                            variant="subtle"
                            size="sm"
                            disabled={!canDelete}
                            title={
                              activeChildRender
                                ? "Wait for the clip or highlights render in progress to finish before deleting."
                                : undefined
                            }
                            onClick={() =>
                              setDelete(job.job_id, {
                                deleteExports: false,
                                pending: false,
                                error: null,
                              })
                            }
                          >
                            Delete
                          </Button>
                        )}
                    </div>
                  </div>

                  {deletion && (
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
                            checked={deletion.deleteExports}
                            disabled={isDeleting}
                            onChange={(event) =>
                              setDelete(job.job_id, {
                                ...deletion,
                                deleteExports: event.target.checked,
                              })
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

                      {deletion.error && (
                        <p role="alert" className="font-medium text-danger">
                          {deletion.error}
                        </p>
                      )}

                      <div className="flex flex-wrap items-center gap-2">
                        <Button
                          variant="danger"
                          size="sm"
                          disabled={isDeleting}
                          onClick={() => {
                            void confirmDelete(
                              job.job_id,
                              deletion.deleteExports,
                            );
                          }}
                        >
                          {isDeleting ? "Deleting…" : "Confirm delete"}
                        </Button>
                        <Button
                          variant="secondary"
                          size="sm"
                          disabled={isDeleting}
                          onClick={() => setDelete(job.job_id, null)}
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

          {nextCursor && (
            <div className="flex flex-wrap items-center justify-between gap-3 pt-1">
              <p className="font-mono text-xs text-ink-tertiary tabular-nums">
                Showing {items.length} of {total}
              </p>
              <Button
                variant="secondary"
                size="md"
                disabled={loadingMore || list.generation !== generation}
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
