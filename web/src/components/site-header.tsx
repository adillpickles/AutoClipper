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
    <header className="flex items-center justify-between gap-4 py-6">
      <Link href="/" className="text-lg font-semibold tracking-tight">
        AutoClipper
      </Link>
      {badge === "mock" && (
        <span className="rounded-full bg-amber-100 px-3 py-1 text-xs font-medium text-amber-900 dark:bg-amber-400/15 dark:text-amber-200">
          Mock data
        </span>
      )}
    </header>
  );
}
