// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Badge } from "@/components/ui/badge";
import { Card, cardClasses } from "@/components/ui/card";
import { Dialog, DialogHeader, focusableWithin } from "@/components/ui/dialog";
import { FieldMessage, Input, inputClasses } from "@/components/ui/input";
import { Notice } from "@/components/ui/notice";
import { clampPercent, ProgressBar, Spinner } from "@/components/ui/progress";
import { SignalStrip, signalBars } from "@/components/ui/signal-strip";

afterEach(() => {
  cleanup();
  document.body.style.overflow = "";
});

function DialogHarness({ onClose }: { onClose: () => void }) {
  const [open, setOpen] = useState(false);
  return (
    <div>
      <button type="button" onClick={() => setOpen(true)}>
        Open
      </button>
      {open && (
        <Dialog
          labelledBy="t"
          onClose={() => {
            onClose();
            setOpen(false);
          }}
        >
          <DialogHeader
            titleId="t"
            title="Title"
            onClose={() => setOpen(false)}
          />
          <input aria-label="Field" />
          <button type="button">Confirm</button>
        </Dialog>
      )}
    </div>
  );
}

describe("Dialog", () => {
  it("takes focus, traps Tab, closes on Escape and restores focus", () => {
    const onClose = vi.fn();
    render(<DialogHarness onClose={onClose} />);
    const opener = screen.getByRole("button", { name: "Open" });
    opener.focus();
    fireEvent.click(opener);

    const dialog = screen.getByRole("dialog", { name: "Title" });
    expect(dialog.getAttribute("aria-modal")).toBe("true");
    expect(document.activeElement).toBe(dialog);
    expect(document.body.style.overflow).toBe("hidden");

    const close = screen.getByRole("button", { name: "Close" });
    const confirm = screen.getByRole("button", { name: "Confirm" });
    // Tab from the last control wraps to the first; Shift+Tab from the first wraps back.
    confirm.focus();
    fireEvent.keyDown(confirm, { key: "Tab" });
    expect(document.activeElement).toBe(close);
    fireEvent.keyDown(close, { key: "Tab", shiftKey: true });
    expect(document.activeElement).toBe(confirm);

    fireEvent.keyDown(window, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(document.activeElement).toBe(opener);
    expect(document.body.style.overflow).toBe("");
  });

  it("lists only reachable controls", () => {
    const root = document.createElement("div");
    root.innerHTML = `
      <button>a</button>
      <button disabled>b</button>
      <input type="hidden" />
      <div inert><button>c</button></div>
      <a href="/x">d</a>
      <span tabindex="-1">e</span>
    `;
    expect(focusableWithin(root).map((el) => el.textContent)).toEqual([
      "a",
      "d",
    ]);
  });
});

describe("progress", () => {
  it("clamps and rounds percentages", () => {
    expect(clampPercent(42.4)).toBe(42);
    expect(clampPercent(-5)).toBe(0);
    expect(clampPercent(140)).toBe(100);
    expect(clampPercent(null)).toBeNull();
    expect(clampPercent(Number.NaN)).toBeNull();
  });

  it("exposes determinate and indeterminate bars to assistive tech", () => {
    render(
      <div>
        <ProgressBar value={65.2} label="Rendering" />
        <ProgressBar value={null} label="Waiting" />
        <Spinner />
      </div>,
    );
    const [determinate, indeterminate] = screen.getAllByRole("progressbar");
    expect(determinate.getAttribute("aria-label")).toBe("Rendering");
    expect(determinate.getAttribute("aria-valuenow")).toBe("65");
    expect(indeterminate.getAttribute("aria-valuenow")).toBeNull();
    // Every animation is gated on motion-safe.
    for (const el of document.querySelectorAll('[class*="animate-"]')) {
      expect(el.className).toMatch(/motion-safe:animate-/);
    }
  });
});

describe("form and surface primitives", () => {
  it("sizes inputs and wires messages", () => {
    expect(inputClasses()).toContain("w-full");
    expect(inputClasses({ fullWidth: false })).not.toContain("w-full");
    expect(inputClasses({ mono: true })).toContain("font-mono");
    render(
      <div>
        <Input aria-label="Path" aria-invalid aria-describedby="m" />
        <FieldMessage id="m" role="alert" tone="error">
          Bad path
        </FieldMessage>
      </div>,
    );
    const input = screen.getByLabelText("Path");
    expect(input.getAttribute("aria-describedby")).toBe("m");
    expect(screen.getByRole("alert").textContent).toBe("Bad path");
  });

  it("renders cards, badges and notices", () => {
    expect(cardClasses({ tone: "elevated", padding: "none" })).toContain(
      "bg-surface-elevated",
    );
    render(
      <Card as="section" aria-label="Panel">
        <Badge tone="accent" live>
          Running
        </Badge>
        <Notice role="status" tone="success" title="Saved">
          New clips are copied there.
        </Notice>
      </Card>,
    );
    expect(screen.getByRole("region", { name: "Panel" })).toBeTruthy();
    expect(screen.getByText("Running").className).toContain("rounded-pill");
    expect(screen.getByRole("status").textContent).toContain("Saved");
  });
});

describe("signal strip", () => {
  it("is deterministic and spikes at each peak", () => {
    const peaks = [{ at: 0.5, rank: 1 }];
    const a = signalBars(41, peaks);
    const b = signalBars(41, peaks);
    expect(a).toEqual(b);
    const tallest = a.reduce(
      (best, bar, index) => (bar.height > a[best].height ? index : best),
      0,
    );
    expect(tallest).toBe(20);
    expect(a[20].hot).toBe(true);
    expect(a[0].hot).toBe(false);
    for (const bar of a) {
      expect(bar.height).toBeGreaterThan(0);
      expect(bar.height).toBeLessThanOrEqual(1);
    }
  });

  it("is hidden from assistive tech and pins only when asked", () => {
    const { container, rerender } = render(<SignalStrip bars={10} />);
    const root = container.firstElementChild as HTMLElement;
    expect(root.getAttribute("aria-hidden")).toBe("true");
    expect(root.textContent).toContain("#1");
    rerender(<SignalStrip bars={10} pins={false} animate={false} />);
    expect(root.textContent).toBe("");
    expect(root.innerHTML).not.toContain("animate-signal-rise");
  });
});
