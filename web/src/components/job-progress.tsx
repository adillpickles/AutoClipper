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
    return <h1 className="text-2xl font-semibold tracking-tight">Loading…</h1>;
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
          className="self-start rounded-lg bg-amber-500 px-5 py-3 font-semibold text-zinc-950 hover:bg-amber-400 focus-visible:ring-2 focus-visible:ring-amber-700 focus-visible:outline-none"
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
    <div className="flex flex-col gap-8">
      <div className="flex flex-col gap-2">
        <p className="text-sm font-medium text-zinc-600 dark:text-zinc-400">
          VOD {job.video_id}
        </p>
        <h1
          className="text-2xl font-semibold tracking-tight sm:text-3xl"
          aria-live="polite"
        >
          {headline}
        </h1>
        {job.state === "queued" && (
          <p className="text-zinc-600 dark:text-zinc-400">
            The server starts it as soon as it&apos;s free.
          </p>
        )}
      </div>

      {fraction !== null && progress && (
        <div className="flex flex-col gap-2">
          <div
            role="progressbar"
            aria-label={progress.message}
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={Math.round(fraction * 100)}
            className="h-2 overflow-hidden rounded-full bg-zinc-200 dark:bg-zinc-800"
          >
            <div
              className="h-full rounded-full bg-amber-500 transition-[width] duration-500"
              style={{ width: `${Math.round(fraction * 100)}%` }}
            />
          </div>
          <p className="text-sm text-zinc-600 dark:text-zinc-400">
            {Math.round(fraction * 100)}% of this step
          </p>
        </div>
      )}

      {knownStages && (
        <ol role="list" className="flex flex-col gap-3" aria-label={listLabel}>
          {JOB_STAGES.map((stage, i) => {
            const status = statusOf(i + 1);
            return (
              <li key={stage.stage} className="flex items-center gap-3">
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
              className="rounded-lg bg-amber-50 px-4 py-3 text-sm text-amber-900 dark:bg-amber-400/10 dark:text-amber-100"
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
        <div className="flex flex-col items-start gap-4">
          <p className="text-lg" role="status">
            {result.candidates.length === 0
              ? "No moments stood out in this VOD."
              : `Found ${result.candidates.length} ${result.candidates.length === 1 ? "moment" : "moments"}.`}
          </p>
          <Link
            href={`/jobs/${job.job_id}/results`}
            className="rounded-lg bg-amber-500 px-5 py-3 font-semibold text-zinc-950 hover:bg-amber-400 focus-visible:ring-2 focus-visible:ring-amber-700 focus-visible:outline-none"
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
  done: "text-zinc-700 dark:text-zinc-300",
  current: "font-medium",
  failed: "font-medium text-red-700 dark:text-red-300",
  pending: "text-zinc-500 dark:text-zinc-400",
};

const STEP_SUFFIX: Record<StepStatus, string> = {
  done: " (done)",
  current: " (in progress)",
  failed: " (stopped here)",
  pending: "",
};

function StepIcon({ status }: { status: StepStatus }) {
  const base =
    "flex size-6 shrink-0 items-center justify-center rounded-full text-xs font-bold";
  if (status === "done") {
    return (
      <span aria-hidden="true" className={`${base} bg-emerald-600 text-white`}>
        ✓
      </span>
    );
  }
  if (status === "failed") {
    return (
      <span aria-hidden="true" className={`${base} bg-red-600 text-white`}>
        !
      </span>
    );
  }
  if (status === "current") {
    return (
      <span aria-hidden="true" className={`${base} border-2 border-amber-500`}>
        <span className="size-2 animate-pulse rounded-full bg-amber-500" />
      </span>
    );
  }
  return (
    <span
      aria-hidden="true"
      className={`${base} border-2 border-zinc-300 dark:border-zinc-700`}
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
      className="flex flex-col gap-3 rounded-xl border border-red-200 bg-red-50 p-5 dark:border-red-900 dark:bg-red-950/40"
    >
      <Heading
        className={`font-semibold text-red-900 dark:text-red-200 ${Heading === "h1" ? "text-2xl" : ""}`}
      >
        {title}
      </Heading>
      <p className="text-red-900 dark:text-red-100">{message}</p>
      {action && (
        <Link
          href="/"
          className="self-start rounded-lg bg-white px-4 py-2 font-medium text-zinc-900 ring-1 ring-zinc-300 hover:bg-zinc-50 focus-visible:ring-2 focus-visible:ring-amber-500 focus-visible:outline-none dark:bg-zinc-900 dark:text-zinc-100 dark:ring-zinc-700"
        >
          {action}
        </Link>
      )}
    </div>
  );
}
