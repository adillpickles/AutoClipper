"use client";

import { useEffect, useId, useState } from "react";

import { Button } from "@/components/ui/button";
import { ErrorState } from "@/components/ui/error-state";
import { Skeleton } from "@/components/ui/skeleton";
import { ApiRequestError, getApiClient } from "@/lib/api/client";
import type { AppSettings, OutputCopy } from "@/lib/api/types";

type Load =
  | { kind: "loading" }
  | { kind: "error"; message: string }
  | { kind: "ready"; settings: AppSettings };

const UNREACHABLE =
  "Couldn't reach the server. Check that AutoClipper is running, then try again.";

function errorMessage(error: unknown): string {
  return error instanceof ApiRequestError ? error.error.message : UNREACHABLE;
}

function LastCopy({ copy }: { copy: OutputCopy }) {
  const what = copy.kind === "export" ? "clip" : "highlights video";
  if (copy.state === "copied") {
    return (
      <p className="text-sm wrap-anywhere text-ink-secondary">
        Last saved {what}:{" "}
        <span className="font-mono text-xs text-ink-primary">
          {copy.filename}
        </span>
      </p>
    );
  }
  return (
    <p className="text-sm font-medium text-danger">
      The last {what} wasn&apos;t saved to the folder. {copy.message}
    </p>
  );
}

/** The settings page: the output folder finished renders are also copied into. */
export function SettingsView() {
  const inputId = useId();
  const [load, setLoad] = useState<Load>({ kind: "loading" });
  const [attempt, setAttempt] = useState(0);
  const [draft, setDraft] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  useEffect(() => {
    let cancelled = false;
    getApiClient()
      .getSettings()
      .then((settings) => {
        if (cancelled) return;
        setLoad({ kind: "ready", settings });
        setDraft(settings.output_folder ?? "");
      })
      .catch((reason: unknown) => {
        if (!cancelled)
          setLoad({ kind: "error", message: errorMessage(reason) });
      });
    return () => {
      cancelled = true;
    };
  }, [attempt]);

  async function save(folder: string | null) {
    setSaving(true);
    setError("");
    setNotice("");
    try {
      const settings = await getApiClient().updateSettings({
        output_folder: folder,
      });
      setLoad({ kind: "ready", settings });
      setDraft(settings.output_folder ?? "");
      setNotice(
        settings.output_folder
          ? "Saved. New clips and highlights videos will be copied there."
          : "Cleared. Finished files stay in the app only.",
      );
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-col gap-2">
        <h1 className="font-display text-2xl font-semibold tracking-tight sm:text-3xl">
          Settings
        </h1>
        <p className="text-sm text-ink-secondary">
          Settings are saved on this computer by the AutoClipper server.
        </p>
      </div>

      {load.kind === "loading" && (
        <div className="flex flex-col gap-3" aria-busy="true">
          <span className="sr-only">Loading settings…</span>
          <Skeleton className="h-6 w-40" />
          <Skeleton className="h-12 w-full" />
        </div>
      )}

      {load.kind === "error" && (
        <ErrorState
          title="Can't load settings"
          message={load.message}
          primaryAction={
            <Button
              size="sm"
              onClick={() => {
                setLoad({ kind: "loading" });
                setAttempt((n) => n + 1);
              }}
            >
              Try again
            </Button>
          }
        />
      )}

      {load.kind === "ready" && (
        <form
          className="flex flex-col gap-4 rounded-panel border border-border-subtle bg-surface-panel p-5 shadow-panel sm:p-7"
          onSubmit={(event) => {
            event.preventDefault();
            void save(draft.trim() || null);
          }}
        >
          <div className="flex flex-col gap-1">
            <h2 className="font-display text-lg font-semibold text-ink-primary">
              Output folder
            </h2>
            <p className="text-sm text-ink-secondary">
              Every clip and highlights video that finishes rendering is also
              copied here, under its download name. Files already in the folder
              are never replaced.
            </p>
          </div>
          <div className="flex flex-col gap-2">
            <label
              htmlFor={inputId}
              className="text-sm font-semibold text-ink-primary"
            >
              Folder path
            </label>
            <input
              id={inputId}
              type="text"
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              placeholder="~/Videos/AutoClipper"
              spellCheck={false}
              autoComplete="off"
              aria-invalid={error ? true : undefined}
              aria-describedby={`${inputId}-hint${error ? ` ${inputId}-error` : ""}`}
              className="rounded-control border border-border-strong bg-surface-elevated px-4 py-3 font-mono text-sm text-ink-primary transition-colors placeholder:text-ink-tertiary focus-visible:border-accent focus-visible:ring-2 focus-visible:ring-focus-ring focus-visible:outline-none aria-invalid:border-danger"
            />
            {error ? (
              <p
                id={`${inputId}-error`}
                role="alert"
                className="text-sm font-medium text-danger"
              >
                {error}
              </p>
            ) : null}
            <p id={`${inputId}-hint`} className="text-sm text-ink-secondary">
              A full path on this computer, such as C:\Users\you\Videos or
              ~/Videos. A folder that doesn&apos;t exist yet is created.
            </p>
          </div>
          <div className="flex flex-wrap gap-2">
            <Button type="submit" disabled={saving}>
              {saving ? "Saving…" : "Save folder"}
            </Button>
            {load.settings.output_folder && (
              <Button
                variant="secondary"
                disabled={saving}
                onClick={() => void save(null)}
              >
                Stop copying
              </Button>
            )}
          </div>
          <div role="status" className="flex flex-col gap-1">
            {notice && <p className="text-sm text-ink-secondary">{notice}</p>}
            {load.settings.last_copy && (
              <LastCopy copy={load.settings.last_copy} />
            )}
          </div>
        </form>
      )}
    </div>
  );
}
