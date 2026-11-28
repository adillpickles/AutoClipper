"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { Suspense, useEffect, useState } from "react";

import { getApiClient, isMockMode } from "@/lib/api/client";

type RunnerBadge = "mock" | "pipeline" | null;

const NAV_ITEMS = [
  { href: "/", label: "Single VOD", match: (path: string) => path === "/" },
  {
    href: "/channels",
    label: "Channels",
    match: (path: string) =>
      path === "/channels" || path.startsWith("/channels/"),
  },
  {
    href: "/library",
    label: "Library",
    match: (path: string) =>
      path === "/library" || path.startsWith("/library/"),
  },
  {
    href: "/settings",
    label: "Settings",
    match: (path: string) => path === "/settings",
  },
] as const;

function NavLinks({
  pathname,
  variant,
}: {
  pathname: string;
  variant: "desktop" | "mobile";
}) {
  if (variant === "desktop") {
    return (
      <nav
        aria-label="Primary"
        className="hidden items-center gap-1 rounded-pill border border-border-subtle bg-surface-panel p-1 sm:inline-flex"
      >
        {NAV_ITEMS.map((item) => {
          const active = pathname !== "" && item.match(pathname);
          return (
            <Link
              key={item.href}
              href={item.href}
              aria-current={active ? "page" : undefined}
              className={`inline-flex min-h-8 items-center rounded-pill px-3 py-1 text-xs font-semibold transition-colors focus-visible:ring-2 focus-visible:ring-focus-ring focus-visible:outline-none ${
                active
                  ? "bg-accent text-accent-ink shadow-card"
                  : "text-ink-secondary hover:bg-surface-elevated hover:text-ink-primary"
              }`}
            >
              {item.label}
            </Link>
          );
        })}
      </nav>
    );
  }

  return (
    <nav
      aria-label="Primary mobile"
      className="flex w-full items-center gap-1 rounded-control border border-border-subtle bg-surface-panel p-1 sm:hidden"
    >
      {NAV_ITEMS.map((item) => {
        const active = pathname !== "" && item.match(pathname);
        return (
          <Link
            key={item.href}
            href={item.href}
            aria-current={active ? "page" : undefined}
            className={`inline-flex min-h-9 flex-1 items-center justify-center rounded-control px-3 py-1.5 text-xs font-semibold transition-colors focus-visible:ring-2 focus-visible:ring-focus-ring focus-visible:outline-none ${
              active
                ? "bg-accent text-accent-ink shadow-card"
                : "text-ink-secondary hover:bg-surface-elevated hover:text-ink-primary"
            }`}
          >
            {item.label}
          </Link>
        );
      })}
    </nav>
  );
}

function ActiveNavLinks({ variant }: { variant: "desktop" | "mobile" }) {
  const pathname = usePathname() ?? "/";
  return <NavLinks pathname={pathname} variant={variant} />;
}

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
    <header className="flex flex-wrap items-center justify-between gap-x-4 gap-y-3 border-b border-border-subtle py-4 sm:flex-nowrap sm:py-5">
      <div className="flex min-w-0 items-center gap-4 sm:gap-6">
        <Link
          href="/"
          aria-label="AutoClipper home"
          className="group inline-flex min-h-10 min-w-0 items-center gap-2.5 rounded-control font-display text-lg font-bold tracking-tight text-ink-primary transition-colors hover:text-accent focus-visible:ring-2 focus-visible:ring-focus-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface-canvas focus-visible:outline-none"
        >
          <span
            aria-hidden="true"
            className="flex size-8 shrink-0 items-center justify-center rounded-control bg-accent text-accent-ink shadow-card transition-transform motion-safe:group-hover:scale-[1.03]"
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
          <span className="truncate">AutoClipper</span>
        </Link>

        <Suspense fallback={<NavLinks pathname="" variant="desktop" />}>
          <ActiveNavLinks variant="desktop" />
        </Suspense>
      </div>

      {badge === "mock" && (
        <span className="inline-flex shrink-0 items-center gap-1.5 rounded-pill border border-signal-border bg-signal-soft px-3 py-1 font-mono text-xs font-medium text-signal-soft-ink">
          <span
            aria-hidden="true"
            className="size-1.5 rounded-full bg-signal"
          />
          Mock data
        </span>
      )}

      <Suspense fallback={<NavLinks pathname="" variant="mobile" />}>
        <ActiveNavLinks variant="mobile" />
      </Suspense>
    </header>
  );
}
