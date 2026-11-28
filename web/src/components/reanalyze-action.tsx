"use client";

import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { ApiRequestError, getApiClient } from "@/lib/api/client";

type State =
  | { kind: "idle" }
  | { kind: "confirm"; pending: boolean; error: string | null };

/**
 * "Re-analyze" on the results page (#187). Analyzing a VOD again normally returns the finished
 * analysis (§ 3.1), so this asks the server for a fresh one of the same VOD and video source
 * (§ 3.9) after a confirm, then follows its progress. The new analysis becomes the VOD's
 * Library row; this one stays on disk.
 */
export function ReanalyzeAction({ jobId }: { jobId: string }) {
  const router = useRouter();
  const [state, setState] = useState<State>({ kind: "idle" });
  // Focus follows the swap: into the confirm panel's first button when it opens, back to
  // "Re-analyze" on Cancel (the button that had focus is replaced each time).
  const container = useRef<HTMLDivElement>(null);
  const moveFocus = useRef(false);
  const confirming = state.kind === "confirm";
  useEffect(() => {
    if (!moveFocus.current) return;
    moveFocus.current = false;
    container.current?.querySelector<HTMLElement>("button")?.focus();
  }, [confirming]);
  function show(next: State) {
    moveFocus.current = true;
    setState(next);
  }

  async function reanalyze() {
    setState({ kind: "confirm", pending: true, error: null });
    try {
      const { job } = await getApiClient().reanalyzeJob(jobId);
      router.push(`/jobs/${job.job_id}`);
    } catch (error) {
      setState({
        kind: "confirm",
        pending: false,
        error:
          error instanceof ApiRequestError
            ? error.error.message
            : "Couldn't start the new analysis. Check that AutoClipper is running and try again.",
      });
    }
  }

  if (state.kind === "idle") {
    return (
      <div key="idle" ref={container} className="contents">
        <Button
          variant="ghost"
          size="sm"
          className="self-start"
          onClick={() => show({ kind: "confirm", pending: false, error: null })}
        >
          Re-analyze
        </Button>
      </div>
    );
  }

  return (
    <div
      key="confirm"
      ref={container}
      role="region"
      aria-label="Confirm re-analyzing this VOD"
      className="flex flex-col gap-3 rounded-control border border-border-subtle bg-surface-elevated p-4 text-sm text-ink-secondary"
    >
      <p className="font-semibold text-ink-primary">
        Analyze this VOD again from scratch?
      </p>
      <p className="leading-5">
        A new analysis runs and renders its top clips again. It replaces this
        one in the Library; these results and their clips stay on disk.
      </p>
      {state.error && (
        <p role="alert" className="font-medium text-danger">
          {state.error}
        </p>
      )}
      <div className="flex flex-wrap items-center gap-2">
        <Button
          variant="primary"
          size="sm"
          disabled={state.pending}
          onClick={() => void reanalyze()}
        >
          {state.pending ? "Starting…" : "Re-analyze"}
        </Button>
        <Button
          variant="secondary"
          size="sm"
          disabled={state.pending}
          onClick={() => show({ kind: "idle" })}
        >
          Cancel
        </Button>
      </div>
    </div>
  );
}
