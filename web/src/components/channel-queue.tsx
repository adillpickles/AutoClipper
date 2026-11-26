"use client";

import { Button, ButtonLink } from "@/components/ui/button";
import { formatTime } from "@/lib/format";
import type { Job } from "@/lib/api/types";

export interface ChannelQueueItem {
  jobId: string;
  videoId: string;
  title: string;
  channelDisplayName: string;
  durationS: number;
  job: Job | null;
  missing: boolean;
  pollError: string | null;
}

function stateBadge(item: ChannelQueueItem): {
  label: string;
  className: string;
  pulse?: boolean;
} {
  if (item.missing) {
    return {
      label: "Expired",
      className: "border-signal-border bg-signal-soft text-signal-soft-ink",
    };
  }
  const state = item.job?.state ?? "queued";
  if (state === "done") {
    return {
      label: "Done",
      className:
        "border-source-both-border bg-source-both-soft text-source-both-soft-ink",
    };
  }
  if (state === "failed") {
    return {
      label: "Failed",
      className: "border-danger-border bg-danger-soft text-danger-soft-ink",
    };
  }
  if (state === "running") {
    const progress = item.job?.progress;
    const label = progress
      ? `Step ${progress.stage_index} of ${progress.stage_count}`
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

export function ChannelQueue({
  items,
  pollOffline = false,
  onRetryPoll,
  onRetry,
  onClearFinished,
}: {
  items: ChannelQueueItem[];
  pollOffline?: boolean;
  onRetryPoll?: () => void;
  onRetry: (item: ChannelQueueItem) => void;
  onClearFinished: () => void;
}) {
  if (items.length === 0) return null;

  let running = 0;
  let queued = 0;
  let done = 0;
  let failed = 0;
  for (const item of items) {
    if (item.missing || item.job?.state === "failed") {
      failed += 1;
    } else if (item.job?.state === "done") {
      done += 1;
    } else if (item.job?.state === "running") {
      running += 1;
    } else {
      queued += 1;
    }
  }
  const hasFinished = done > 0 || failed > 0;

  return (
    <section
      aria-labelledby="channel-queue-heading"
      className="flex flex-col gap-4 rounded-panel border border-border-subtle bg-surface-panel p-5 shadow-panel sm:p-6"
    >
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex flex-col gap-1">
          <h2
            id="channel-queue-heading"
            className="font-display text-base font-semibold text-ink-primary sm:text-lg"
          >
            Analysis queue
          </h2>
          <p
            className="text-xs text-ink-secondary sm:text-sm"
            aria-live="polite"
          >
            {items.length} {items.length === 1 ? "VOD" : "VODs"}
            {running > 0 ? ` · ${running} running` : ""}
            {queued > 0 ? ` · ${queued} queued` : ""}
            {done > 0 ? ` · ${done} done` : ""}
            {failed > 0 ? ` · ${failed} failed` : ""}
          </p>
        </div>
        {hasFinished && (
          <Button variant="subtle" size="sm" onClick={onClearFinished}>
            Clear finished
          </Button>
        )}
      </div>

      {pollOffline && (
        <div
          role="alert"
          className="flex flex-wrap items-center justify-between gap-3 rounded-card border border-danger-border bg-danger-soft px-4 py-3 text-sm text-danger-soft-ink"
        >
          <p className="font-medium">
            Can&apos;t reach the server to refresh job progress. Check that
            AutoClipper is running, then try again.
          </p>
          {onRetryPoll && (
            <Button variant="secondary" size="sm" onClick={onRetryPoll}>
              Retry
            </Button>
          )}
        </div>
      )}

      <ol role="list" className="flex flex-col gap-3">
        {items.map((item) => {
          const badge = stateBadge(item);
          const state = item.job?.state ?? "queued";
          const progress = item.job?.progress ?? null;
          const fraction =
            state === "running" && progress?.stage_fraction != null
              ? Math.round(progress.stage_fraction * 100)
              : null;
          const warnings = item.job?.warnings ?? [];

          return (
            <li
              key={item.jobId}
              className="flex flex-col gap-3 rounded-card border border-border-subtle bg-surface-elevated p-4 sm:flex-row sm:items-center sm:justify-between sm:gap-4"
            >
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
                  <span className="font-mono text-xs text-ink-tertiary">
                    {formatTime(item.durationS)}
                  </span>
                  <span className="truncate text-xs text-ink-tertiary">
                    {item.channelDisplayName}
                  </span>
                </div>

                <p className="truncate text-sm font-semibold text-ink-primary">
                  {item.title}
                </p>

                {state === "running" && progress && (
                  <div className="flex flex-col gap-1.5">
                    <div className="flex items-center justify-between gap-2 text-xs text-ink-secondary">
                      <span>{progress.message}</span>
                      {fraction !== null && (
                        <span className="font-mono">{fraction}%</span>
                      )}
                    </div>
                    {fraction !== null && (
                      <div
                        role="progressbar"
                        aria-label={`${item.title}: ${progress.message}`}
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

                {state === "queued" && !item.missing && (
                  <p className="text-xs text-ink-secondary">
                    Waiting in queue — runs automatically after the current VOD
                    finishes.
                  </p>
                )}

                {!item.missing && warnings.length > 0 && (
                  <ul
                    role="list"
                    aria-label={`${item.title} warnings`}
                    className="flex flex-col gap-1 text-xs text-signal-soft-ink"
                  >
                    {warnings.map((warning) => (
                      <li key={warning.code}>{warning.message}</li>
                    ))}
                  </ul>
                )}

                {state === "failed" && item.job?.error && (
                  <p role="alert" className="text-xs font-medium text-danger">
                    {item.job.error.message}
                  </p>
                )}

                {item.missing && (
                  <p className="text-xs text-signal-soft-ink">
                    This job no longer exists on the server (it may have
                    restarted). Queue it again to analyze.
                  </p>
                )}

                {item.pollError && !item.missing && !pollOffline && (
                  <p className="text-xs text-signal-soft-ink">
                    {item.pollError}
                  </p>
                )}
              </div>

              <div className="flex shrink-0 flex-wrap items-center gap-2">
                {!item.missing && state === "done" && (
                  <ButtonLink
                    href={`/jobs/${item.jobId}/results`}
                    variant="primary"
                    size="sm"
                  >
                    View results
                  </ButtonLink>
                )}
                {!item.missing &&
                  (state === "queued" || state === "running") && (
                    <ButtonLink
                      href={`/jobs/${item.jobId}`}
                      variant="secondary"
                      size="sm"
                    >
                      View progress
                    </ButtonLink>
                  )}
                {(item.missing || state === "failed") && (
                  <Button
                    variant="secondary"
                    size="sm"
                    onClick={() => onRetry(item)}
                  >
                    Retry
                  </Button>
                )}
                {!item.missing && state === "failed" && (
                  <ButtonLink
                    href={`/jobs/${item.jobId}`}
                    variant="ghost"
                    size="sm"
                  >
                    Details
                  </ButtonLink>
                )}
              </div>
            </li>
          );
        })}
      </ol>
    </section>
  );
}
