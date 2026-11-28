"use client";

import { useState } from "react";

import { Button } from "@/components/ui/button";
import { ApiRequestError, getApiClient } from "@/lib/api/client";
import type { ArchiveKind, ArchiveOmission } from "@/lib/api/types";

const REASONS: Record<ArchiveOmission["reason"], string> = {
  queued: "waiting to render",
  running: "still rendering",
  failed: "render failed",
  file_missing: "file no longer on disk",
};

const NOUNS: Record<ArchiveKind, [one: string, many: string]> = {
  clips: ["clip", "clips"],
  highlights: ["highlights video", "highlights videos"],
};

function count(n: number, kind: ArchiveKind): string {
  const [one, many] = NOUNS[kind];
  return `${n} ${n === 1 ? one : many}`;
}

function omissionLabel(item: ArchiveOmission): string {
  if (item.rank !== null) return `Clip ${item.rank}`;
  return item.candidate_id ? `Clip ${item.candidate_id}` : "Highlights video";
}

/** "Download all" for a job's finished clips and highlights videos, each as one ZIP. */
export function ArchiveDownloads({ jobId }: { jobId: string }) {
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [omitted, setOmitted] = useState<ArchiveOmission[]>([]);

  async function download(kind: ArchiveKind) {
    setBusy(true);
    setMessage("");
    setOmitted([]);
    try {
      const client = getApiClient();
      const status = await client.getArchiveStatus(jobId, kind);
      setOmitted(status.omitted);
      if (!status.download_url) {
        setMessage(
          status.omitted.length > 0
            ? `None of the ${NOUNS[kind][1]} have finished yet:`
            : `There are no ${NOUNS[kind][1]} to download yet.`,
        );
        return;
      }
      // A plain link lets the browser write the ZIP to disk as it streams in.
      const link = document.createElement("a");
      link.href = client.resolveUrl(status.download_url);
      link.download = status.filename;
      link.click();
      setMessage(
        status.omitted.length > 0
          ? `Downloading ${count(status.ready_count, kind)}. Left out (listed in missing.txt):`
          : `Downloading ${count(status.ready_count, kind)} as ${status.filename}.`,
      );
    } catch (error) {
      setMessage(
        error instanceof ApiRequestError
          ? error.error.message
          : "Couldn't reach the server. Check that AutoClipper is running, then try again.",
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex min-w-0 flex-col gap-2">
      <div className="flex flex-wrap items-center gap-2">
        <span className="mr-1 w-full text-xs font-semibold text-ink-tertiary sm:w-auto">
          Get everything
        </span>
        <Button
          variant="ghost"
          size="sm"
          disabled={busy}
          onClick={() => void download("clips")}
        >
          <ZipGlyph />
          Download all clips (ZIP)
        </Button>
        <Button
          variant="ghost"
          size="sm"
          disabled={busy}
          onClick={() => void download("highlights")}
        >
          <ZipGlyph />
          Download all highlights (ZIP)
        </Button>
      </div>
      <div role="status" className="text-sm leading-6 text-ink-secondary">
        {message && <p>{message}</p>}
        {omitted.length > 0 && (
          <ul className="mt-1 list-disc space-y-1 pl-5 marker:text-ink-tertiary">
            {omitted.map((item) => (
              <li key={item.render_id} className="wrap-anywhere">
                {omissionLabel(item)}: {REASONS[item.reason]}
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

function ZipGlyph() {
  return (
    <svg
      viewBox="0 0 16 16"
      fill="none"
      aria-hidden="true"
      className="size-3.5 shrink-0"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d="M8 2.5v8M4.5 7.5L8 11l3.5-3.5M3 13.5h10" />
    </svg>
  );
}
