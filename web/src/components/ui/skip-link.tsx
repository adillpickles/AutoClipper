"use client";

import type { MouseEvent } from "react";

export function SkipLink({ targetId = "main-content" }: { targetId?: string }) {
  function handleClick(event: MouseEvent<HTMLAnchorElement>) {
    const target =
      document.getElementById(targetId) ?? document.querySelector("main");
    if (!target) return;
    event.preventDefault();
    if (!target.hasAttribute("tabindex")) {
      target.setAttribute("tabindex", "-1");
    }
    target.focus();
  }

  return (
    <a
      href={`#${targetId}`}
      onClick={handleClick}
      className="sr-only focus:not-sr-only focus:fixed focus:top-3 focus:left-3 focus:z-50 focus:rounded-control focus:bg-accent focus:px-4 focus:py-2.5 focus:text-sm focus:font-semibold focus:text-accent-ink focus:shadow-panel focus:ring-2 focus:ring-focus-ring focus:ring-offset-2 focus:ring-offset-surface-canvas focus:outline-none"
    >
      Skip to main content
    </a>
  );
}
