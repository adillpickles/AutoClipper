"use client";

import { useEffect, useRef, useState, type KeyboardEvent } from "react";

import { Button } from "@/components/ui/button";
import { getApiClient, isMockMode } from "@/lib/api/client";
import type {
  PreflightCheck,
  PreflightReport,
  ToolSetupItem,
  ToolSetupName,
  ToolSetupStatus,
} from "@/lib/api/types";

const SETUP_POLL_INTERVAL_MS = 1000;

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
): string {
  if (running) return "Downloading tools...";
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

export function PreflightBanner() {
  const browserMock = isMockMode();
  const [report, setReport] = useState<PreflightReport | null>(null);
  const [setup, setSetup] = useState<ToolSetupStatus | null>(null);
  const [requestError, setRequestError] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);
  const [completedInstall, setCompletedInstall] = useState(false);
  const [dismissed, setDismissed] = useState(false);
  const pollInFlight = useRef(false);

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
          if (!cancelled && result && result.state !== "idle") {
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
    if (browserMock || setupState !== "running") return;
    let cancelled = false;
    const api = getApiClient();

    const poll = () => {
      if (pollInFlight.current) return;
      const pending = api.getToolSetup?.();
      if (!pending) return;
      pollInFlight.current = true;
      pending
        .then((next) => {
          if (cancelled || !next) return;
          setSetup(next);
          if (next.state === "done" || next.state === "failed") {
            if (next.state === "done") {
              setCompletedInstall(true);
            }
            api
              .getPreflight()
              .then((fresh) => {
                if (!cancelled) setReport(fresh);
              })
              .catch(() => {
                // Ignore refresh failure; user still sees the setup outcome.
              });
          }
        })
        .catch(() => {
          // Transient poll failure: keep trying on the next tick.
        })
        .finally(() => {
          pollInFlight.current = false;
        });
    };

    const timer = window.setInterval(poll, SETUP_POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      pollInFlight.current = false;
      window.clearInterval(timer);
    };
  }, [browserMock, setupState]);

  useEffect(() => {
    if (!dismissed) return;
    // The dismiss control unmounts with the banner, so put focus on the form
    // the user is about to fill in.
    document.querySelector<HTMLInputElement>('input[name="vod"]')?.focus();
  }, [dismissed]);

  if (browserMock || dismissed) return null;

  const problems = problemChecks(report);
  const missingTools = installableMissingTools(problems);
  const running = starting || setup?.state === "running";
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

  async function handleStartSetup() {
    if (running) return;
    setRequestError(null);
    setStarting(true);
    try {
      const api = getApiClient();
      const targetTools = missingTools.length > 0 ? missingTools : undefined;
      const { setup: next } = await api.startToolSetup(
        targetTools ? { tools: targetTools } : undefined,
      );
      setSetup(next);
      if (next.state === "done") {
        setCompletedInstall(true);
        const fresh = await api.getPreflight().catch(() => null);
        if (fresh) setReport(fresh);
      }
    } catch (error) {
      setRequestError(
        error instanceof Error
          ? error.message
          : "Could not start tool download. Try again or run `uv run autoclipper setup-tools`.",
      );
    } finally {
      setStarting(false);
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
        className="rounded-card border border-source-both-border bg-source-both-soft p-4 text-sm text-source-both-soft-ink shadow-card"
      >
        <div className="flex items-start justify-between gap-4">
          <div className="flex flex-col gap-1">
            <h2 className="font-display text-base font-semibold">
              Managed tools installed
            </h2>
            <p>
              Pinned FFmpeg/FFprobe and TwitchDownloaderCLI builds were verified
              and installed. AutoClipper is ready.
            </p>
          </div>
          <button
            type="button"
            onClick={() => setDismissed(true)}
            aria-label="Dismiss setup notice"
            className="shrink-0 rounded-control border border-source-both-border bg-surface-panel px-2.5 py-1 text-xs font-medium text-source-both-soft-ink hover:bg-surface-elevated focus-visible:ring-2 focus-visible:ring-focus-ring focus-visible:ring-offset-2 focus-visible:ring-offset-source-both-soft focus-visible:outline-none"
          >
            Dismiss
          </button>
        </div>
      </section>
    );
  }

  // The polite region is mounted before the request resolves so a warning-only
  // result is announced as a change to an existing live region.
  if (!hasFailure) {
    return (
      <section
        role="status"
        aria-live="polite"
        aria-atomic="true"
        onKeyDown={handleKeyDown}
        className={
          visible
            ? "rounded-card border border-signal-border bg-signal-soft p-4 text-sm text-signal-soft-ink shadow-card"
            : "sr-only"
        }
      >
        {visible && (
          <BannerBody
            tone="warn"
            title="Optional setup items missing"
            summary="AutoClipper can run, but some optional features are currently unavailable."
            problems={problems}
            missingTools={missingTools}
            setup={setup}
            running={running}
            setupError={setupError}
            completedInstall={completedInstall}
            onStartSetup={handleStartSetup}
            onDismiss={() => setDismissed(true)}
          />
        )}
      </section>
    );
  }

  return (
    <section
      role="alert"
      aria-atomic="true"
      onKeyDown={handleKeyDown}
      className="rounded-card border border-danger-border bg-danger-soft p-4 text-sm text-danger-soft-ink shadow-card"
    >
      <BannerBody
        tone="fail"
        title="Setup issues need attention"
        summary="Some required local tools or credentials are missing before real VOD analysis can run."
        problems={problems}
        missingTools={missingTools}
        setup={setup}
        running={running}
        setupError={setupError}
        completedInstall={completedInstall}
        onStartSetup={handleStartSetup}
        onDismiss={() => setDismissed(true)}
      />
    </section>
  );
}

