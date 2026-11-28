"use client";

import {
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
  type KeyboardEvent,
} from "react";

import type { ClipExport } from "@/components/clip-card";
import { Button, FOCUS_RING, type ButtonSize } from "@/components/ui/button";
import type { Candidate, VodSummary } from "@/lib/api/types";
import { titleFor } from "@/lib/reason";
import {
  browserShareEnvironment,
  buildShareCaption,
  copyText,
  loadShareFile,
  menuPosition,
  shareCapability,
  shareVideo,
  vodTimestampUrl,
  type ShareCapability,
  type ShareEnvironment,
  type ShareOutcome,
} from "@/lib/share";

type Status =
  | { tone: "ok" | "info"; text: string }
  | { tone: "error"; text: string; manual?: string };

const STATUS_FOR: Partial<Record<ShareOutcome["kind"], Status>> = {
  "shared-file": { tone: "ok", text: "Shared." },
  "shared-text": {
    tone: "ok",
    text: "Shared the caption and link. Download the video to attach it.",
  },
  retry: {
    tone: "info",
    text: "The video is ready to share now. Press Share again.",
  },
  unsupported: {
    tone: "error",
    text: "This browser can't share the video. Download it and copy the caption instead.",
  },
  failed: {
    tone: "error",
    text: "Sharing didn't work. Download the video and copy the caption instead.",
  },
};

const ITEM = `flex min-h-11 w-full flex-col items-start justify-center gap-0.5 rounded-control px-3 py-2 text-left text-sm font-semibold text-ink-primary transition-colors hover:bg-surface-subtle focus:bg-surface-subtle disabled:cursor-wait disabled:opacity-60 ${FOCUS_RING}`;
const HINT = "text-xs font-normal text-ink-tertiary";

/** Whether this browser has the Popover API (the menu renders only on the client). */
function supportsPopover(): boolean {
  return (
    typeof HTMLElement !== "undefined" &&
    typeof HTMLElement.prototype.showPopover === "function"
  );
}

/**
 * Share a finished clip or highlights video (#196): the MP4 itself through the Web Share API
 * where the browser accepts files, otherwise the caption and VOD link, plus Download, "Copy
 * caption" and "Copy VOD link" everywhere. Render it only once the file is finished.
 *
 * Returns the trigger and a visible status line as siblings, so in a wrapping row the status
 * takes a line of its own.
 */
