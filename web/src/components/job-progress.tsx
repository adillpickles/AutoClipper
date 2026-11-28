"use client";

import { useParams, useRouter } from "next/navigation";
import { useEffect, useLayoutEffect, useRef, useState } from "react";

import {
  CountValue,
  pipelineCounts,
  STAGE_LABELS,
  StageVisual,
} from "@/components/pipeline-stages";
import { Badge, type BadgeTone } from "@/components/ui/badge";
import { Button, ButtonLink } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/empty-state";
import { ErrorState } from "@/components/ui/error-state";
import { Notice } from "@/components/ui/notice";
import { ProgressBar } from "@/components/ui/progress";
import { JobProgressSkeleton } from "@/components/ui/skeleton";
import {
  ApiRequestError,
  getApiClient,
  POLL_INTERVAL_MS,
} from "@/lib/api/client";
import { JOB_STAGES } from "@/lib/api/stages";
import type { AnalysisResult, Job } from "@/lib/api/types";
import {
  clearJustRequested,
  shouldPace,
  targetStep,
  usePacedStep,
  wasJustRequested,
} from "@/lib/stage-pacing";
import { momentsSummary } from "@/lib/top-clips";
import { usePrefersReducedMotion } from "@/lib/use-reduced-motion";

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

  // With cacheComponents, Next keeps this page in a hidden <Activity> when the user leaves it
  // (as for the start form, #184), so coming back would show the old screen with its replay
  // already done, and open the results at once (#187). That happens when the same VOD is
  // analyzed again and the server returns this job. Effect cleanup runs when the page is
  // hidden: go back to loading, so the next visit polls and decides afresh whether to replay
  // the stages and open the results. useLayoutEffect lands it before the page shows again.
  useLayoutEffect(() => {
    return () => setView({ kind: "loading" });
  }, []);

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
    return <JobProgressSkeleton heading="h1" />;
  }
  if (view.kind === "missing") {
    return (
      <ErrorState
        heading="h1"
        tone="signal"
        title="This analysis no longer exists"
        message={view.message}
        primaryAction={
          <ButtonLink href="/" variant="primary" size="lg">
            Start a new analysis
          </ButtonLink>
        }
      />
    );
  }
  if (view.kind === "error" || view.kind === "offline") {
    const retry = () => {
      setView({ kind: "loading" });
      setAttempt((n) => n + 1);
    };
    return (
      <ErrorState
        heading="h1"
        alert
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
        primaryAction={
          <Button variant="primary" size="lg" onClick={retry}>
            Retry
          </Button>
        }
        secondaryAction={
          <ButtonLink href="/" variant="secondary" size="lg">
            Start another VOD
          </ButtonLink>
        }
      />
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
  // The stepper lists the contract's stages. A server reporting other stages (`stage` is an
  // open enum) still gets the headline, just no list and no pacing.
  const knownStages = stageCount === JOB_STAGES.length;
  // Decided once, from the job as first seen (#188): walk through the stages unless this is an
  // old finished job. A failure is shown at once.
  // A job the start form just asked for is replayed too, even when the server reused one that
  // finished earlier (#186).
  const [paceOnOpen] = useState(
    () => wasJustRequested(job.job_id) || shouldPace(job, Date.now()),
  );
  useEffect(() => clearJustRequested(job.job_id), [job.job_id]);
  const { shown, catchingUp, skip } = usePacedStep(
    targetStep(job, stageCount),
    { pace: knownStages && paceOnOpen && job.state !== "failed" },
  );
  const reducedMotion = usePrefersReducedMotion();
  const router = useRouter();
  // "Skip to the results" moves focus to the first action of the finished screen, which
  // replaces the button that had focus.
  const doneActions = useRef<HTMLDivElement>(null);
  const focusDoneActions = useRef(false);
  function skipReplay() {
    focusDoneActions.current = true;
    skip();
  }

  const finished = shown > stageCount;
  useEffect(() => {
    if (!finished || !focusDoneActions.current) return;
    focusDoneActions.current = false;
    doneActions.current?.querySelector<HTMLElement>("a, button")?.focus();
  }, [finished]);
  const shownStage =
    knownStages && shown >= 1 && !finished ? JOB_STAGES[shown - 1] : null;
  const shownLabel = shownStage
    ? (STAGE_LABELS[shownStage.stage] ?? shownStage.message)
    : (progress?.message ?? "");
  // The state the screen shows: a finished job reads as running until the stepper catches up.
  const shownState: Job["state"] =
    job.state === "done" && !finished ? "running" : job.state;
  const headline = {
    queued: "Waiting to start",
    running:
      shown >= 1 ? `Step ${shown} of ${stageCount}: ${shownLabel}` : "Working",
    done: "Analysis finished",
    failed: "Analysis stopped",
  }[shownState];
  const listLabel = {
    queued: `${stageCount} steps, not started`,
    running: `Step ${shown} of ${stageCount}`,
    done: `All ${stageCount} steps done`,
    failed: `Stopped at step ${shown} of ${stageCount}`,
  }[shownState];
  // A measured fraction belongs to the server's current stage, so the bar appears only once
  // the stepper shows that stage.
  const fraction =
    job.state === "running" && !catchingUp
      ? (progress?.stage_fraction ?? null)
      : null;

  function statusOf(index: number): StepStatus {
    if (finished || index < shown) return "done";
    if (index === shown) return job.state === "failed" ? "failed" : "current";
    return "pending";
  }

  // A job this screen watched finish, or one just requested or just finished, opens its results
  // by itself once the stepper's replay is done or skipped (#187); its top 10 are already
  // rendering on the server. An old finished job opened again stays here with "See the clips",
  // so Back from the results never bounces forward again; `replace` keeps this page out of the
  // history for the same reason.
  const opensResults =
    paceOnOpen &&
    job.state === "done" &&
    finished &&
    result !== null &&
    result.candidates.length > 0;
  const resultsHref = `/jobs/${job.job_id}/results`;
  useEffect(() => {
    if (opensResults) router.replace(resultsHref);
  }, [opensResults, resultsHref, router]);

  // Each number appears once the stepper has passed the stage that produced it.
  const counts = result
    ? pipelineCounts(result).map((count) => {
        const index = JOB_STAGES.findIndex((s) => s.stage === count.stage) + 1;
        return { ...count, known: finished || index < shown };
      })
    : [];

  const status = STATUS_BADGE[shownState];

  return (
    <div className="relative flex flex-col gap-6 overflow-hidden rounded-panel border border-border-subtle bg-surface-panel p-5 shadow-panel sm:p-8">
      {/* A faint accent wash behind the headline: the edit bay's "on air" light. */}
      <div
        aria-hidden="true"
        className="pointer-events-none absolute inset-x-0 top-0 h-40 bg-[radial-gradient(60%_100%_at_50%_0%,var(--accent-soft),transparent)]"
      />
      <div className="relative flex flex-col gap-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <Badge mono className="max-w-full truncate">
            VOD {job.video_id}
          </Badge>
          <Badge tone={status.tone} live={status.live} dot={!status.live}>
            {status.label}
          </Badge>
        </div>
        <h1 className="font-display text-2xl font-semibold tracking-[-0.025em] text-balance break-words text-ink-primary sm:text-3xl">
          {headline}
        </h1>
        {/* Stage changes are announced here. While the stepper replays stages the server has
            already finished, it says "Analyzing…" once instead of every step. */}
        <p className="sr-only" aria-live="polite" data-testid="stage-announcer">
          {catchingUp ? "Analyzing…" : headline}
        </p>
        {job.state === "queued" && (
          <p className="text-sm leading-6 text-ink-secondary">
            The server starts it as soon as it&apos;s free.
          </p>
        )}
      </div>

      {shownStage && job.state !== "failed" && (
        <div className="relative flex flex-col items-center gap-3 rounded-card border border-border-subtle bg-surface-canvas/60 px-4 pt-5 pb-4">
          <StageVisual
            key={shownStage.stage}
            stage={shownStage.stage}
            animated={!reducedMotion}
          />
          <p className="text-sm font-semibold text-ink-primary">{shownLabel}</p>
          {fraction !== null && progress && (
            <div className="flex w-full max-w-sm flex-col gap-2">
              <ProgressBar
                value={fraction * 100}
                label={progress.message}
                size="sm"
              />
              <p className="text-center font-mono text-xs text-ink-secondary tabular-nums">
                {Math.round(fraction * 100)}% of this step
              </p>
            </div>
          )}
          {catchingUp && job.state === "done" && (
            <Button variant="ghost" size="sm" onClick={skipReplay}>
              Skip to the results
            </Button>
          )}
        </div>
      )}

      {counts.length > 0 && (
        <dl
          aria-label="Analysis numbers"
          className={`relative grid gap-2 sm:gap-3 ${
            counts.length === 3 ? "grid-cols-3" : "grid-cols-2"
          }`}
        >
          {counts.map((count) => (
            <div
              key={count.label}
              className="flex min-w-0 flex-col gap-1 rounded-card border border-border-subtle bg-surface-elevated px-3 py-2.5 sm:px-4"
            >
              <dt className="text-xs leading-4 text-ink-secondary">
                {count.label}
              </dt>
              <dd
                className={`font-mono text-lg font-semibold tabular-nums transition-colors sm:text-xl ${
                  count.known ? "text-ink-primary" : "text-ink-tertiary"
                }`}
              >
                {count.known && count.value !== null ? (
                  <CountValue value={count.value} />
                ) : (
                  <>
                    <span aria-hidden="true">–</span>
                    <span className="sr-only">Not counted yet</span>
                  </>
                )}
              </dd>
            </div>
          ))}
        </dl>
      )}

      {knownStages && (
        <ol
          role="list"
          className="relative flex flex-col gap-3.5 border-t border-border-subtle pt-5 before:absolute before:top-8 before:bottom-3 before:left-[0.6875rem] before:w-0.5 before:rounded-pill before:bg-border-subtle"
          aria-label={listLabel}
        >
          {JOB_STAGES.map((stage, i) => {
            const status = statusOf(i + 1);
            return (
              <li
                key={stage.stage}
                aria-current={status === "current" ? "step" : undefined}
                className="relative flex items-center gap-3 text-sm"
              >
                <StepIcon status={status} />
                <span className={`min-w-0 ${STEP_TEXT[status]}`}>
                  {STAGE_LABELS[stage.stage] ?? stage.message}
                  <span className="sr-only">{STEP_SUFFIX[status]}</span>
                </span>
              </li>
            );
          })}
        </ol>
      )}

      {job.warnings.length > 0 && (
        <ul
          role="list"
          aria-label="Analysis warnings"
          className="flex flex-col gap-2"
        >
          {job.warnings.map((warning) => (
            <li key={warning.code}>
              <Notice tone="signal">{warning.message}</Notice>
            </li>
          ))}
        </ul>
      )}

      {job.state === "failed" && job.error && (
        <ErrorState
          heading="h2"
          alert
          title="What happened"
          message={job.error.message}
          primaryAction={
            <ButtonLink href="/" variant="primary">
              Try another VOD
            </ButtonLink>
          }
        />
      )}

      <div ref={doneActions} className="contents">
        {job.state === "done" &&
          finished &&
          result &&
          (result.candidates.length === 0 ? (
            <div className="border-t border-border-subtle pt-5">
              <EmptyState
                heading="h2"
                title="No moments stood out"
                description={
                  result.warnings.length > 0
                    ? "Nothing could be ranked from the signals that were available for this VOD."
                    : "Analysis finished, but no moments stood out to rank for this VOD."
                }
                action={
                  <ButtonLink href="/" variant="primary">
                    Try another VOD
                  </ButtonLink>
                }
                secondaryAction={
                  <ButtonLink
                    href={`/jobs/${job.job_id}/results`}
                    variant="secondary"
                  >
                    See the results
                  </ButtonLink>
                }
              />
            </div>
          ) : (
            <div className="flex flex-col gap-4 border-t border-border-subtle pt-5">
              <div className="flex items-start gap-3">
                <span
                  aria-hidden="true"
                  className="mt-0.5 flex size-7 shrink-0 items-center justify-center rounded-pill border border-success-border bg-success-soft text-success"
                >
                  <CheckGlyph className="size-3.5" />
                </span>
                <p
                  className="text-base leading-7 font-semibold text-ink-primary"
                  role="status"
                >
                  {momentsSummary(result.candidates.length)}
                  {opensResults && " Opening the clips…"}
                </p>
              </div>
              <div className="flex flex-wrap items-center gap-3">
                <ButtonLink href={resultsHref} variant="primary" size="lg">
                  See the clips
                </ButtonLink>
                <ButtonLink href="/" variant="secondary" size="lg">
                  Analyze another VOD
                </ButtonLink>
              </div>
            </div>
          ))}
      </div>
    </div>
  );
}

const STATUS_BADGE: Record<
  Job["state"],
  { label: string; tone: BadgeTone; live: boolean }
> = {
  queued: { label: "Queued", tone: "neutral", live: false },
  running: { label: "Analyzing", tone: "accent", live: true },
  done: { label: "Finished", tone: "success", live: false },
  failed: { label: "Stopped", tone: "danger", live: false },
};

function CheckGlyph({ className = "" }: { className?: string }) {
  return (
    <svg
      viewBox="0 0 16 16"
      fill="none"
      aria-hidden="true"
      className={className}
      stroke="currentColor"
      strokeWidth="2.2"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d="M3.5 8.5l3 3 6-7" />
    </svg>
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
  // Solid backgrounds hide the stepper's connector line behind each icon.
  const base =
    "relative z-10 flex size-6 shrink-0 items-center justify-center rounded-pill text-xs font-bold";
  if (status === "done") {
    return (
      <span aria-hidden="true" className={`${base} bg-success text-accent-ink`}>
        <CheckGlyph className="size-3.5" />
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
      <span
        aria-hidden="true"
        className={`${base} border-2 border-accent bg-surface-panel shadow-[0_0_0_4px_var(--accent-soft)]`}
      >
        <span className="size-2 rounded-pill bg-accent motion-safe:animate-pulse" />
      </span>
    );
  }
  return (
    <span
      aria-hidden="true"
      className={`${base} border-2 border-border-strong bg-surface-panel`}
    />
  );
}
