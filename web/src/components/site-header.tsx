"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { Suspense, useEffect, useState, useSyncExternalStore } from "react";

import { getApiClient, isMockMode } from "@/lib/api/client";

type RunnerBadge = "mock" | "pipeline" | null;

// The API mode never changes while the page is open.
const subscribeNever = () => () => {};

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
          ? "flex items-stretch gap-0.5"
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
                ? "min-h-16 px-2.5 text-sm"
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

/** `/library/` and `/library` are the same page; only the root keeps its slash. */
export function normalizePath(pathname: string): string {
  const trimmed = pathname.replace(/\/+$/, "");
  return trimmed === "" ? "/" : trimmed;
}

function ActiveNavLinks({ variant }: { variant: "desktop" | "mobile" }) {
  const pathname = normalizePath(usePathname() ?? "/");
  return <NavLinks pathname={pathname} variant={variant} />;
}

/**
 * The app bar. `width` matches the page's content column: "wide" for the results and clip
 * editor screens, "default" (max-w-5xl) everywhere else.
 */
export function SiteHeader({
  width = "default",
}: {
  width?: "default" | "wide";
} = {}) {
  // Read after hydration, never while hydrating: the server render and the browser's first
  // render must match even if their view of the API mode differs (#217).
  const browserMock = useSyncExternalStore(
    subscribeNever,
    isMockMode,
    () => false,
  );
  const [serverBadge, setServerBadge] = useState<RunnerBadge>(null);
  const badge: RunnerBadge = browserMock ? "mock" : serverBadge;

  useEffect(() => {
    if (browserMock) return;
    let cancelled = false;
    getApiClient()
      .getHealth()
      .then((health) => {
        if (!cancelled) setServerBadge(health.job_runner);
      })
      .catch(() => {
        // The active screen handles server reachability errors.
      });
    return () => {
      cancelled = true;
    };
  }, [browserMock]);

  return (
    // Full-bleed: the bar spans the viewport and centers its own content, so it never depends
    // on how wide the page below is (the progress screen is much narrower than results).
    <header className="ml-[calc(50%-50vw)] w-screen border-b border-border-subtle">
      <div
        className={`mx-auto flex flex-wrap items-center px-4 sm:px-8 md:flex-nowrap md:items-stretch ${
          width === "wide" ? "max-w-7xl" : "max-w-5xl"
        }`}
      >
        <Link
          href="/"
          aria-label="AutoClipper home"
          className="group order-1 my-2 inline-flex min-h-10 shrink-0 items-center gap-2.5 self-center rounded-control font-display text-[1.0625rem] font-bold tracking-[-0.02em] whitespace-nowrap text-ink-primary focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-focus-ring md:my-3 md:mr-5"
        >
          <BrandMark />
          <span>AutoClipper</span>
        </Link>

        <div className="order-2 hidden min-w-0 md:flex">
          <Suspense fallback={<NavLinks pathname="" variant="desktop" />}>
            <ActiveNavLinks variant="desktop" />
          </Suspense>
        </div>

        <div className="order-2 ml-auto flex shrink-0 items-center pl-4 md:order-3">
          {badge === "mock" && <MockBadge />}
        </div>

        <div className="order-4 w-full md:hidden">
          <Suspense fallback={<NavLinks pathname="" variant="mobile" />}>
            <ActiveNavLinks variant="mobile" />
          </Suspense>
        </div>
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
