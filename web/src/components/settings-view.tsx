"use client";

import { useEffect, useId, useState } from "react";

import { Button } from "@/components/ui/button";
import { Card, CardHeader } from "@/components/ui/card";
import { ErrorState } from "@/components/ui/error-state";
import { FieldMessage, Input, Label } from "@/components/ui/input";
import { PageHeader } from "@/components/ui/page-header";
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
      <p className="flex flex-wrap items-baseline gap-x-2 text-sm wrap-anywhere text-ink-secondary">
        <span>Last saved {what}:</span>
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
    <div className="flex flex-col gap-8">
      <PageHeader
        title="Settings"
        description="Saved on this computer by the AutoClipper server."
      />

      {load.kind === "loading" && (
        <Card padding="lg" aria-busy="true" className="flex flex-col gap-4">
          <span className="sr-only">Loading settings…</span>
          <Skeleton className="h-5 w-36" />
          <Skeleton className="h-4 w-full max-w-md" />
          <Skeleton className="h-12 w-full" />
          <Skeleton className="h-10 w-32" />
        </Card>
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
        <Card
          as="form"
          padding="none"
          className="flex flex-col"
          onSubmit={(event) => {
            event.preventDefault();
            void save(draft.trim() || null);
          }}
        >
          <div className="flex flex-col gap-5 p-5 sm:p-7">
            <CardHeader
              title="Output folder"
              description="Every clip and highlights video that finishes rendering is also copied here, under its download name. Files already in the folder are never replaced."
            />
            <div className="flex flex-col gap-2">
              <Label htmlFor={inputId}>Folder path</Label>
              <Input
                id={inputId}
                type="text"
                mono
                value={draft}
                onChange={(event) => setDraft(event.target.value)}
                placeholder="~/Videos/AutoClipper"
                spellCheck={false}
                autoComplete="off"
                aria-invalid={error ? true : undefined}
                aria-describedby={`${inputId}-hint${error ? ` ${inputId}-error` : ""}`}
              />
              {error ? (
                <FieldMessage id={`${inputId}-error`} role="alert" tone="error">
                  {error}
                </FieldMessage>
              ) : null}
              <FieldMessage id={`${inputId}-hint`}>
                A full path on this computer, such as C:\Users\you\Videos or
                ~/Videos. A folder that doesn&apos;t exist yet is created.
              </FieldMessage>
            </div>
          </div>
          <div className="flex flex-col gap-3 border-t border-border-subtle px-5 py-4 sm:flex-row sm:items-center sm:justify-between sm:px-7">
            <div role="status" className="flex min-w-0 flex-col gap-1">
              {notice && (
                <p className="text-sm text-success-soft-ink">{notice}</p>
              )}
              {load.settings.last_copy && (
                <LastCopy copy={load.settings.last_copy} />
              )}
            </div>
            <div className="flex shrink-0 flex-wrap gap-2 sm:ml-auto">
              {load.settings.output_folder && (
                <Button
                  variant="ghost"
                  disabled={saving}
                  onClick={() => void save(null)}
                >
                  Stop copying
                </Button>
              )}
              <Button type="submit" disabled={saving}>
                {saving ? "Saving…" : "Save folder"}
              </Button>
            </div>
          </div>
        </Card>
      )}
    </div>
  );
}