export function ShareMenu({
  fileUrl,
  filename,
  title,
  channel,
  videoId,
  startS,
  subject,
  align = "start",
  side = "bottom",
  size = "md",
  className = "",
  environment,
}: {
  /** Resolved URL of the finished MP4. */
  fileUrl: string;
  filename: string;
  title: string;
  channel?: string | null;
  videoId: string;
  /** The moment's start in the VOD, for the timestamp link; omit for a whole-stream video. */
  startS?: number | null;
  /** What is shared, for screen readers: "clip 3", "the highlights video". */
  subject: string;
  align?: "start" | "end";
  side?: "top" | "bottom";
  size?: ButtonSize;
  className?: string;
  /** Test seam; defaults to the browser's `navigator`. */
  environment?: ShareEnvironment;
}) {
  const [open, setOpen] = useState(false);
  const [capability, setCapability] = useState<ShareCapability>("none");
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState<Status | null>(null);
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const rootRef = useRef<HTMLDivElement | null>(null);
  const menuRef = useRef<HTMLDivElement | null>(null);
  const fileRef = useRef<{ url: string; file: Promise<File | null> } | null>(
    null,
  );
  const menuId = useId();
  const triggerId = useId();

  const caption = buildShareCaption({ title, channel, videoId, startS });
  const link = vodTimestampUrl(videoId, startS);
  const hasStart = startS !== null && startS !== undefined;

  const env = () => environment ?? browserShareEnvironment();

  function items(): HTMLElement[] {
    const menu = menuRef.current;
    if (!menu) return [];
    return Array.from(
      menu.querySelectorAll<HTMLElement>("[role='menuitem']"),
    ).filter((item) => !item.hasAttribute("disabled"));
  }

  function preparedFile(current: ShareEnvironment): Promise<File | null> {
    if (fileRef.current?.url !== fileUrl) {
      fileRef.current = {
        url: fileUrl,
        file: loadShareFile(fileUrl, filename, current),
      };
    }
    return fileRef.current.file;
  }

  function openMenu() {
    const current = env();
    const next = shareCapability(current);
    setCapability(next);
    setStatus(null);
    setOpen(true);
    // Load the MP4 while the menu is open: the share sheet must open within the click's
    // activation window, which a large download started on click would outlast.
    if (next === "files") void preparedFile(current);
  }

  function closeMenu(returnFocus: boolean) {
    setOpen(false);
    if (returnFocus) triggerRef.current?.focus();
  }

  // Show the menu in the top layer where the Popover API exists, so a card's or dialog's
  // `overflow: hidden` can't clip it, placed next to the trigger and kept on screen. It stays
  // inside this component's DOM, so an enclosing dialog's focus trap still contains it.
  // Without the API it falls back to the absolute position from its classes.
  useLayoutEffect(() => {
    const menu = menuRef.current;
    const button = triggerRef.current;
    if (!open || !menu || !button || typeof menu.showPopover !== "function") {
      return;
    }
    const place = () => {
      const { left, top } = menuPosition(
        button.getBoundingClientRect(),
        { width: menu.offsetWidth, height: menu.offsetHeight },
        { width: window.innerWidth, height: window.innerHeight },
        side,
        align,
      );
      Object.assign(menu.style, {
        position: "fixed",
        inset: "auto",
        margin: "0",
        left: `${left}px`,
        top: `${top}px`,
      });
    };
    try {
      menu.showPopover();
    } catch {
      return;
    }
    place();
    window.addEventListener("resize", place);
    window.addEventListener("scroll", place, true);
    return () => {
      window.removeEventListener("resize", place);
      window.removeEventListener("scroll", place, true);
    };
  }, [open, side, align]);

  // Focus the first item when the menu opens.
  useEffect(() => {
    if (!open) return;
    menuRef.current
      ?.querySelector<HTMLElement>("[role='menuitem']:not([disabled])")
      ?.focus();
  }, [open]);

  // A press outside closes the menu without moving focus.
  useEffect(() => {
    if (!open) return;
    const onPointerDown = (event: PointerEvent) => {
      if (
        rootRef.current &&
        event.target instanceof Node &&
        !rootRef.current.contains(event.target)
      ) {
        setOpen(false);
      }
    };
    document.addEventListener("pointerdown", onPointerDown);
    return () => document.removeEventListener("pointerdown", onPointerDown);
  }, [open]);

  function onMenuKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    const list = items();
    const index = list.indexOf(document.activeElement as HTMLElement);
    let next: HTMLElement | undefined;
    switch (event.key) {
      case "Escape":
        // Keep an enclosing dialog open: Escape closes only the menu.
        event.preventDefault();
        event.stopPropagation();
        closeMenu(true);
        return;
      case "Tab":
        setOpen(false);
        return;
      case "ArrowDown":
        next = list[(index + 1) % list.length];
        break;
      case "ArrowUp":
        next = list[(index - 1 + list.length) % list.length];
        break;
      case "Home":
        next = list[0];
        break;
      case "End":
        next = list[list.length - 1];
        break;
      default:
        return;
    }
    event.preventDefault();
    next?.focus();
  }

  async function share() {
    const current = env();
    setBusy(true);
    try {
      const file = capability === "files" ? await preparedFile(current) : null;
      const outcome = await shareVideo({
        file,
        title,
        caption,
        link,
        env: current,
      });
      // A cancelled share sheet is not a share: show nothing.
      setStatus(STATUS_FOR[outcome.kind] ?? null);
    } finally {
      setBusy(false);
      closeMenu(true);
    }
  }

  async function copy(text: string, what: string) {
    const copied = await copyText(text, env());
    setStatus(
      copied
        ? { tone: "ok", text: `${what} copied.` }
        : {
            tone: "error",
            text: `Couldn't copy the ${what.toLowerCase()}. Select it below and copy it.`,
            manual: text,
          },
    );
    closeMenu(true);
  }

  const shareLabel =
    capability === "files" ? "Share video" : "Share caption and link";
  const shareHint =
    capability === "files"
      ? "Send the MP4 to an app"
      : "This browser can't attach the video";

  return (
    <>
      <div ref={rootRef} className={`relative ${className}`}>
        <Button
          ref={triggerRef}
          id={triggerId}
          variant="secondary"
          size={size}
          aria-haspopup="menu"
          aria-expanded={open}
          aria-controls={open ? menuId : undefined}
          onClick={() => (open ? closeMenu(false) : openMenu())}
          onKeyDown={(event) => {
            if (
              !open &&
              (event.key === "ArrowDown" || event.key === "ArrowUp")
            ) {
              event.preventDefault();
              openMenu();
            }
          }}
        >
          <svg
            viewBox="0 0 20 20"
            fill="none"
            aria-hidden="true"
            className="size-4"
            stroke="currentColor"
            strokeWidth="1.8"
            strokeLinecap="round"
            strokeLinejoin="round"
          >
            <path d="M10 3v9M6.5 6.5 10 3l3.5 3.5M5 10.5v5a1.5 1.5 0 0 0 1.5 1.5h7a1.5 1.5 0 0 0 1.5-1.5v-5" />
          </svg>
          Share <span className="sr-only">{subject}</span>
        </Button>
        {open && (
          <div
            ref={menuRef}
            id={menuId}
            role="menu"
            popover={supportsPopover() ? "manual" : undefined}
            aria-labelledby={triggerId}
            onKeyDown={onMenuKeyDown}
            className={`absolute z-30 flex w-64 max-w-[calc(100vw-2rem)] flex-col gap-0.5 rounded-card border border-border-strong bg-surface-elevated p-1.5 shadow-dialog ${
              side === "top" ? "bottom-full mb-2" : "top-full mt-2"
            } ${align === "end" ? "right-0" : "left-0"}`}
          >
            {capability !== "none" && (
              <button
                type="button"
                role="menuitem"
                tabIndex={-1}
                className={ITEM}
                disabled={busy}
                onClick={() => void share()}
              >
                {busy ? "Preparing the video…" : shareLabel}
                <span className={HINT}>{shareHint}</span>
              </button>
            )}
            <a
              role="menuitem"
              tabIndex={-1}
              href={fileUrl}
              download={filename}
              className={ITEM}
              onClick={() => closeMenu(true)}
            >
              Download
              <span className={HINT}>Save the MP4 to post it yourself</span>
            </a>
            <button
              type="button"
              role="menuitem"
              tabIndex={-1}
              className={ITEM}
              onClick={() => void copy(caption, "Caption")}
            >
              Copy caption
              <span className={HINT}>Title, channel and VOD link</span>
            </button>
            {link && (
              <button
                type="button"
                role="menuitem"
                tabIndex={-1}
                className={ITEM}
                onClick={() => void copy(link, "Link")}
              >
                {hasStart ? "Copy VOD timestamp link" : "Copy VOD link"}
                <span className={HINT}>
                  {hasStart
                    ? "Opens the stream at this moment"
                    : "Opens the full stream on Twitch"}
                </span>
              </button>
            )}
          </div>
        )}
        <span role="status" aria-live="polite" className="sr-only">
          {status?.text ?? ""}
        </span>
      </div>
      {status && (
        <div className="flex basis-full flex-col gap-1.5">
          {/* Announced by the live region above; shown here for sighted users. */}
          <p
            aria-hidden="true"
            className={`text-xs leading-relaxed text-pretty ${
              status.tone === "error"
                ? "text-danger-soft-ink"
                : status.tone === "ok"
                  ? "text-success-soft-ink"
                  : "text-ink-secondary"
            }`}
          >
            {status.text}
          </p>
          {status.tone === "error" && status.manual && (
            <textarea
              readOnly
              value={status.manual}
              rows={3}
              aria-label="Text to copy"
              onFocus={(event) => event.currentTarget.select()}
              className={`w-full resize-none rounded-control border border-border-subtle bg-surface-subtle px-3 py-2 font-mono text-xs text-ink-primary ${FOCUS_RING}`}
            />
          )}
        </div>
      )}
    </>
  );
}

/** The results card's Share button: nothing until the card's export has finished. */
export function ClipShareMenu({
  clip,
  candidate,
  filename,
  vod,
  videoId,
}: {
  clip: ClipExport;
  candidate: Candidate;
  filename: string;
  vod?: Pick<VodSummary, "broadcaster_display_name">;
  videoId: string;
}) {
  if (clip.kind !== "export" || clip.export.state !== "done" || !clip.url) {
    return null;
  }
  return (
    <ShareMenu
      fileUrl={clip.url}
      filename={filename}
      title={titleFor(candidate)}
      channel={vod?.broadcaster_display_name}
      videoId={videoId}
      startS={clip.export.start_s}
      subject={`clip ${candidate.rank}`}
      side="top"
      align="end"
    />
  );
}
