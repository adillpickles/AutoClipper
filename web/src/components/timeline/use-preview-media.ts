"use client";

// Preview video sources for scrubbing frames (#189). Uses only the existing candidate-media
// endpoint (api-contract.md § 5): no thumbnail route exists, so frames come from seeking a muted
// <video> through a preview file. Requests start on the first interaction with the timeline, go
// best rank first, and stop as soon as one preview covers the whole VOD (a local or uploaded file,
// or mock mode). Otherwise each top moment's own segment gives frames around that moment.
// A `preparing` answer ends the pass: a local file being converted is shared by every moment, so
// asking the other nine would only repeat the same answer. The next pass, on a later interaction
// at least `PREVIEW_RETRY_MS` later, asks again.

import { useCallback, useEffect, useRef, useState } from "react";

import { getApiClient } from "@/lib/api/client";
import type { Candidate } from "@/lib/api/types";

/** Don't re-ask about a preview that was `preparing` or failed sooner than this. */
export const PREVIEW_RETRY_MS = 4000;

export interface PreviewSource {
  candidateId: string;
  url: string;
  /** VOD time at media time 0. */
  startS: number;
  /** End of usable coverage in VOD time. */
  endS: number;
}

type Status =
  | { kind: "available" }
  | { kind: "unavailable" }
  | { kind: "retry"; at: number };

function coversWholeVod(source: PreviewSource, durationS: number): boolean {
  return source.startS <= 1 && source.endS >= durationS - 1;
}

/**
 * The preview that shows `timeS`, preferring one prepared for a moment containing that time, then
 * the narrowest covering file. `null` when no preview covers it.
 */
export function previewSourceFor(
  timeS: number,
  sources: readonly PreviewSource[],
  momentId: string | null,
): PreviewSource | null {
  const covering = sources.filter((s) => timeS >= s.startS && timeS <= s.endS);
  if (covering.length === 0) return null;
  return (
    covering.find((s) => s.candidateId === momentId) ??
    [...covering].sort((a, b) => a.endS - a.startS - (b.endS - b.startS))[0]
  );
}

export function usePreviewMedia(
  jobId: string,
  candidates: readonly Candidate[],
  durationS: number,
) {
  const [sources, setSources] = useState<PreviewSource[]>([]);
  const statusRef = useRef<Map<string, Status>>(new Map());
  const sourcesRef = useRef<PreviewSource[]>([]);
  const runningRef = useRef(false);
  /** No pass before this time (set when the server reported a preview still `preparing`). */
  const pauseUntilRef = useRef(0);
  const mountedRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  const publish = useCallback((next: PreviewSource[]) => {
    sourcesRef.current = next;
    if (mountedRef.current) setSources(next);
  }, []);

  /** Ask for previews not yet known; safe to call on every pointer enter or focus. */
  const ensure = useCallback(async () => {
    if (runningRef.current) return;
    if (Date.now() < pauseUntilRef.current) return;
    if (sourcesRef.current.some((s) => coversWholeVod(s, durationS))) return;
    runningRef.current = true;
    const client = getApiClient();
    try {
      const ordered = [...candidates].sort((a, b) => a.rank - b.rank);
      for (const candidate of ordered) {
        if (!mountedRef.current) return;
        const status = statusRef.current.get(candidate.candidate_id);
        if (status?.kind === "available" || status?.kind === "unavailable") {
          continue;
        }
        if (status?.kind === "retry" && Date.now() < status.at) continue;
        try {
          const media = await client.getCandidateMedia(
            jobId,
            candidate.candidate_id,
          );
          const preview = media.preview;
          if (
            preview.status === "available" &&
            preview.kind === "video_file" &&
            preview.url &&
            preview.media_start_s !== null &&
            preview.media_end_s !== null
          ) {
            statusRef.current.set(candidate.candidate_id, {
              kind: "available",
            });
            const source: PreviewSource = {
              candidateId: candidate.candidate_id,
              url: client.resolveUrl(preview.url),
              startS: preview.media_start_s,
              endS: preview.media_end_s,
            };
            publish([...sourcesRef.current, source]);
            if (coversWholeVod(source, durationS)) return;
          } else if (preview.status === "preparing") {
            // Retry this one first next time; don't ask the rest about the same file now.
            pauseUntilRef.current = Date.now() + PREVIEW_RETRY_MS;
            return;
          } else {
            statusRef.current.set(candidate.candidate_id, {
              kind: "unavailable",
            });
          }
        } catch {
          // Frames are optional: show the timeline without a frame and try again later.
          statusRef.current.set(candidate.candidate_id, {
            kind: "retry",
            at: Date.now() + PREVIEW_RETRY_MS,
          });
        }
      }
    } finally {
      runningRef.current = false;
    }
  }, [candidates, durationS, jobId, publish]);

  /** Drop a source whose file failed to load in the browser, so the timeline shows no frame. */
  const markFailed = useCallback(
    (url: string) => {
      const failed = sourcesRef.current.filter((s) => s.url === url);
      for (const source of failed) {
        statusRef.current.set(source.candidateId, { kind: "unavailable" });
      }
      publish(sourcesRef.current.filter((s) => s.url !== url));
    },
    [publish],
  );

  return { sources, ensure, markFailed };
}