function BannerBody({
  tone,
  title,
  summary,
  problems,
  missingTools,
  setup,
  running,
  setupError,
  completedInstall,
  onStartSetup,
  onDismiss,
}: {
  tone: "warn" | "fail";
  title: string;
  summary: string;
  problems: PreflightCheck[];
  missingTools: ToolSetupName[];
  setup: ToolSetupStatus | null;
  running: boolean;
  setupError: string | null;
  completedInstall: boolean;
  onStartSetup: () => void;
  onDismiss: () => void;
}) {
  const fail = tone === "fail";
  const showInstallPanel =
    missingTools.length > 0 || running || setupError !== null;
  const items = setup?.items ?? [];

  return (
    <>
      <div className="flex items-start justify-between gap-4">
        <div className="flex flex-col gap-1">
          <h2 className="font-display text-base font-semibold">{title}</h2>
          <p>{summary}</p>
        </div>
        <button
          type="button"
          onClick={onDismiss}
          aria-label="Dismiss setup notice"
          className={`shrink-0 rounded-control border bg-surface-panel px-2.5 py-1 text-xs font-medium focus-visible:ring-2 focus-visible:ring-focus-ring focus-visible:ring-offset-2 focus-visible:outline-none ${
            fail
              ? "border-danger-border text-danger-soft-ink ring-offset-danger-soft hover:bg-surface-elevated"
              : "border-signal-border text-signal-soft-ink ring-offset-signal-soft hover:bg-surface-elevated"
          }`}
        >
          Dismiss
        </button>
      </div>

      {showInstallPanel && (
        <div className="mt-3 rounded-control border border-border-subtle bg-surface-panel p-3 text-ink-primary">
          <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
            <div className="flex flex-col gap-0.5">
              <p className="font-medium text-ink-primary">
                Automatic local tool setup
              </p>
              <p className="text-xs text-ink-secondary">
                Downloads pinned upstream builds (FFmpeg/FFprobe GPL-3.0,
                TwitchDownloaderCLI MIT) into your AutoClipper tools folder with
                SHA-256 verification.
              </p>
            </div>
            {(missingTools.length > 0 || running || setupError !== null) && (
              <Button
                variant="primary"
                size="sm"
                onClick={onStartSetup}
                disabled={running}
                className="shrink-0 self-start sm:self-center"
              >
                {downloadButtonLabel(
                  missingTools,
                  running,
                  setupError !== null,
                )}
              </Button>
            )}
          </div>

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
              className="mt-2.5 rounded-control border border-danger-border bg-danger-soft px-3 py-2 text-xs text-danger-soft-ink"
            >
              {setupError}
            </p>
          )}
        </div>
      )}

      {completedInstall && !running && !setupError && problems.length > 0 && (
        <p className="mt-2.5 rounded-control border border-source-both-border bg-source-both-soft px-3 py-2 text-xs text-source-both-soft-ink">
          Managed tools installed. Complete the remaining items below to finish
          setup.
        </p>
      )}

      <ul className="mt-3 flex flex-col gap-2.5">
        {problems.map((check) => {
          const isFail = check.status === "fail";
          return (
            <li
              key={check.name}
              className="rounded-control bg-surface-panel px-3 py-2.5 text-ink-primary"
            >
              <div className="flex flex-wrap items-center gap-2">
                <span
                  className={
                    isFail
                      ? "rounded-pill bg-danger px-2 py-0.5 text-xs font-semibold text-danger-ink"
                      : "rounded-pill border border-signal-border bg-signal-soft px-2 py-0.5 text-xs font-semibold text-signal-soft-ink"
                  }
                >
                  {isFail ? "Required" : "Warning"}
                </span>
                <span className="font-medium">{check.label}</span>
              </div>
              <p className="mt-1 text-ink-secondary">{check.message}</p>
              {check.fix && (
                <p className="mt-1.5 font-mono text-xs break-words text-ink-tertiary">
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
        <span className="font-mono text-ink-secondary">
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
                  : "h-full w-1/3 animate-pulse rounded-pill bg-accent"
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
