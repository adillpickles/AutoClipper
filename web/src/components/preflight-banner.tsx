"use client";

import { useEffect, useState, type KeyboardEvent } from "react";

import { getApiClient, isMockMode } from "@/lib/api/client";
import type { PreflightCheck, PreflightReport } from "@/lib/api/types";

function problemChecks(report: PreflightReport | null): PreflightCheck[] {
  if (!report) return [];
  const problems = report.checks.filter((check) => check.status !== "ok");
  return problems.sort((a, b) => {
    if (a.status === b.status) return 0;
    return a.status === "fail" ? -1 : 1;
  });
}

export function PreflightBanner() {
  const browserMock = isMockMode();
  const [report, setReport] = useState<PreflightReport | null>(null);
  const [dismissed, setDismissed] = useState(false);

  useEffect(() => {
    if (browserMock) return;
    let cancelled = false;
    getApiClient()
      .getPreflight()
      .then((result) => {
        if (!cancelled) setReport(result);
      })
      .catch(() => {
        // Ignore reachability or older-backend errors here; form submission reports them.
      });
    return () => {
      cancelled = true;
    };
  }, [browserMock]);

  useEffect(() => {
    if (!dismissed) return;
    // The dismiss control unmounts with the banner, so put focus on the form
    // the user is about to fill in.
    document.querySelector<HTMLInputElement>('input[name="vod"]')?.focus();
  }, [dismissed]);

  if (browserMock || dismissed) return null;

  const problems = problemChecks(report);
  const hasFailure = problems.some((check) => check.status === "fail");
  const visible = problems.length > 0;

  function handleKeyDown(event: KeyboardEvent<HTMLElement>) {
    if (event.key === "Escape") {
      setDismissed(true);
    }
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
  onDismiss,
}: {
  tone: "warn" | "fail";
  title: string;
  summary: string;
  problems: PreflightCheck[];
  onDismiss: () => void;
}) {
  const fail = tone === "fail";
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
                <p className="mt-1.5 font-mono text-xs text-ink-tertiary">
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
