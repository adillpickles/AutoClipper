"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { getApiClient, isMockMode } from "@/lib/api/client";

type RunnerBadge = "mock" | "pipeline" | null;

export function SiteHeader() {
  const browserMock = isMockMode();
  const [badge, setBadge] = useState<RunnerBadge>(browserMock ? "mock" : null);

  useEffect(() => {
    if (browserMock) return;
    let cancelled = false;
    getApiClient()
      .getHealth()
      .then((health) => {
        if (!cancelled) setBadge(health.job_runner);
      })
      .catch(() => {
        // The active screen handles server reachability errors.
      });
    return () => {
      cancelled = true;
    };
  }, [browserMock]);

  return (
    <header className="flex items-center justify-between gap-4 border-b border-border-subtle py-5">
      <Link
        href="/"
        className="group inline-flex items-center gap-2.5 rounded-control font-display text-lg font-bold tracking-tight text-ink-primary transition-colors hover:text-accent focus-visible:ring-2 focus-visible:ring-focus-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface-canvas focus-visible:outline-none"
      >
        <span
          aria-hidden="true"
          className="flex size-8 items-center justify-center rounded-control bg-accent text-accent-ink shadow-card transition-transform group-hover:scale-[1.03]"
        >
          <svg
            viewBox="0 0 24 24"
            fill="none"
            className="size-4"
            stroke="currentColor"
            strokeWidth="2.2"
            strokeLinecap="round"
            strokeLinejoin="round"
          >
            <rect x="3" y="5" width="18" height="14" rx="3" />
            <path
              d="M10 9.5l5 2.5-5 2.5V9.5z"
              fill="currentColor"
              stroke="none"
            />
          </svg>
        </span>
        <span>AutoClipper</span>
      </Link>
      {badge === "mock" && (
        <span className="inline-flex items-center gap-1.5 rounded-pill border border-signal-border bg-signal-soft px-3 py-1 font-mono text-xs font-medium text-signal-soft-ink">
          <span
            aria-hidden="true"
            className="size-1.5 rounded-full bg-signal"
          />
          Mock data
        </span>
      )}
    </header>
  );
}
