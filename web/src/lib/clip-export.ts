import type { ClipExport } from "@/components/clip-card";

/**
 * Whether two clip states would show the same thing. Export polls bring back a new object every
 * 2 s even when nothing changed; keeping the old one lets React skip the re-render (#241).
 */
export function sameClipExport(
  current: ClipExport | undefined,
  next: ClipExport,
): boolean {
  if (current === undefined) return false;
  if (current === next) return true;
  switch (next.kind) {
    case "pending":
      return current.kind === "pending";
    case "unavailable":
    case "error":
      return current.kind === next.kind && current.message === next.message;
    case "export":
      return (
        current.kind === "export" &&
        current.url === next.url &&
        JSON.stringify(current.export) === JSON.stringify(next.export)
      );
  }
}
