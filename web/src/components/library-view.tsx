"use client";

import {
  useCallback,
  useEffect,
  useId,
  useRef,
  useState,
  type FormEvent,
} from "react";

import { Badge, type BadgeTone } from "@/components/ui/badge";
import { Button, ButtonLink } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/empty-state";
import { ErrorState } from "@/components/ui/error-state";
import { Input } from "@/components/ui/input";
import { Notice } from "@/components/ui/notice";
import { PageHeader } from "@/components/ui/page-header";
import { ProgressBar } from "@/components/ui/progress";
import { Skeleton } from "@/components/ui/skeleton";
import {
  ApiRequestError,
  getApiClient,
  POLL_INTERVAL_MS,
} from "@/lib/api/client";
import { schedulePoll as pollAfter } from "@/lib/poll";
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
  tone: BadgeTone;
  live?: boolean;
  /** The row's left edge: a tally light for the job's state. */
  edge: string;
} {
  if (job.state === "done") {
    return { label: "Done", tone: "success", edge: "bg-success" };
  }
  if (job.state === "failed") {
    return {
      label: job.error?.code === "interrupted" ? "Interrupted" : "Failed",
      tone: "danger",
      edge: "bg-danger",
    };
  }
  if (job.state === "running") {
    const label = job.progress
      ? `Step ${job.progress.stage_index} of ${job.progress.stage_count}`
      : "Running";
    return { label, tone: "accent", live: true, edge: "bg-accent" };
  }
  return { label: "Queued", tone: "neutral", edge: "bg-border-strong" };
}

const STAT_CLASS =
  "inline-flex items-center gap-1 rounded-pill border border-border-subtle bg-surface-elevated px-2.5 py-0.5";

