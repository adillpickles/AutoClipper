"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useState } from "react";

import {
  ApiRequestError,
  getApiClient,
  POLL_INTERVAL_MS,
} from "@/lib/api/client";
import { JOB_STAGES } from "@/lib/api/stages";
import type { AnalysisResult, Job } from "@/lib/api/types";

type View =
  | { kind: "loading" }
  | { kind: "job"; job: Job; result: AnalysisResult | null }
  | { kind: "missing"; message: string }
  | { kind: "error"; message: string }
  | { kind: "offline" };

// Back off on network errors (2, 4, 8 s, capped at 10 s) and give up after about a minute (§ 9.1).
const FIRST_BACKOFF_MS = 2000;
const MAX_BACKOFF_MS = 10_000;
const GIVE_UP_AFTER_MS = 60_000;

export function JobProgress() {
  const { jobId } = useParams<{ jobId: string }>();
  const [view, setView] = useState<View>({ kind: "loading" });
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let failingSince: number | null = null;
    let backoff = FIRST_BACKOFF_MS;
    const client = getApiClient();

    async function poll() {
      try {
        const job = await client.getJob(jobId);
        const result =
          job.state === "done" ? await client.getResult(jobId) : null;
        // Reset the backoff only after a whole round succeeds, so a failing result fetch backs
        // off and gives up like any other failure.
        failingSince = null;
        backoff = FIRST_BACKOFF_MS;
        if (cancelled) return;
        setView({ kind: "job", job, result });
        if (job.state === "queued" || job.state === "running") {
          timer = setTimeout(poll, POLL_INTERVAL_MS);
        }
      } catch (error) {
        if (cancelled) return;
        if (error instanceof ApiRequestError && error.status === 404) {
          setView({ kind: "missing", message: error.error.message });
          return;
        }
        if (error instanceof ApiRequestError && error.status >= 500) {
          // The server answered, so it isn't unreachable: it failed.
          setView({ kind: "error", message: error.error.message });
          return;
        }
        failingSince ??= Date.now();
        if (Date.now() - failingSince > GIVE_UP_AFTER_MS) {
          setView({ kind: "offline" });
          return;
        }
        timer = setTimeout(poll, backoff);
        backoff = Math.min(backoff * 2, MAX_BACKOFF_MS);
      }
    }

    void poll();
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [jobId, attempt]);

  if (view.kind === "loading") {
    return (
      <h1 className="font-display text-2xl font-semibold tracking-tight text-ink-primary">
        Loading…
      </h1>
    );
  }
  if (view.kind === "missing") {
    return (
      <Problem
        heading="h1"
        title="This analysis no longer exists"
        message={view.message}
        action="Start again"
      />
    );
  }
  if (view.kind === "error" || view.kind === "offline") {
    const retry = () => {
      setView({ kind: "loading" });
      setAttempt((n) => n + 1);
    };
    return (
      <div className="flex flex-col gap-4">
        <Problem
          heading="h1"
          title={
            view.kind === "offline"
              ? "Can't reach the server"
              : "Something went wrong"
          }
          message={
            view.kind === "offline"
              ? "Check that AutoClipper is running, then try again."
              : view.message
          }
        />
        <button
          type="button"
          onClick={retry}
          className="self-start rounded-control bg-accent px-5 py-3 text-sm font-semibold text-accent-ink shadow-card transition-colors hover:bg-accent-hover focus-visible:ring-2 focus-visible:ring-focus-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface-canvas focus-visible:outline-none"
        >
          Retry
        </button>
      </div>
    );
  }
  return <JobStatus job={view.job} result={view.result} />;
}

type StepStatus = "done" | "current" | "failed" | "pending";

