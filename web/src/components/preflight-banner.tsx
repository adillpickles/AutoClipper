"use client";

import { useEffect, useRef, useState, type KeyboardEvent } from "react";

import { Badge } from "@/components/ui/badge";
import { Button, FOCUS_RING } from "@/components/ui/button";
import { getApiClient, isMockMode } from "@/lib/api/client";
import type {
  PreflightCheck,
  PreflightReport,
  ToolSetupItem,
  ToolSetupName,
  ToolSetupStatus,
} from "@/lib/api/types";

const BANNER_BASE =
  "relative overflow-hidden rounded-card border bg-surface-panel p-4 pl-5 text-sm leading-6 shadow-card sm:p-5 sm:pl-6";
const EDGE = "absolute inset-y-0 left-0 w-[3px]";
const DISMISS_CLASS = `-mt-1 -mr-1 inline-flex min-h-9 shrink-0 items-center rounded-control px-2.5 text-xs font-semibold text-ink-tertiary transition-colors hover:bg-surface-elevated hover:text-ink-primary ${FOCUS_RING}`;

const SETUP_POLL_BASE_MS = 1000;
const SETUP_POLL_MAX_BACKOFF_MS = 8000;
const SETUP_POLL_MAX_FAILURES = 4;

function problemChecks(report: PreflightReport | null): PreflightCheck[] {
  if (!report) return [];
  const problems = report.checks.filter((check) => check.status !== "ok");
  return problems.sort((a, b) => {
    if (a.status === b.status) return 0;
    return a.status === "fail" ? -1 : 1;
  });
}

function installableMissingTools(problems: PreflightCheck[]): ToolSetupName[] {
  const missing: ToolSetupName[] = [];
  for (const check of problems) {
    if (check.source === "config") continue;
    if (check.name === "ffmpeg" || check.name === "twitch_downloader") {
      missing.push(check.name);
    }
  }
  return missing;
}