const ROW_CLASS =
  "relative flex flex-col gap-4 overflow-hidden rounded-card border border-border-subtle bg-surface-panel py-4 pr-4 pl-5 shadow-card transition-colors hover:border-border-strong sm:py-5 sm:pr-5 sm:pl-6";

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
            className={`${ROW_CLASS} sm:flex-row sm:items-center sm:justify-between`}
          >
            <span className="absolute inset-y-0 left-0 w-[3px] bg-border-subtle" />
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
    let timer: (() => void) | undefined;
    let failingSince: number | null = null;
    let backoff = FIRST_BACKOFF_MS;
    const params = listFilters(appliedQuery, stateFilter);

    function schedulePoll(delay = POLL_INTERVAL_MS) {
      timer = pollAfter(() => void pollOnce(), delay);
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
      timer?.();
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
      <PageHeader
        title="Library"
        description="Past and running analyses. Reopen finished results, check on running jobs, or remove old analyses from this computer."
        actions={
          <ButtonLink href="/" variant="primary" size="md">
            Analyze a VOD
          </ButtonLink>
        }
      />

      <section
        aria-label="Filter library"
        className="flex flex-col gap-3 rounded-panel border border-border-subtle bg-surface-panel p-3 shadow-panel sm:p-4"
      >
        <form
          onSubmit={handleSearchSubmit}
          className="flex flex-col gap-2 sm:flex-row"
        >
          <label htmlFor={searchInputId} className="sr-only">
            Search by VOD title, channel, or VOD ID
          </label>
          <div className="relative min-w-0 flex-1">
            <svg
              viewBox="0 0 20 20"
              fill="none"
              aria-hidden="true"
              className="pointer-events-none absolute top-1/2 left-3.5 size-4 -translate-y-1/2 text-ink-tertiary"
              stroke="currentColor"
              strokeWidth="1.8"
              strokeLinecap="round"
            >
              <circle cx="9" cy="9" r="5.5" />
              <path d="M13.2 13.2L17 17" />
            </svg>
            <Input
              id={searchInputId}
              type="search"
              value={searchInput}
              onChange={(event) => setSearchInput(event.target.value)}
              placeholder="Search title, channel, or VOD ID"
              className="pl-10"
            />
          </div>
          <div className="flex items-center gap-2">
            <Button
              type="submit"
              variant="secondary"
              size="md"
              className="min-h-11 flex-1 sm:flex-none"
            >
              Search
            </Button>
            {hasFilters && (
              <Button
                type="button"
                variant="ghost"
                size="md"
                className="min-h-11"
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
            className="-mx-1 flex max-w-full overflow-x-auto px-1 py-0.5"
          >
            <div className="inline-flex rounded-control border border-border-subtle bg-surface-canvas p-1">
              {STATE_FILTERS.map((option) => {
                const active = stateFilter === option.value;
                return (
                  <button
                    key={option.value}
                    type="button"
                    aria-pressed={active}
                    onClick={() => triggerReload(appliedQuery, option.value)}
                    className={`inline-flex min-h-8 items-center rounded-[0.375rem] px-3 text-xs font-semibold whitespace-nowrap transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus-ring ${
                      active
                        ? "bg-surface-subtle text-ink-primary shadow-card"
                        : "text-ink-tertiary hover:text-ink-primary"
                    }`}
                  >
                    {option.label}
                  </button>
                );
              })}
            </div>
          </div>

          <div className="flex items-center gap-3">
            {view.kind === "loaded" && (
              <p className="font-mono text-xs text-ink-tertiary tabular-nums">
                {total} {total === 1 ? "analysis" : "analyses"}
              </p>
            )}
            <Button
              variant="ghost"
              size="sm"
              disabled={view.kind === "loading"}
              onClick={() => triggerReload(appliedQuery, stateFilter)}
            >
              <svg
                viewBox="0 0 16 16"
                fill="none"
                aria-hidden="true"
                className="size-3.5"
                stroke="currentColor"
                strokeWidth="1.8"
                strokeLinecap="round"
                strokeLinejoin="round"
              >
                <path d="M13 8a5 5 0 1 1-1.46-3.54M13 2.5v2.75h-2.75" />
              </svg>
              Refresh
            </Button>
          </div>
        </div>
      </section>

      {pollOffline && (
        <Notice
          role="alert"
          tone="danger"
          action={
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
          }
        >
          Can&apos;t reach the server to refresh running jobs. Check that
          AutoClipper is running, then try again.
        </Notice>
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
                <li key={job.job_id} className={ROW_CLASS}>
                  <span
                    aria-hidden="true"
                    className={`absolute inset-y-0 left-0 w-[3px] ${badge.edge}`}
                  />
                  <div className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
                    <div className="flex min-w-0 flex-1 flex-col gap-1.5">
                      <div className="flex flex-wrap items-center gap-2">
                        <Badge
                          tone={badge.tone}
                          live={badge.live}
                          dot={!badge.live}
                          mono
                        >
                          {badge.label}
                        </Badge>

                        {job.warning_count > 0 && job.state === "done" && (
                          <Badge tone="signal" mono>
                            {job.warning_count}{" "}
                            {job.warning_count === 1 ? "warning" : "warnings"}
                          </Badge>
                        )}

                        <span className="font-mono text-xs text-ink-tertiary">
                          {formatDateTime(job.created_at)}
                        </span>
                      </div>

                      <h2 className="font-display text-base font-semibold tracking-[-0.01em] break-words text-ink-primary sm:text-lg">
                        {title}
                      </h2>

                      {(job.broadcaster_display_name ||
                        job.title ||
                        fileName ||
                        job.duration_s !== null) && (
                        <p className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-ink-secondary">
                          {job.broadcaster_display_name && (
                            <span className="font-semibold text-ink-primary">
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
                        <div className="mt-1 flex max-w-md flex-col gap-1.5">
                          <div className="flex items-center justify-between gap-2 text-xs text-ink-secondary">
                            <span>{job.progress.message}</span>
                            {fraction !== null && (
                              <span className="font-mono tabular-nums">
                                {fraction}%
                              </span>
                            )}
                          </div>
                          {fraction !== null && (
                            <ProgressBar
                              value={fraction}
                              label={`${title}: ${job.progress.message}`}
                              size="sm"
                            />
                          )}
                        </div>
                      )}

                      {job.state === "failed" && (
                        <p
                          role="alert"
                          className="text-xs leading-5 font-medium text-danger"
                        >
                          {job.error?.message ??
                            "This analysis stopped before finishing."}
                        </p>
                      )}

                      {job.state === "done" && (
                        <div className="flex flex-wrap items-center gap-1.5 pt-1 text-xs text-ink-secondary">
                          {job.candidate_count !== null && (
                            <span className={STAT_CLASS}>
                              <strong className="font-semibold text-ink-primary tabular-nums">
                                {job.candidate_count}
                              </strong>{" "}
                              {job.candidate_count === 1 ? "moment" : "moments"}
                            </span>
                          )}
                          {(job.export_count > 0 ||
                            job.missing_export_count > 0) && (
                            <span className={STAT_CLASS}>
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
                            <span className={STAT_CLASS}>
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
                          variant="secondary"
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
                            variant="ghost"
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
                      className="relative flex flex-col gap-3 overflow-hidden rounded-control border border-danger-border bg-danger-soft py-3.5 pr-4 pl-5 text-xs text-danger-soft-ink"
                    >
                      <span
                        aria-hidden="true"
                        className="absolute inset-y-0 left-0 w-[3px] bg-danger"
                      />
                      <p className="font-display text-sm font-semibold text-ink-primary">
                        Delete this analysis?
                      </p>
                      <p className="leading-5 text-pretty">
                        This removes the job from the library index and deletes
                        its saved analysis result JSON on disk.{" "}
                        {renderFilesCount === 0
                          ? "Any original local video file you provided is never deleted."
                          : fileName
                            ? `Your original local video file (${fileName}) is never deleted.`
                            : "Any original local video file is never deleted."}
                      </p>

                      {renderFilesCount > 0 && (
                        <label className="flex cursor-pointer items-start gap-2.5 rounded-control border border-border-subtle bg-surface-panel px-3 py-2.5 text-xs text-ink-primary transition-colors hover:border-border-strong">
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
                        <p role="alert" className="font-semibold text-danger">
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
            <Notice role="alert" tone="danger">
              {loadMoreError}
            </Notice>
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
