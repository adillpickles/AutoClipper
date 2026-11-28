// One way to word a render's progress (clip cards, the clip editor, the highlights dialog).
// The stage message already says what is happening ("Rendering the clip"), so it is never
// prefixed with another "Rendering…"; the step count goes on a separate, quieter line.

import type { Progress } from "@/lib/api/types";

export interface RenderProgressText {
  /** What is happening now: the stage message, or `fallback` before the first update. */
  label: string;
  /** "Step 2 of 3" when the stage position is known, otherwise `null`. */
  step: string | null;
}

export function renderProgressText(
  progress: Progress | null | undefined,
  fallback: string,
): RenderProgressText {
  const label = progress?.message.trim() || fallback;
  const step =
    progress && progress.stage_count > 0 && progress.stage_index > 0
      ? `Step ${progress.stage_index} of ${progress.stage_count}`
      : null;
  return { label, step };
}
