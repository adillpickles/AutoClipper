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

  const problems = problemChecks(report);
  if (dismissed || problems.length === 0) return null;

  const hasFailure = problems.some((check) => check.status === "fail");

  function handleKeyDown(event: KeyboardEvent<HTMLElement>) {
    if (event.key === "Escape") {
      setDismissed(true);
    }
  }

  return (
    <section
      role={hasFailure ? "alert" : "status"}
      aria-live={hasFailure ? "assertive" : "polite"}
      onKeyDown={handleKeyDown}
      className={
        hasFailure
          ? "rounded-xl border border-red-300 bg-red-50/90 p-4 text-sm text-red-950 dark:border-red-900 dark:bg-red-950/40 dark:text-red-100"
          : "rounded-xl border border-amber-300 bg-amber-50/90 p-4 text-sm text-amber-950 dark:border-amber-800/80 dark:bg-amber-950/35 dark:text-amber-100"
      }
    >
      <div className="flex items-start justify-between gap-4">
        <div className="flex flex-col gap-1">
          <h2 className="font-semibold">
            {hasFailure
              ? "Setup issues need attention"
              : "Optional setup items missing"}
          </h2>
          <p
            className={
              hasFailure
                ? "text-red-900/90 dark:text-red-200/90"
                : "text-amber-900/90 dark:text-amber-200/90"
            }
          >
            {hasFailure
              ? "Some required local tools or credentials are missing before real VOD analysis can run."
              : "AutoClipper can run, but some optional features are currently unavailable."}
          </p>
        </div>
        <button
          type="button"
          onClick={() => setDismissed(true)}
          aria-label="Dismiss setup notice"
          className={
            hasFailure
              ? "shrink-0 rounded-md border border-red-300/80 px-2.5 py-1 text-xs font-medium text-red-900 hover:bg-red-100 focus-visible:ring-2 focus-visible:ring-red-600 focus-visible:outline-none dark:border-red-800 dark:text-red-100 dark:hover:bg-red-900/50 dark:focus-visible:ring-red-400"
              : "shrink-0 rounded-md border border-amber-300/80 px-2.5 py-1 text-xs font-medium text-amber-900 hover:bg-amber-100 focus-visible:ring-2 focus-visible:ring-amber-600 focus-visible:outline-none dark:border-amber-700/80 dark:text-amber-100 dark:hover:bg-amber-900/50 dark:focus-visible:ring-amber-400"
          }
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
              className="rounded-lg bg-white/75 px-3 py-2.5 dark:bg-zinc-950/45"
            >
              <div className="flex flex-wrap items-center gap-2">
                <span
                  className={
                    isFail
                      ? "rounded-full bg-red-100 px-2 py-0.5 text-xs font-semibold text-red-900 dark:bg-red-900/70 dark:text-red-100"
                      : "rounded-full bg-amber-100 px-2 py-0.5 text-xs font-semibold text-amber-900 dark:bg-amber-900/70 dark:text-amber-100"
                  }
                >
                  {isFail ? "Required" : "Warning"}
                </span>
                <span className="font-medium text-zinc-950 dark:text-zinc-100">
                  {check.label}
                </span>
              </div>
              <p className="mt-1 text-zinc-800 dark:text-zinc-300">
                {check.message}
              </p>
              {check.fix && (
                <p className="mt-1.5 font-mono text-xs text-zinc-700 dark:text-zinc-300">
                  Fix: {check.fix}
                </p>
              )}
            </li>
          );
        })}
      </ul>
    </section>
  );
}
