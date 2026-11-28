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
  const desktop = variant === "desktop";
  return (
    <nav
      aria-label={desktop ? "Primary" : "Primary mobile"}
      className={
        desktop
          ? "flex items-stretch gap-1"
          : "-mx-1 flex items-stretch overflow-x-auto"
      }
    >
      {NAV_ITEMS.map((item) => {
        const active = pathname !== "" && item.match(pathname);
        return (
          <Link
            key={item.href}
            href={item.href}
            aria-current={active ? "page" : undefined}
            className={`relative inline-flex items-center justify-center rounded-control font-semibold whitespace-nowrap transition-colors after:absolute after:inset-x-2 after:-bottom-px after:h-0.5 after:rounded-pill after:transition-colors focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-focus-ring ${
              desktop
                ? "min-h-16 px-3 text-sm"
                : "min-h-11 flex-1 px-1.5 text-[0.8125rem]"
            } ${
              active
                ? "text-ink-primary after:bg-accent"
                : "text-ink-tertiary after:bg-transparent hover:text-ink-primary"
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
    <header className="flex flex-wrap items-center border-b border-border-subtle sm:flex-nowrap sm:items-stretch">
      <Link
        href="/"
        aria-label="AutoClipper home"
        className="group order-1 my-2 inline-flex min-h-10 min-w-0 items-center gap-2.5 self-center rounded-control font-display text-[1.0625rem] font-bold tracking-[-0.02em] text-ink-primary focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-focus-ring sm:my-3 sm:mr-8"
      >
        <BrandMark />
        <span className="truncate">AutoClipper</span>
      </Link>

      <div className="order-2 hidden sm:flex">
        <Suspense fallback={<NavLinks pathname="" variant="desktop" />}>
          <ActiveNavLinks variant="desktop" />
        </Suspense>
      </div>

      <div className="order-2 ml-auto flex items-center pl-4 sm:order-3">
        {badge === "mock" && <MockBadge />}
      </div>

      <div className="order-4 w-full sm:hidden">
        <Suspense fallback={<NavLinks pathname="" variant="mobile" />}>
          <ActiveNavLinks variant="mobile" />
        </Suspense>
      </div>
    </header>
  );
}

/** The mark shared with the landing page: three chat bars, the middle one peaking. */
function BrandMark() {
  return (
    <svg
      viewBox="0 0 32 32"
      aria-hidden="true"
      className="size-8 shrink-0 transition-transform duration-200 motion-safe:group-hover:-rotate-3"
    >
      <rect width="32" height="32" rx="8" fill="var(--accent)" />
      <rect
        x="7"
        y="15"
        width="4"
        height="10"
        rx="2"
        fill="var(--surface-canvas)"
        opacity=".55"
      />
      <rect
        x="14"
        y="7"
        width="4"
        height="18"
        rx="2"
        fill="var(--surface-canvas)"
      />
      <rect
        x="21"
        y="12"
        width="4"
        height="13"
        rx="2"
        fill="var(--surface-canvas)"
        opacity=".55"
      />
    </svg>
  );
}

function MockBadge() {
  return (
    <span
      title="No server: the app is running on built-in sample data."
      className="inline-flex shrink-0 items-center gap-1.5 rounded-pill border border-signal-border bg-signal-soft px-2.5 py-1 text-xs font-semibold text-signal-soft-ink"
    >
      <span aria-hidden="true" className="size-1.5 rounded-full bg-signal" />
      Mock data
    </span>
  );
}