function formatMegabytes(bytes: number): string {
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function downloadButtonLabel(
  missing: ToolSetupName[],
  running: boolean,
  hasError: boolean,
  pollLost: boolean,
): string {
  if (running) return "Downloading tools...";
  if (pollLost) return "Check setup status";
  if (hasError) return "Retry download";
  if (missing.length === 1) {
    return missing[0] === "ffmpeg"
      ? "Download FFmpeg & FFprobe"
      : "Download TwitchDownloaderCLI";
  }
  return "Download missing tools";
}

function stageBadgeText(stage: string): string {
  switch (stage) {
    case "queued":
      return "Queued";
    case "checking":
      return "Checking";
    case "downloading":
      return "Downloading";
    case "verifying":
      return "Verifying SHA-256";
    case "extracting":
      return "Extracting";
    case "done":
      return "Installed";
    case "skipped":
      return "Already installed";
    case "failed":
      return "Failed";
    default:
      return stage;
  }
}

function stageAnnouncement(setup: ToolSetupStatus | null): string {
  if (!setup || setup.items.length === 0) return "";
  return setup.items
    .map((item) => `${item.label}: ${stageBadgeText(item.stage)}`)
    .join(". ");
}

export function PreflightBanner() {
  const browserMock = isMockMode();
  const [report, setReport] = useState<PreflightReport | null>(null);
  const [setup, setSetup] = useState<ToolSetupStatus | null>(null);
  const [requestError, setRequestError] = useState<string | null>(null);
  const [pollLost, setPollLost] = useState(false);
  const [pollAttempt, setPollAttempt] = useState(0);
  const [starting, setStarting] = useState(false);
  const [completedInstall, setCompletedInstall] = useState(false);
  const [dismissed, setDismissed] = useState(false);

  const mountedRef = useRef(true);
  const userStartedRef = useRef(false);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  useEffect(() => {
    if (browserMock) return;
    let cancelled = false;
    const api = getApiClient();
    api
      .getPreflight()
      .then((result) => {
        if (!cancelled) setReport(result);
      })
      .catch(() => {
        // Ignore reachability or older-backend errors here; form submission reports them.
      });
    const initialSetup = api.getToolSetup?.();
    if (initialSetup) {
      initialSetup
        .then((result) => {
          if (
            !cancelled &&
            !userStartedRef.current &&
            result &&
            result.state !== "idle"
          ) {
            setSetup(result);
          }
        })
        .catch(() => {
          // Older backend without /tools/setup: keep preflight banner working normally.
        });
    }
    return () => {
      cancelled = true;
    };
  }, [browserMock]);

  const setupState = setup?.state ?? null;

  useEffect(() => {
    if (browserMock || setupState !== "running" || pollLost) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let failures = 0;
    let delayMs = SETUP_POLL_BASE_MS;
    const api = getApiClient();

    function scheduleNext(waitMs: number) {
      if (cancelled) return;
      timer = setTimeout(() => {
        void poll();
      }, waitMs);
    }

    async function poll() {
      const pending = api.getToolSetup?.();
      if (!pending) return;
      try {
        const next = await pending;
        if (cancelled || !next) return;
        failures = 0;
        delayMs = SETUP_POLL_BASE_MS;
        setSetup(next);
        if (next.state === "done" || next.state === "failed") {
          if (next.state === "done") {
            setCompletedInstall(true);
          }
          // Gate this refresh on component mount (not this effect's `cancelled`),
          // because `setSetup(next)` immediately cleans up this polling effect.
          api
            .getPreflight()
            .then((fresh) => {
              if (mountedRef.current) setReport(fresh);
            })
            .catch(() => {
              // Ignore refresh failure; user still sees the setup outcome.
            });
          return;
        }
        scheduleNext(SETUP_POLL_BASE_MS);
      } catch {
        if (cancelled) return;
        failures += 1;
        if (failures >= SETUP_POLL_MAX_FAILURES) {
          setPollLost(true);
          setRequestError(
            "Lost connection to the AutoClipper server while checking tool setup. Check that the server is running and retry.",
          );
          return;
        }
        const wait = delayMs;
        delayMs = Math.min(delayMs * 2, SETUP_POLL_MAX_BACKOFF_MS);
        scheduleNext(wait);
      }
    }

    scheduleNext(SETUP_POLL_BASE_MS);
    return () => {
      cancelled = true;
      if (timer !== undefined) clearTimeout(timer);
    };
  }, [browserMock, setupState, pollLost, pollAttempt]);

  useEffect(() => {
    if (!dismissed) return;
    // The dismiss control unmounts with the banner, so put focus on the form
    // the user is about to fill in.
    document.querySelector<HTMLInputElement>('input[name="vod"]')?.focus();
  }, [dismissed]);

  if (browserMock || dismissed) return null;

  const problems = problemChecks(report);
  const missingTools = installableMissingTools(problems);
  const running = starting || (setup?.state === "running" && !pollLost);
  const setupError =
    requestError ??
    (setup?.state === "failed"
      ? (setup.error?.message ??
        "Tool download failed. Try again or install manually.")
      : null);
  const hasFailure = problems.some((check) => check.status === "fail");
  const showCompletedBanner =
    completedInstall && problems.length === 0 && !running;
  const visible = problems.length > 0 || running || showCompletedBanner;

  async function handleActionClick() {
    if (running) return;
    const api = getApiClient();
    setRequestError(null);

    if (pollLost) {
      setStarting(true);
      try {
        const current = await api.getToolSetup();
        if (!mountedRef.current) return;
        setPollLost(false);
        setSetup(current);
        if (current.state === "running") {
          setPollAttempt((n) => n + 1);
          return;
        }
        if (current.state === "done") {
          setCompletedInstall(true);
        }
        const fresh = await api.getPreflight().catch(() => null);
        if (mountedRef.current && fresh) setReport(fresh);
      } catch (error) {
        if (mountedRef.current) {
          setRequestError(
            error instanceof Error
              ? error.message
              : "Could not reach the AutoClipper server. Check that it is running and retry.",
          );
        }
      } finally {
        if (mountedRef.current) setStarting(false);
      }
      return;
    }

    userStartedRef.current = true;
    setPollLost(false);
    setStarting(true);
    try {
      const targetTools = missingTools.length > 0 ? missingTools : undefined;
      const { setup: next } = await api.startToolSetup(
        targetTools ? { tools: targetTools } : undefined,
      );
      if (!mountedRef.current) return;
      setSetup(next);
      if (next.state === "done") {
        setCompletedInstall(true);
        const fresh = await api.getPreflight().catch(() => null);
        if (mountedRef.current && fresh) setReport(fresh);
      }
    } catch (error) {
      if (mountedRef.current) {
        setRequestError(
          error instanceof Error
            ? error.message
            : "Could not start tool download. Try again or run `uv run autoclipper setup-tools`.",
        );
      }
    } finally {
      if (mountedRef.current) setStarting(false);
    }
  }

  function handleKeyDown(event: KeyboardEvent<HTMLElement>) {
    if (event.key === "Escape") {
      setDismissed(true);
    }
  }

  if (showCompletedBanner) {
    return (
      <section
        role="status"
        aria-live="polite"
        aria-atomic="true"
        onKeyDown={handleKeyDown}
        className={`${BANNER_BASE} border-success-border`}
      >
        <span aria-hidden="true" className={`${EDGE} bg-success`} />
        <div className="flex items-start justify-between gap-4">
          <div className="flex flex-col gap-1">
            <h2 className="font-display text-base font-semibold text-success-soft-ink">
              Managed tools installed
            </h2>
            <p className="text-ink-secondary">
              Pinned FFmpeg/FFprobe and TwitchDownloaderCLI builds were verified
              and installed. AutoClipper is ready.
            </p>
          </div>
          <button
            type="button"
            onClick={() => setDismissed(true)}
            aria-label="Dismiss setup notice"
            className={DISMISS_CLASS}
          >
            Dismiss
          </button>
        </div>
      </section>
    );
  }

  const showInstallPanel =
    missingTools.length > 0 || running || setupError !== null || pollLost;
  const announcement = stageAnnouncement(setup);

  // The polite region is mounted before the request resolves so a warning-only
  // result is announced as a change to an existing live region.
  if (!hasFailure) {
    return (
      <div
        onKeyDown={handleKeyDown}
        className={visible ? `${BANNER_BASE} border-signal-border` : undefined}
      >
        {visible && <span aria-hidden="true" className={`${EDGE} bg-signal`} />}
        <section
          role="status"
          aria-live="polite"
          aria-atomic="true"
          className={visible ? undefined : "sr-only"}
        >
          {visible && (
            <BannerHeader
              tone="warn"
              title="Optional setup items missing"
              summary="AutoClipper can run, but some optional features are currently unavailable."
              onDismiss={() => setDismissed(true)}
            />
          )}
        </section>
        {visible && showInstallPanel && (
          <InstallPanel
            missingTools={missingTools}
            setup={setup}
            running={running}
            setupError={setupError}
            pollLost={pollLost}
            announcement={announcement}
            onActionClick={handleActionClick}
          />
        )}
        {visible && (
          <BannerChecklist
            problems={problems}
            completedInstall={completedInstall}
            running={running}
            setupError={setupError}
          />
        )}
      </div>
    );
  }

  // Keep the assertive alert limited to static readiness status so live download
  // byte-count updates in InstallPanel never interrupt screen readers.
  return (
    <div
      onKeyDown={handleKeyDown}
      className={`${BANNER_BASE} border-danger-border`}
    >
      <span aria-hidden="true" className={`${EDGE} bg-danger`} />
      <section role="alert" aria-atomic="true">
        <BannerHeader
          tone="fail"
          title="Setup issues need attention"
          summary="Some required local tools or credentials are missing before real VOD analysis can run."
          onDismiss={() => setDismissed(true)}
        />
      </section>
      {showInstallPanel && (
        <InstallPanel
          missingTools={missingTools}
          setup={setup}
          running={running}
          setupError={setupError}
          pollLost={pollLost}
          announcement={announcement}
          onActionClick={handleActionClick}
        />
      )}
      <BannerChecklist
        problems={problems}
        completedInstall={completedInstall}
        running={running}
        setupError={setupError}
      />
    </div>
  );
}

function BannerHeader({
  tone,
  title,
  summary,
  onDismiss,
}: {
  tone: "warn" | "fail";
  title: string;
  summary: string;
  onDismiss: () => void;
}) {
  const fail = tone === "fail";
  return (
    <div className="flex items-start justify-between gap-4">
      <div className="flex flex-col gap-1">
        <h2
          className={`font-display text-base font-semibold ${
            fail ? "text-danger-soft-ink" : "text-signal-soft-ink"
          }`}
        >
          {title}
        </h2>
        <p className="text-ink-secondary">{summary}</p>
      </div>
      <button
        type="button"
        onClick={onDismiss}
        aria-label="Dismiss setup notice"
        className={DISMISS_CLASS}
      >
        Dismiss
      </button>
    </div>
  );
}

function InstallPanel({
  missingTools,
  setup,
  running,
  setupError,
  pollLost,
  announcement,
  onActionClick,
}: {
  missingTools: ToolSetupName[];
  setup: ToolSetupStatus | null;
  running: boolean;
  setupError: string | null;
  pollLost: boolean;
  announcement: string;
  onActionClick: () => void;
}) {
  const items = setup?.items ?? [];
  return (
    <div className="mt-4 rounded-card border border-border-subtle bg-surface-elevated p-4 text-ink-primary">
      <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
        <div className="flex flex-col gap-0.5">
          <p className="font-semibold text-ink-primary">
            Automatic local tool setup
          </p>
          <p className="text-xs text-ink-secondary">
            Downloads pinned upstream builds (FFmpeg/FFprobe GPL-3.0,
            TwitchDownloaderCLI MIT) into your AutoClipper tools folder with
            SHA-256 verification.
          </p>
        </div>
        <Button
          variant="primary"
          size="sm"
          onClick={onActionClick}
          disabled={running}
          className="shrink-0 self-start sm:self-center"
        >
          {downloadButtonLabel(
            missingTools,
            running,
            setupError !== null,
            pollLost,
          )}
        </Button>
      </div>

      {/* Stage-only polite announcement so byte-count ticks stay quiet. */}
      <p className="sr-only" role="status" aria-live="polite">
        {announcement}
      </p>

      {items.length > 0 && (
        <div className="mt-3 flex flex-col gap-2 border-t border-border-subtle pt-2.5">
          {items.map((item) => (
            <ToolProgressRow key={item.tool} item={item} />
          ))}
        </div>
      )}

      {setupError && (
        <p
          role="alert"
          className="mt-3 rounded-control border border-danger-border bg-danger-soft px-3 py-2 text-xs leading-5 text-danger-soft-ink"
        >
          {setupError}
        </p>
      )}
    </div>
  );
}

function BannerChecklist({
  problems,
  completedInstall,
  running,
  setupError,
}: {
  problems: PreflightCheck[];
  completedInstall: boolean;
  running: boolean;
  setupError: string | null;
}) {
  return (
    <>
      {completedInstall && !running && !setupError && problems.length > 0 && (
        <p className="mt-3 rounded-control border border-success-border bg-success-soft px-3 py-2 text-xs leading-5 text-success-soft-ink">
          Managed tools installed. Complete the remaining items below to finish
          setup.
        </p>
      )}

      <ul className="mt-4 flex flex-col divide-y divide-border-subtle border-t border-border-subtle">
        {problems.map((check) => {
          const isFail = check.status === "fail";
          return (
            <li key={check.name} className="py-3 text-ink-primary">
              <div className="flex flex-wrap items-center gap-2">
                <Badge tone={isFail ? "danger" : "signal"}>
                  {isFail ? "Required" : "Warning"}
                </Badge>
                <span className="font-semibold">{check.label}</span>
              </div>
              <p className="mt-1 text-ink-secondary">{check.message}</p>
              {check.fix && (
                <p className="mt-2 rounded-control border border-border-subtle bg-surface-canvas/70 px-2.5 py-1.5 font-mono text-xs break-words text-ink-secondary">
                  Fix: {check.fix}
                </p>
              )}
            </li>
          );
        })}
      </ul>
    </>
  );
}

function ToolProgressRow({ item }: { item: ToolSetupItem }) {
  const pct = item.fraction !== null ? Math.round(item.fraction * 100) : null;
  const byteText =
    item.bytes_downloaded !== null && item.bytes_total !== null
      ? `${formatMegabytes(item.bytes_downloaded)} / ${formatMegabytes(item.bytes_total)}`
      : null;
  const active =
    item.stage === "downloading" ||
    item.stage === "verifying" ||
    item.stage === "extracting" ||
    item.stage === "checking";

  return (
    <div className="flex flex-col gap-1 text-xs">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="font-medium text-ink-primary">{item.label}</span>
        <span className="font-mono text-ink-secondary tabular-nums">
          {stageBadgeText(item.stage)}
          {pct !== null ? ` (${pct}%)` : ""}
          {byteText ? ` · ${byteText}` : ""}
        </span>
      </div>
      <p className="text-ink-secondary">{item.message}</p>
      {(active || item.stage === "done") && (
        <div
          role="progressbar"
          aria-label={`${item.label} setup progress`}
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={pct ?? undefined}
          className="h-1.5 w-full overflow-hidden rounded-pill bg-surface-subtle"
        >
          <div
            className={
              item.stage === "done"
                ? "h-full rounded-pill bg-source-both transition-all"
                : pct !== null
                  ? "h-full rounded-pill bg-accent transition-all"
                  : "h-full w-1/3 rounded-pill bg-accent motion-safe:animate-pulse"
            }
            style={
              item.stage === "done"
                ? { width: "100%" }
                : pct !== null
                  ? { width: `${pct}%` }
                  : undefined
            }
          />
        </div>
      )}
    </div>
  );
}