function JobStatus({
  job,
  result,
}: {
  job: Job;
  result: AnalysisResult | null;
}) {
  const progress = job.progress;
  const stageCount = progress?.stage_count ?? JOB_STAGES.length;
  const current = progress?.stage_index ?? 0;
  // The stepper lists the contract's stages. A server reporting other stages (`stage` is an
  // open enum) still gets the headline, just no list.
  const knownStages = stageCount === JOB_STAGES.length;
  const headline = {
    queued: "Waiting to start",
    running: progress
      ? `Step ${progress.stage_index} of ${progress.stage_count}: ${progress.message}`
      : "Working",
    done: "Analysis finished",
    failed: "Analysis stopped",
  }[job.state];
  const listLabel = {
    queued: `${stageCount} steps, not started`,
    running: `Step ${current} of ${stageCount}`,
    done: `All ${stageCount} steps done`,
    failed: `Stopped at step ${current} of ${stageCount}`,
  }[job.state];
  const fraction =
    job.state === "running" ? (progress?.stage_fraction ?? null) : null;

  function statusOf(index: number): StepStatus {
    if (job.state === "done" || index < current) return "done";
    if (index === current) return job.state === "failed" ? "failed" : "current";
    return "pending";
  }

  return (
    <div className="flex flex-col gap-6 rounded-panel border border-border-subtle bg-surface-panel p-6 shadow-panel sm:p-8">
      <div className="flex flex-col gap-2">
        <p className="inline-flex w-fit items-center gap-2 rounded-pill border border-border-subtle bg-surface-elevated px-3 py-1 font-mono text-xs font-medium text-ink-secondary">
          VOD {job.video_id}
        </p>
        <h1
          className="font-display text-2xl font-semibold tracking-tight text-ink-primary sm:text-3xl"
          aria-live="polite"
        >
          {headline}
        </h1>
        {job.state === "queued" && (
          <p className="text-sm text-ink-secondary">
            The server starts it as soon as it&apos;s free.
          </p>
        )}
      </div>

      {fraction !== null && progress && (
        <div className="flex flex-col gap-2 rounded-card border border-border-subtle bg-surface-elevated p-4">
          <div
            role="progressbar"
            aria-label={progress.message}
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={Math.round(fraction * 100)}
            className="h-2 overflow-hidden rounded-pill bg-surface-subtle"
          >
            <div
              className="h-full rounded-pill bg-accent transition-[width] duration-500"
              style={{ width: `${Math.round(fraction * 100)}%` }}
            />
          </div>
          <p className="font-mono text-xs text-ink-secondary tabular-nums">
            {Math.round(fraction * 100)}% of this step
          </p>
        </div>
      )}

      {knownStages && (
        <ol
          role="list"
          className="flex flex-col gap-3 border-t border-border-subtle pt-5"
          aria-label={listLabel}
        >
          {JOB_STAGES.map((stage, i) => {
            const status = statusOf(i + 1);
            return (
              <li key={stage.stage} className="flex items-center gap-3 text-sm">
                <StepIcon status={status} />
                <span className={STEP_TEXT[status]}>
                  {stage.message}
                  <span className="sr-only">{STEP_SUFFIX[status]}</span>
                </span>
              </li>
            );
          })}
        </ol>
      )}

      {job.warnings.length > 0 && (
        <ul role="list" className="flex flex-col gap-2">
          {job.warnings.map((warning) => (
            <li
              key={warning.code}
              className="rounded-control border border-signal-border bg-signal-soft px-4 py-3 text-sm text-signal-soft-ink"
            >
              {warning.message}
            </li>
          ))}
        </ul>
      )}

      {job.state === "failed" && job.error && (
        <Problem
          title="What happened"
          message={job.error.message}
          action="Try another VOD"
          alert
        />
      )}

      {job.state === "done" && result && (
        <div className="flex flex-col items-start gap-4 border-t border-border-subtle pt-5">
          <p className="text-base font-medium text-ink-primary" role="status">
            {result.candidates.length === 0
              ? "No moments stood out in this VOD."
              : `Found ${result.candidates.length} ${result.candidates.length === 1 ? "moment" : "moments"}.`}
          </p>
          <Link
            href={`/jobs/${job.job_id}/results`}
            className="rounded-control bg-accent px-5 py-3 text-sm font-semibold text-accent-ink shadow-card transition-colors hover:bg-accent-hover focus-visible:ring-2 focus-visible:ring-focus-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface-panel focus-visible:outline-none"
          >
            {result.candidates.length === 0
              ? "See the results"
              : "See the clips"}
          </Link>
        </div>
      )}
    </div>
  );
}

const STEP_TEXT: Record<StepStatus, string> = {
  done: "text-ink-secondary",
  current: "font-semibold text-ink-primary",
  failed: "font-semibold text-danger",
  pending: "text-ink-tertiary",
};

const STEP_SUFFIX: Record<StepStatus, string> = {
  done: " (done)",
  current: " (in progress)",
  failed: " (stopped here)",
  pending: "",
};

function StepIcon({ status }: { status: StepStatus }) {
  const base =
    "flex size-6 shrink-0 items-center justify-center rounded-pill text-xs font-bold";
  if (status === "done") {
    return (
      <span
        aria-hidden="true"
        className={`${base} bg-source-both text-source-both-ink`}
      >
        ✓
      </span>
    );
  }
  if (status === "failed") {
    return (
      <span aria-hidden="true" className={`${base} bg-danger text-danger-ink`}>
        !
      </span>
    );
  }
  if (status === "current") {
    return (
      <span aria-hidden="true" className={`${base} border-2 border-accent`}>
        <span className="size-2 animate-pulse rounded-pill bg-accent" />
      </span>
    );
  }
  return (
    <span
      aria-hidden="true"
      className={`${base} border-2 border-border-strong`}
    />
  );
}

function Problem({
  title,
  message,
  action,
  heading: Heading = "h2",
  alert = false,
}: {
  title: string;
  message: string;
  action?: string;
  heading?: "h1" | "h2";
  alert?: boolean;
}) {
  return (
    <div
      role={alert ? "alert" : undefined}
      className="flex flex-col gap-3 rounded-card border border-danger-border bg-danger-soft p-5"
    >
      <Heading
        className={`font-display font-semibold text-danger-soft-ink ${Heading === "h1" ? "text-2xl" : "text-base"}`}
      >
        {title}
      </Heading>
      <p className="text-sm text-danger-soft-ink">{message}</p>
      {action && (
        <Link
          href="/"
          className="self-start rounded-control border border-border-strong bg-surface-panel px-4 py-2 text-sm font-medium text-ink-primary transition-colors hover:bg-surface-elevated focus-visible:ring-2 focus-visible:ring-focus-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface-panel focus-visible:outline-none"
        >
          {action}
        </Link>
      )}
    </div>
  );
}
