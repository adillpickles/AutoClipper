"use client";

import {
  useEffect,
  useRef,
  type KeyboardEvent,
  type ReactNode,
  type RefObject,
} from "react";

const FOCUSABLE = [
  "a[href]",
  "button:not([disabled])",
  "input:not([disabled]):not([type='hidden'])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  // Media with native controls is a Tab stop in every browser (the highlights player).
  "video[controls]",
  "audio[controls]",
  "[tabindex]:not([tabindex='-1'])",
].join(",");

/** The elements Tab can reach inside `root`, in DOM order. */
export function focusableWithin(root: HTMLElement): HTMLElement[] {
  return Array.from(root.querySelectorAll<HTMLElement>(FOCUSABLE)).filter(
    (element) => !element.hasAttribute("inert") && !element.closest("[inert]"),
  );
}

/**
 * A modal dialog: a scrim, a panel that keeps keyboard focus inside it, Escape to close, and
 * focus returned to whatever opened it. Clicking the scrim does not close it, so a stray
 * click never throws away edits.
 */
export function Dialog({
  labelledBy,
  describedBy,
  onClose,
  initialFocus,
  size = "md",
  className = "",
  children,
}: {
  labelledBy: string;
  describedBy?: string;
  onClose: () => void;
  initialFocus?: RefObject<HTMLElement | null>;
  size?: "md" | "lg";
  className?: string;
  children: ReactNode;
}) {
  const panelRef = useRef<HTMLDivElement | null>(null);
  // Kept in a ref so parent rerenders never restart the effects below.
  const closeRef = useRef(onClose);
  useEffect(() => {
    closeRef.current = onClose;
  }, [onClose]);

  useEffect(() => {
    const previouslyFocused =
      document.activeElement instanceof HTMLElement
        ? document.activeElement
        : null;
    (initialFocus?.current ?? panelRef.current)?.focus();

    // Focus must never rest outside the panel while the dialog is open. When the focused
    // control is removed (a button replaced by progress, say), browsers drop focus to <body>
    // without any event, so Tab is guarded at the document level as well.
    function pullFocusIn() {
      const panel = panelRef.current;
      if (!panel) return;
      (focusableWithin(panel)[0] ?? panel).focus();
    }
    const outside = (target: EventTarget | null) => {
      const panel = panelRef.current;
      return (
        panel !== null && !(target instanceof Node && panel.contains(target))
      );
    };

    // Escape closes from anywhere, even when focus has left the panel.
    const keydown = (event: globalThis.KeyboardEvent) => {
      if (event.key === "Escape") {
        closeRef.current();
      } else if (event.key === "Tab" && outside(document.activeElement)) {
        event.preventDefault();
        pullFocusIn();
      }
    };
    const focusin = (event: FocusEvent) => {
      if (outside(event.target)) pullFocusIn();
    };
    window.addEventListener("keydown", keydown);
    document.addEventListener("focusin", focusin);

    const { overflow } = document.body.style;
    document.body.style.overflow = "hidden";
    return () => {
      window.removeEventListener("keydown", keydown);
      document.removeEventListener("focusin", focusin);
      document.body.style.overflow = overflow;
      if (previouslyFocused?.isConnected) previouslyFocused.focus();
    };
    // Runs once per mount: the dialog takes focus when it opens.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function handleKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    if (event.key !== "Tab" || !panelRef.current) return;
    const items = focusableWithin(panelRef.current);
    if (items.length === 0) {
      event.preventDefault();
      return;
    }
    const first = items[0];
    const last = items[items.length - 1];
    const active = document.activeElement;
    if (!items.includes(active as HTMLElement)) {
      // Focus sits on the panel or a non-tabbable region inside it.
      event.preventDefault();
      (event.shiftKey ? last : first).focus();
    } else if (event.shiftKey && active === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && active === last) {
      event.preventDefault();
      first.focus();
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-end justify-center sm:items-center sm:p-6">
      <div
        aria-hidden="true"
        className="absolute inset-0 bg-scrim backdrop-blur-[2px] motion-safe:animate-scrim-in"
      />
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={labelledBy}
        aria-describedby={describedBy}
        tabIndex={-1}
        onKeyDown={handleKeyDown}
        className={`relative flex max-h-[92dvh] w-full flex-col overflow-hidden rounded-t-panel border border-border-strong bg-surface-panel shadow-dialog focus:outline-none motion-safe:animate-dialog-in sm:max-h-[min(88dvh,52rem)] sm:rounded-panel ${
          size === "lg" ? "sm:max-w-2xl" : "sm:max-w-lg"
        } ${className}`}
      >
        {children}
      </div>
    </div>
  );
}

/** Title bar of a dialog, with the close control on the right. */
export function DialogHeader({
  titleId,
  title,
  description,
  descriptionId,
  onClose,
  closeLabel = "Close",
}: {
  titleId: string;
  title: ReactNode;
  description?: ReactNode;
  descriptionId?: string;
  onClose: () => void;
  closeLabel?: string;
}) {
  return (
    <div className="flex items-start justify-between gap-4 border-b border-border-subtle px-5 pt-5 pb-4 sm:px-6">
      <div className="flex min-w-0 flex-col gap-1">
        <h2
          id={titleId}
          className="font-display text-xl font-semibold tracking-[-0.02em] text-ink-primary"
        >
          {title}
        </h2>
        {description && (
          <p
            id={descriptionId}
            className="text-sm leading-6 text-pretty text-ink-secondary"
          >
            {description}
          </p>
        )}
      </div>
      <button
        type="button"
        onClick={onClose}
        aria-label={closeLabel}
        className="-mt-1 -mr-2 inline-flex size-10 shrink-0 items-center justify-center rounded-control text-ink-tertiary transition-colors hover:bg-surface-elevated hover:text-ink-primary focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus-ring"
      >
        <svg
          viewBox="0 0 20 20"
          fill="none"
          aria-hidden="true"
          className="size-5"
          stroke="currentColor"
          strokeWidth="1.8"
          strokeLinecap="round"
        >
          <path d="M5 5l10 10M15 5L5 15" />
        </svg>
      </button>
    </div>
  );
}
