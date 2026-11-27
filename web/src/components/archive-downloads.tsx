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
      <div className="flex flex-wrap gap-2">
        <Button
          variant="secondary"
          size="sm"
          disabled={busy}
          onClick={() => void download("clips")}
        >
          Download all clips (ZIP)
        </Button>
        <Button
          variant="secondary"
          size="sm"
          disabled={busy}
          onClick={() => void download("highlights")}
        >
          Download all highlights (ZIP)
        </Button>
      </div>
      <div role="status" className="text-sm text-ink-secondary">
        {message && <p>{message}</p>}
        {omitted.length > 0 && (
          <ul className="mt-1 list-disc space-y-1 pl-5">
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
