"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useState } from "react";

import {
  ApiRequestError,
  getApiClient,
  POLL_INTERVAL_MS,
} from "@/lib/api/client";
import { JOB_STAGES } from "@/lib/api/mock";
import type { AnalysisResult, Job } from "@/lib/api/types";

type View =
  | { kind: "loading" }
  | { kind: "job"; job: Job; result: AnalysisResult | null }
  | { kind: "missing"; message: string }
  | { kind: "offline" };

// Back off on network errors (2, 4, 8 s, capped at 10 s) and give up after about a minute (§ 9.1).
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
    let backoff = 2000;
    const client = getApiClient();

    async function poll() {
      try {
        const job = await client.getJob(jobId);
        failingSince = null;
        backoff = 2000;
        if (cancelled) return;
        if (job.state === "done") {
          const result = await client.getResult(jobId);
          if (!cancelled) setView({ kind: "job", job, result });
          return;
        }
        setView({ kind: "job", job, result: null });
        if (job.state === "failed") return;
        timer = setTimeout(poll, POLL_INTERVAL_MS);
      } catch (error) {
        if (cancelled) return;
        if (error instanceof ApiRequestError && error.status === 404) {
          setView({ kind: "missing", message: error.error.message });
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
    return <p className="text-zinc-600 dark:text-zinc-400">Loading…</p>;
  }
  if (view.kind === "missing") {
    return (
      <Problem
        title="This analysis no longer exists"
        message={view.message}
        action="Start again"
      />
    );
  }
  if (view.kind === "offline") {
    return (
      <div className="flex flex-col gap-4">
        <Problem
          title="Can't reach the server"
          message="Check that AutoClipper is running, then try again."
        />
        <button
          type="button"
          onClick={() => setAttempt((n) => n + 1)}
          className="self-start rounded-lg bg-amber-500 px-5 py-3 font-semibold text-zinc-950 hover:bg-amber-400 focus-visible:ring-2 focus-visible:ring-amber-700 focus-visible:outline-none"
        >
          Retry
        </button>
      </div>
    );
  }
  return <JobStatus job={view.job} result={view.result} />;
}

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
  const headline = {
    queued: "Waiting to start",
    running: progress
      ? `Step ${progress.stage_index} of ${progress.stage_count}: ${progress.message}`
      : "Working",
    done: "Analysis finished",
    failed: "Analysis stopped",
  }[job.state];

  return (
    <div className="flex flex-col gap-8">
      <div className="flex flex-col gap-2">
        <p className="text-sm font-medium text-zinc-500 dark:text-zinc-400">
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
            Another analysis is ahead of this one.
          </p>
        )}
      </div>

      {job.state === "running" && progress?.stage_fraction != null && (
        <div className="flex flex-col gap-2">
          <div
            role="progressbar"
            aria-label={progress.message}
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={Math.round(progress.stage_fraction * 100)}
            className="h-2 overflow-hidden rounded-full bg-zinc-200 dark:bg-zinc-800"
          >
            <div
              className="h-full rounded-full bg-amber-500 transition-[width]"
              style={{ width: `${Math.round(progress.stage_fraction * 100)}%` }}
            />
          </div>
          <p className="text-sm text-zinc-600 dark:text-zinc-400">
            {Math.round(progress.stage_fraction * 100)}% of this step
          </p>
        </div>
      )}

      <ol
        role="list"
        className="flex flex-col gap-3"
        aria-label={`Step ${current} of ${stageCount}`}
      >
        {JOB_STAGES.map((stage, i) => {
          const index = i + 1;
          const status =
            job.state === "done" || index < current
              ? "done"
              : index === current
                ? job.state === "failed"
                  ? "failed"
                  : "current"
                : "pending";
          return (
            <li key={stage.stage} className="flex items-center gap-3">
              <StepIcon status={status} />
              <span
                className={
                  status === "pending"
                    ? "text-zinc-500 dark:text-zinc-500"
                    : status === "failed"
                      ? "font-medium text-red-700 dark:text-red-300"
                      : status === "current"
                        ? "font-medium"
                        : "text-zinc-700 dark:text-zinc-300"
                }
              >
                {stage.message}
                <span className="sr-only">
                  {
                    {
                      done: " (done)",
                      current: " (in progress)",
                      failed: " (stopped here)",
                      pending: "",
                    }[status]
                  }
                </span>
              </span>
            </li>
          );
        })}
      </ol>

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
        />
      )}

      {job.state === "done" && result && (
        <p className="text-lg">
          {result.candidates.length === 0
            ? "No moments stood out in this VOD."
            : `Found ${result.candidates.length} ${result.candidates.length === 1 ? "moment" : "moments"}.`}
        </p>
      )}
    </div>
  );
}

function StepIcon({
  status,
}: {
  status: "done" | "current" | "failed" | "pending";
}) {
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
}: {
  title: string;
  message: string;
  action?: string;
}) {
  return (
    <div className="flex flex-col gap-3 rounded-xl border border-red-200 bg-red-50 p-5 dark:border-red-900 dark:bg-red-950/40">
      <h2 className="font-semibold text-red-900 dark:text-red-200">{title}</h2>
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
