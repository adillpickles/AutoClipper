"use client";

import { Badge, type BadgeTone } from "@/components/ui/badge";
import { Button, ButtonLink } from "@/components/ui/button";
import { CardHeader, cardClasses } from "@/components/ui/card";
import { Notice } from "@/components/ui/notice";
import { ProgressBar } from "@/components/ui/progress";
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
  tone: BadgeTone;
  live?: boolean;
} {
  if (item.missing) {
    return { label: "Expired", tone: "signal" };
  }
  const state = item.job?.state ?? "queued";
  if (state === "done") {
    return { label: "Done", tone: "success" };
  }
  if (state === "failed") {
    return { label: "Failed", tone: "danger" };
  }
  if (state === "running") {
    const progress = item.job?.progress;
    const label = progress
      ? `Step ${progress.stage_index} of ${progress.stage_count}`
      : "Running";
    return { label, tone: "accent", live: true };
  }
  return { label: "Queued", tone: "neutral" };
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
      className={cardClasses({
        tone: "panel",
        padding: "lg",
        className: "flex flex-col gap-4",
      })}
    >
      <CardHeader
        id="channel-queue-heading"
        title="Analysis queue"
        description={
          <span aria-live="polite">
            {items.length} {items.length === 1 ? "VOD" : "VODs"}
            {running > 0 ? ` · ${running} running` : ""}
            {queued > 0 ? ` · ${queued} queued` : ""}
            {done > 0 ? ` · ${done} done` : ""}
            {failed > 0 ? ` · ${failed} failed` : ""}
          </span>
        }
        actions={
          hasFinished ? (
            <Button variant="ghost" size="sm" onClick={onClearFinished}>
              Clear finished
            </Button>
          ) : undefined
        }
      />

      {pollOffline && (
        <Notice
          role="alert"
          tone="danger"
          action={
            onRetryPoll && (
              <Button variant="secondary" size="sm" onClick={onRetryPoll}>
                Retry
              </Button>
            )
          }
        >
          Can&apos;t reach the server to refresh job progress. Check that
          AutoClipper is running, then try again.
        </Notice>
      )}

      <ol
        role="list"
        className="flex flex-col divide-y divide-border-subtle border-t border-border-subtle"
      >
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
              className="flex flex-col gap-3 py-4 sm:flex-row sm:items-center sm:justify-between sm:gap-6"
            >
              <div className="flex min-w-0 flex-1 flex-col gap-1.5">
                <div className="flex flex-wrap items-center gap-2">
                  <Badge tone={badge.tone} live={badge.live}>
                    {badge.label}
                  </Badge>
                  <span className="font-mono text-xs text-ink-tertiary tabular-nums">
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
                        <span className="font-mono tabular-nums">
                          {fraction}%
                        </span>
                      )}
                    </div>
                    {fraction !== null && (
                      <ProgressBar
                        size="sm"
                        value={fraction}
                        label={`${item.title}: ${progress.message}`}
                      />
                    )}
                  </div>
                )}

                {state === "queued" && !item.missing && (
                  <p className="text-xs text-ink-secondary">
                    Waiting in queue. Runs automatically after the current VOD
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
