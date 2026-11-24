// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Button, ButtonLink } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/empty-state";
import { ErrorState } from "@/components/ui/error-state";
import {
  ClipEditorSkeleton,
  ClipListSkeleton,
  JobProgressSkeleton,
  Skeleton,
} from "@/components/ui/skeleton";
import { SkipLink } from "@/components/ui/skip-link";

describe("UI primitives", () => {
  afterEach(() => {
    cleanup();
  });

  it("renders Button with default type='button' and ButtonLink with href", () => {
    const onClick = vi.fn();
    render(
      <div>
        <Button onClick={onClick} variant="secondary" size="sm">
          Retry
        </Button>
        <ButtonLink href="/" variant="primary">
          Home
        </ButtonLink>
      </div>,
    );

    const button = screen.getByRole("button", { name: "Retry" });
    expect(button.getAttribute("type")).toBe("button");
    fireEvent.click(button);
    expect(onClick).toHaveBeenCalledTimes(1);

    const link = screen.getByRole("link", { name: "Home" });
    expect(link.getAttribute("href")).toBe("/");
  });

  it("renders accessible reduced-motion-safe loading skeletons", () => {
    const { container } = render(
      <div>
        <Skeleton className="h-4 w-20" />
        <JobProgressSkeleton heading="h1" />
        <ClipListSkeleton count={3} heading="none" />
        <ClipEditorSkeleton heading="none" />
      </div>,
    );

    const statuses = screen.getAllByRole("status");
    expect(statuses).toHaveLength(3);
    for (const status of statuses) {
      expect(status.getAttribute("aria-busy")).toBe("true");
    }
    expect(
      screen.getByRole("heading", {
        level: 1,
        name: "Loading analysis status…",
      }),
    ).toBeTruthy();

    // Every animated skeleton pulse is gated on motion-safe.
    const animated = container.querySelectorAll('[class*="animate-pulse"]');
    expect(animated.length).toBeGreaterThan(0);
    for (const el of animated) {
      expect(el.className).toContain("motion-safe:animate-pulse");
    }
  });

  it("renders EmptyState and ErrorState with recovery actions", () => {
    const onRetry = vi.fn();
    render(
      <div>
        <EmptyState
          title="No moments found"
          description="Nothing stood out in this stream."
          action={<ButtonLink href="/">Try another VOD</ButtonLink>}
        />
        <ErrorState
          alert
          title="Server unreachable"
          message="Check that AutoClipper is running."
          details="code: offline"
          primaryAction={<Button onClick={onRetry}>Retry</Button>}
          secondaryAction={
            <ButtonLink href="/" variant="secondary">
              Back home
            </ButtonLink>
          }
        />
      </div>,
    );

    expect(
      screen.getByRole("heading", { level: 2, name: "No moments found" }),
    ).toBeTruthy();
    expect(
      screen
        .getByRole("link", { name: "Try another VOD" })
        .getAttribute("href"),
    ).toBe("/");

    const alert = screen.getByRole("alert");
    expect(alert.textContent).toContain("Server unreachable");
    expect(alert.textContent).toContain("code: offline");
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(onRetry).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("link", { name: "Back home" })).toBeTruthy();
  });

  it("moves focus to the main landmark when SkipLink is activated", () => {
    render(
      <div>
        <SkipLink />
        <main id="main-content" tabIndex={-1}>
          Content
        </main>
      </div>,
    );

    const skip = screen.getByRole("link", { name: "Skip to main content" });
    const main = screen.getByRole("main");
    fireEvent.click(skip);
    expect(document.activeElement).toBe(main);
  });
});
