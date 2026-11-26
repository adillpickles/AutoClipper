// Shared per-job browser state (remembered export IDs and per-candidate trims) so edits in
// the clip editor carry back to the results screen and the highlights dialog.

export interface SavedTrim {
  start_s: number;
  end_s: number;
}

interface ResetMarker {
  reset: true;
}

type StoredTrimEntry = SavedTrim | ResetMarker;

const exportsKey = (jobId: string) => `autoclipper.exports.${jobId}`;
const trimsKey = (jobId: string) => `autoclipper.trims.${jobId}`;

function isValidTrim(value: unknown): value is SavedTrim {
  if (typeof value !== "object" || value === null) return false;
  const candidate = value as Record<string, unknown>;
  return (
    typeof candidate.start_s === "number" &&
    typeof candidate.end_s === "number" &&
    Number.isFinite(candidate.start_s) &&
    Number.isFinite(candidate.end_s) &&
    candidate.start_s >= 0 &&
    candidate.start_s < candidate.end_s
  );
}

function isResetMarker(value: unknown): value is ResetMarker {
  return (
    typeof value === "object" &&
    value !== null &&
    (value as Record<string, unknown>).reset === true
  );
}

function readRawTrimEntries(jobId: string): Record<string, StoredTrimEntry> {
  try {
    const raw = JSON.parse(
      window.localStorage.getItem(trimsKey(jobId)) ?? "{}",
    ) as Record<string, unknown>;
    if (typeof raw !== "object" || raw === null) return {};
    const valid: Record<string, StoredTrimEntry> = {};
    for (const [candidateId, value] of Object.entries(raw)) {
      if (isValidTrim(value)) {
        valid[candidateId] = { start_s: value.start_s, end_s: value.end_s };
      } else if (isResetMarker(value)) {
        valid[candidateId] = { reset: true };
      }
    }
    return valid;
  } catch {
    return {};
  }
}

export function rememberedExport(
  jobId: string,
  candidateId: string,
): string | null {
  try {
    const saved = JSON.parse(
      window.localStorage.getItem(exportsKey(jobId)) ?? "{}",
    ) as Record<string, unknown>;
    const value = saved[candidateId];
    return typeof value === "string" && value.length > 0 ? value : null;
  } catch {
    return null;
  }
}

export function rememberExport(
  jobId: string,
  candidateId: string,
  exportId: string,
): void {
  try {
    const saved = JSON.parse(
      window.localStorage.getItem(exportsKey(jobId)) ?? "{}",
    ) as Record<string, string>;
    window.localStorage.setItem(
      exportsKey(jobId),
      JSON.stringify({ ...saved, [candidateId]: exportId }),
    );
  } catch {
    // Storage is optional; nothing breaks without it.
  }
}

export function rememberedTrims(jobId: string): Record<string, SavedTrim> {
  const entries = readRawTrimEntries(jobId);
  const trims: Record<string, SavedTrim> = {};
  for (const [candidateId, entry] of Object.entries(entries)) {
    if (isValidTrim(entry)) {
      trims[candidateId] = entry;
    }
  }
  return trims;
}

export function rememberedTrim(
  jobId: string,
  candidateId: string,
): SavedTrim | null {
  return rememberedTrims(jobId)[candidateId] ?? null;
}

/** Whether the user explicitly reset this candidate's trim back to its original bounds. */
export function isTrimReset(jobId: string, candidateId: string): boolean {
  const entry = readRawTrimEntries(jobId)[candidateId];
  return entry !== undefined && isResetMarker(entry);
}

export function rememberTrim(
  jobId: string,
  candidateId: string,
  trim: SavedTrim,
): void {
  if (!isValidTrim(trim)) return;
  try {
    const saved = readRawTrimEntries(jobId);
    window.localStorage.setItem(
      trimsKey(jobId),
      JSON.stringify({
        ...saved,
        [candidateId]: { start_s: trim.start_s, end_s: trim.end_s },
      }),
    );
  } catch {
    // Storage is optional; nothing breaks without it.
  }
}

/**
 * Clear a saved trim. Pass `recordReset: true` when the user explicitly clicked Reset so
 * callers (like the highlights dialog) do not fall back to an earlier remembered export's trim.
 */
export function clearRememberedTrim(
  jobId: string,
  candidateId: string,
  options?: { recordReset?: boolean },
): void {
  try {
    const saved = readRawTrimEntries(jobId);
    if (options?.recordReset) {
      window.localStorage.setItem(
        trimsKey(jobId),
        JSON.stringify({ ...saved, [candidateId]: { reset: true } }),
      );
      return;
    }
    if (!(candidateId in saved)) return;
    delete saved[candidateId];
    if (Object.keys(saved).length === 0) {
      window.localStorage.removeItem(trimsKey(jobId));
    } else {
      window.localStorage.setItem(trimsKey(jobId), JSON.stringify(saved));
    }
  } catch {
    // Storage is optional; nothing breaks without it.
  }
}
