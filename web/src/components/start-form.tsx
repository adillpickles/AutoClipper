"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import {
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
  type ChangeEvent,
  type DragEvent,
  type FormEvent,
} from "react";

import { Button, FOCUS_RING, TEXT_LINK } from "@/components/ui/button";
import { cardClasses } from "@/components/ui/card";
import { FieldMessage, Input, Label } from "@/components/ui/input";
import { Notice } from "@/components/ui/notice";
import { parseChannel } from "@/lib/api/channel";
import { ApiRequestError, getApiClient, isMockMode } from "@/lib/api/client";
import type { UploadProgress, VideoUpload } from "@/lib/api/types";
import {
  hasSupportedVideoExtension,
  isVodFormatExample,
  looksAbsolute,
  parseVod,
  VIDEO_FILE_ACCEPT,
  VOD_FORMAT_EXAMPLES,
  vodInputMessage,
} from "@/lib/api/vod";
import { formatBytes } from "@/lib/format";
import { markJustRequested } from "@/lib/stage-pacing";

const PATH_MESSAGE =
  "Use the full path of the file, like /Users/you/Videos/vod.mp4 or C:\\Videos\\vod.mp4.";

const UNSUPPORTED_FILE_MESSAGE =
  "Unsupported file type. Choose a video file (.mp4, .mkv, .ts, .mov, .webm, .flv, or .avi).";

const EMPTY_FILE_MESSAGE = "That file is empty. Choose a complete video file.";

const VOD_EXAMPLES = [
  { label: "Full link", value: VOD_FORMAT_EXAMPLES[0] },
  { label: "Short link", value: VOD_FORMAT_EXAMPLES[1] },
  { label: "VOD ID", value: VOD_FORMAT_EXAMPLES[2] },
] as const;

const EXAMPLE_VOD_MESSAGE =
  "That's an example of the format. Replace 2345678901 with your own VOD before analyzing.";

type VideoSourceMode = "upload" | "path";

export function StartForm() {
  const router = useRouter();
  const vodId = useId();
  const fileId = useId();
  const pathId = useId();
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const abortControllerRef = useRef<AbortController | null>(null);

  const [vod, setVod] = useState("");
  const [filledFromExample, setFilledFromExample] = useState(false);
  const [sourceMode, setSourceMode] = useState<VideoSourceMode>("upload");

  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [uploadedRef, setUploadedRef] = useState<{
    file: File;
    upload: VideoUpload;
  } | null>(null);
  const [isDragging, setIsDragging] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [uploadProgress, setUploadProgress] = useState<UploadProgress | null>(
    null,
  );
  const [selectionError, setSelectionError] = useState<string | null>(null);
  const [requestError, setRequestError] = useState<string | null>(null);
  const [uploadNotice, setUploadNotice] = useState<string | null>(null);

  const [videoPath, setVideoPath] = useState("");
  const [vodError, setVodError] = useState<string | null>(null);
  const [pathError, setPathError] = useState<string | null>(null);
  const [formError, setFormError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const parsedId = parseVod(vod);
  const parsedChannelLogin = parsedId === null ? parseChannel(vod) : null;
  const exampleOnly =
    filledFromExample && isVodFormatExample(vod) && !isMockMode();
  const uploadError = selectionError ?? requestError;

  // Clears the in-flight markers (button label, disabled controls, upload
  // bar). The typed link, chosen file and path are kept so the user can start
  // again with the same input.
  function resetPending() {
    setSubmitting(false);
    setUploading(false);
    setUploadProgress(null);
    setIsDragging(false);
  }

  // The form stays "Starting…" after a successful submit so a second click
  // can't start another job before the job page opens. With cacheComponents,
  // Next keeps this page in a hidden <Activity> instead of unmounting it, so
  // that state would still be there when the user comes back (#184). Effect
  // cleanup runs when the page is hidden (and on a real unmount): abort any
  // request still running and reset before the page can be shown again.
  // useLayoutEffect makes the reset land before the page is visible again.
  useLayoutEffect(() => {
    return () => {
      const controller = abortControllerRef.current;
      abortControllerRef.current = null;
      controller?.abort();
      resetPending();
    };
  }, []);

  // A full-page back/forward cache restore brings the page back without
  // running effects again, so reset there too.
  useEffect(() => {
    function handlePageShow(event: PageTransitionEvent) {
      if (!event.persisted) return;
      const controller = abortControllerRef.current;
      abortControllerRef.current = null;
      controller?.abort();
      resetPending();
    }
    window.addEventListener("pageshow", handlePageShow);
    return () => window.removeEventListener("pageshow", handlePageShow);
  }, []);

  // Editing the input means the user wants a fresh start. Only a stale state
  // is cleared: a request still in flight keeps its controller and stays
  // pending until it settles or is cancelled.
  function clearStalePending() {
    if (abortControllerRef.current === null && submitting) resetPending();
  }

  function selectFile(file: File) {
    clearStalePending();
    setSelectionError(null);
    setRequestError(null);
    setUploadNotice(null);
    setFormError(null);
    setUploadedRef(null);
    if (file.size <= 0) {
      setSelectedFile(null);
      setSelectionError(EMPTY_FILE_MESSAGE);
      return;
    }
    if (!hasSupportedVideoExtension(file.name)) {
      setSelectedFile(null);
      setSelectionError(UNSUPPORTED_FILE_MESSAGE);
      return;
    }
    setSelectedFile(file);
  }

  function handleFileChange(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0] ?? null;
    if (file) {
      selectFile(file);
    }
    event.target.value = "";
  }

  function clearSelectedFile() {
    setSelectedFile(null);
    setUploadedRef(null);
    setSelectionError(null);
    setRequestError(null);
    setUploadNotice(null);
    setUploadProgress(null);
  }

  function handleDragEnter(event: DragEvent<HTMLDivElement>) {
    event.preventDefault();
    event.stopPropagation();
    if (!submitting) {
      setIsDragging(true);
    }
  }

  function handleDragOver(event: DragEvent<HTMLDivElement>) {
    event.preventDefault();
    event.stopPropagation();
    if (!submitting && !isDragging) {
      setIsDragging(true);
    }
  }

  function handleDragLeave(event: DragEvent<HTMLDivElement>) {
    event.preventDefault();
    event.stopPropagation();
    const related = event.relatedTarget as Node | null;
    if (!related || !event.currentTarget.contains(related)) {
      setIsDragging(false);
    }
  }

  function handleDrop(event: DragEvent<HTMLDivElement>) {
    event.preventDefault();
    event.stopPropagation();
    setIsDragging(false);
    if (submitting) return;
    const file = event.dataTransfer.files?.[0] ?? null;
    if (file) {
      selectFile(file);
    }
  }

  function cancelUpload() {
    abortControllerRef.current?.abort();
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (submitting || abortControllerRef.current !== null) return;
    setFormError(null);
    setUploadNotice(null);

    const nextVodError = exampleOnly
      ? EXAMPLE_VOD_MESSAGE
      : vodInputMessage(vod);
    const badPath =
      sourceMode === "path" &&
      videoPath.trim() !== "" &&
      !looksAbsolute(videoPath);

    setVodError(nextVodError);
    setPathError(badPath ? PATH_MESSAGE : null);
    if (nextVodError || badPath || (sourceMode === "upload" && selectionError))
      return;

    const controller = new AbortController();
    abortControllerRef.current = controller;
    setRequestError(null);
    setSubmitting(true);
    let wasUploading = false;
    try {
      const api = getApiClient();
      let uploadId: string | null = null;

      if (sourceMode === "upload" && selectedFile !== null) {
        if (uploadedRef && uploadedRef.file === selectedFile) {
          uploadId = uploadedRef.upload.upload_id;
        } else {
          wasUploading = true;
          setUploading(true);
          setUploadProgress({
            loaded: 0,
            total: selectedFile.size > 0 ? selectedFile.size : null,
            fraction: selectedFile.size > 0 ? 0 : null,
          });
          const uploaded = await api.uploadVideo(selectedFile, {
            onProgress: (progress) => {
              if (!controller.signal.aborted) setUploadProgress(progress);
            },
            signal: controller.signal,
          });
          controller.signal.throwIfAborted();
          setUploadedRef({ file: selectedFile, upload: uploaded });
          uploadId = uploaded.upload_id;
          setUploading(false);
          wasUploading = false;
        }
      }

      const { job } = await api.createJob(
        uploadId !== null
          ? { vod, video_path: null, upload_id: uploadId }
          : {
              vod,
              video_path:
                sourceMode === "path" ? videoPath.trim() || null : null,
            },
      );
      if (!controller.signal.aborted) {
        // The server may hand back a job that finished earlier (#186); the progress page still
        // walks through its stages for a request made just now (#188).
        markJustRequested(job.job_id);
        router.push(`/jobs/${job.job_id}`);
      }
    } catch (error) {
      if (abortControllerRef.current !== controller) return;
      setUploading(false);
      setSubmitting(false);
      setUploadProgress(null);

      if (
        typeof error === "object" &&
        error !== null &&
        "name" in error &&
        error.name === "AbortError"
      ) {
        setUploadNotice(
          "Upload cancelled. Your file is still selected if you want to try again.",
        );
        return;
      }
      if (error instanceof ApiRequestError) {
        if (error.error.code === "invalid_vod_link") {
          setVodError(error.error.message);
        } else if (error.error.code === "invalid_video_path") {
          setPathError(error.error.message);
        } else if (error.error.code === "invalid_video_upload") {
          setUploadedRef(null);
          setRequestError(error.error.message);
        } else {
          setFormError(error.error.message);
        }
      } else if (wasUploading) {
        setRequestError(
          "Upload failed before finishing. Check that the server is running and try again.",
        );
      } else {
        setFormError(
          "Can't reach the server. Check that it's running and try again.",
        );
      }
    } finally {
      if (abortControllerRef.current === controller) {
        abortControllerRef.current = null;
      }
    }
  }

  const uploadPercent =
    uploadProgress?.fraction !== null && uploadProgress?.fraction !== undefined
      ? Math.round(uploadProgress.fraction * 100)
      : null;

  return (
    <form
      onSubmit={submit}
      noValidate
      aria-labelledby="analyze-heading"
      className={cardClasses({
        tone: "panel",
        padding: "none",
        className: "flex flex-col",
      })}
    >
      <div className="flex flex-col gap-5 p-5 sm:p-7">
        <div className="flex flex-col gap-1">
          <h2
            id="analyze-heading"
            className="font-display text-xl font-semibold tracking-[-0.02em] text-ink-primary"
          >
            Analyze a VOD
          </h2>
          <p className="text-sm leading-6 text-ink-secondary">
            Paste a past broadcast from Twitch. That&apos;s all it needs.
          </p>
        </div>

        <div className="flex flex-col gap-2">
          <Label htmlFor={vodId}>Twitch VOD link</Label>
          <Input
            id={vodId}
            name="vod"
            type="text"
            inputMode="url"
            autoComplete="off"
            spellCheck={false}
            inputSize="lg"
            disabled={submitting}
            placeholder="https://www.twitch.tv/videos/2345678901"
            value={vod}
            onChange={(event) => {
              clearStalePending();
              setVod(event.target.value);
              setFilledFromExample(false);
              setVodError(null);
            }}
            aria-invalid={vodError !== null}
            aria-describedby={
              vodError ? `${vodId}-error` : `${vodId}-hint ${vodId}-examples`
            }
          />
          {vodError ? (
            <div className="flex flex-col gap-1">
              <FieldMessage id={`${vodId}-error`} role="alert" tone="error">
                {vodError}
              </FieldMessage>
              {parsedChannelLogin && (
                <p className="text-sm leading-6 text-ink-secondary">
                  Want to pick from this channel&apos;s recent broadcasts?{" "}
                  <Link
                    href={`/channels?channel=${encodeURIComponent(parsedChannelLogin)}`}
                    className={TEXT_LINK}
                  >
                    Open {parsedChannelLogin} in Channels
                  </Link>
                </p>
              )}
            </div>
          ) : (
            <FieldMessage
              id={`${vodId}-hint`}
              tone={parsedId && !exampleOnly ? "success" : "hint"}
            >
              {exampleOnly
                ? EXAMPLE_VOD_MESSAGE
                : parsedId
                  ? `Ready to analyze VOD ${parsedId}. Chat and viewer clips come from this broadcast.`
                  : "A past broadcast: a twitch.tv/videos/… link, or just its numeric ID."}
            </FieldMessage>
          )}
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1.5">
            <p id={`${vodId}-examples`} className="text-xs text-ink-tertiary">
              Example formats (swap in your own VOD):
            </p>
            <div
              className="flex flex-wrap gap-1.5"
              role="group"
              aria-label="Example VOD links"
            >
              {VOD_EXAMPLES.map((example) => (
                <button
                  key={example.label}
                  type="button"
                  disabled={submitting}
                  onClick={() => {
                    setVod(example.value);
                    setFilledFromExample(true);
                    setVodError(null);
                  }}
                  className={`min-h-7 rounded-pill border border-border-subtle px-2.5 text-xs font-medium text-ink-secondary transition-colors hover:border-accent-border hover:text-ink-primary disabled:cursor-not-allowed disabled:opacity-50 ${FOCUS_RING}`}
                >
                  {example.label}
                </button>
              ))}
            </div>
          </div>
        </div>
      </div>

      <fieldset className="flex min-w-0 flex-col gap-3 border-t border-border-subtle bg-surface-canvas/35 px-5 py-5 sm:px-7">
        <div className="flex flex-wrap items-start justify-between gap-x-4 gap-y-3">
          <div className="flex min-w-0 flex-col gap-1">
            <legend className="text-sm font-semibold text-ink-primary">
              Already have the video?{" "}
              <span className="font-normal text-ink-tertiary">(optional)</span>
            </legend>
            <p className="max-w-md text-sm leading-6 text-ink-secondary">
              Your file only replaces the media download. The Twitch VOD link
              above is still required for metadata, chat, and viewer clips.
            </p>
          </div>

          <div
            role="group"
            aria-label="Video file source"
            className="inline-flex shrink-0 rounded-control border border-border-strong bg-surface-canvas p-0.5"
          >
            {(
              [
                { mode: "upload", label: "Upload or drop file" },
                { mode: "path", label: "Use file path on this computer" },
              ] as const
            ).map((option) => {
              const pressed = sourceMode === option.mode;
              return (
                <button
                  key={option.mode}
                  type="button"
                  disabled={submitting}
                  aria-pressed={pressed}
                  onClick={() => {
                    if (option.mode === "upload") {
                      setSourceMode("upload");
                      setPathError(null);
                    } else {
                      setSourceMode("path");
                      setSelectionError(null);
                      setRequestError(null);
                      setUploadNotice(null);
                    }
                  }}
                  className={`min-h-8 rounded-[0.375rem] px-3 text-xs font-semibold transition-colors disabled:cursor-not-allowed disabled:opacity-50 ${FOCUS_RING} ${
                    pressed
                      ? "bg-surface-subtle text-ink-primary shadow-card"
                      : "text-ink-tertiary hover:text-ink-primary"
                  }`}
                >
                  {option.mode === "upload" ? (
                    option.label
                  ) : (
                    <>
                      <span className="sm:hidden" aria-hidden="true">
                        File path
                      </span>
                      <span className="max-sm:sr-only">{option.label}</span>
                    </>
                  )}
                </button>
              );
            })}
          </div>
        </div>

        {sourceMode === "upload" ? (
          <div className="flex flex-col gap-2">
            <input
              ref={fileInputRef}
              id={fileId}
              name="video_file"
              type="file"
              tabIndex={-1}
              accept={VIDEO_FILE_ACCEPT}
              disabled={submitting}
              aria-label="Choose a VOD video file"
              aria-invalid={uploadError !== null}
              aria-describedby={
                uploadError ? `${fileId}-error` : `${fileId}-hint`
              }
              onChange={handleFileChange}
              className="sr-only"
            />

            {selectedFile === null ? (
              <div
                data-testid="vod-dropzone"
                data-dragging={isDragging ? "true" : "false"}
                onDragEnter={handleDragEnter}
                onDragOver={handleDragOver}
                onDragLeave={handleDragLeave}
                onDrop={handleDrop}
                className={`flex flex-col items-start gap-3 rounded-card border border-dashed p-4 transition-colors sm:flex-row sm:items-center sm:justify-between ${
                  isDragging
                    ? "border-accent bg-accent-soft"
                    : uploadError
                      ? "border-danger-border bg-danger-soft"
                      : "border-border-strong hover:border-ink-tertiary/60"
                }`}
              >
                <div className="flex min-w-0 items-start gap-3">
                  <span
                    aria-hidden="true"
                    className={`mt-0.5 flex size-9 shrink-0 items-center justify-center rounded-control border ${
                      isDragging
                        ? "border-accent-border bg-accent text-accent-ink"
                        : "border-border-subtle bg-surface-elevated text-ink-tertiary"
                    }`}
                  >
                    <svg
                      viewBox="0 0 20 20"
                      fill="none"
                      className="size-4"
                      stroke="currentColor"
                      strokeWidth="1.8"
                      strokeLinecap="round"
                      strokeLinejoin="round"
                    >
                      <path d="M10 13V4M6.5 7.5 10 4l3.5 3.5M4 13.5v1A1.5 1.5 0 0 0 5.5 16h9a1.5 1.5 0 0 0 1.5-1.5v-1" />
                    </svg>
                  </span>
                  <div className="flex min-w-0 flex-col gap-0.5">
                    <p className="text-sm font-medium text-ink-primary">
                      {isDragging
                        ? "Drop video file to attach it"
                        : "Drop a video file here, or choose one"}
                    </p>
                    <p
                      id={`${fileId}-hint`}
                      className="text-xs leading-5 text-ink-tertiary"
                    >
                      .mp4, .mkv, .ts, .mov, .webm, .flv or .avi. Long VODs
                      stream straight to the server.
                    </p>
                  </div>
                </div>
                <Button
                  variant="secondary"
                  size="sm"
                  disabled={submitting}
                  aria-describedby={
                    uploadError ? `${fileId}-error` : `${fileId}-hint`
                  }
                  onClick={() => fileInputRef.current?.click()}
                  className="shrink-0"
                >
                  Choose video file
                </Button>
              </div>
            ) : (
              <div className="flex flex-col gap-3 rounded-card border border-border-strong bg-surface-elevated p-4">
                <div className="flex flex-wrap items-center justify-between gap-3">
                  <div className="flex min-w-0 flex-1 items-center gap-3">
                    <span
                      aria-hidden="true"
                      className="flex size-9 shrink-0 items-center justify-center rounded-control bg-surface-media text-ink-secondary"
                    >
                      <svg
                        viewBox="0 0 20 20"
                        fill="currentColor"
                        className="size-3.5"
                      >
                        <path d="M6 4.8v10.4c0 .8.9 1.3 1.6.9l8-5.2a1 1 0 0 0 0-1.7l-8-5.2C6.9 3.5 6 4 6 4.8z" />
                      </svg>
                    </span>
                    <div className="min-w-0 flex-1">
                      <p
                        title={selectedFile.name}
                        className="truncate font-mono text-sm font-medium text-ink-primary"
                      >
                        {selectedFile.name}
                      </p>
                      <p className="text-xs text-ink-secondary">
                        {formatBytes(selectedFile.size)}
                        {uploading
                          ? " · Uploading to server"
                          : uploadedRef?.file === selectedFile
                            ? " · Uploaded and verified"
                            : " · Ready to upload when analysis starts"}
                      </p>
                    </div>
                  </div>
                  {uploading ? (
                    <Button
                      variant="secondary"
                      size="sm"
                      onClick={cancelUpload}
                    >
                      Cancel upload
                    </Button>
                  ) : (
                    <div className="flex shrink-0 items-center gap-1">
                      <Button
                        variant="subtle"
                        size="sm"
                        disabled={submitting}
                        aria-describedby={
                          uploadError ? `${fileId}-error` : undefined
                        }
                        onClick={() => fileInputRef.current?.click()}
                      >
                        Change
                      </Button>
                      <Button
                        variant="ghost"
                        size="sm"
                        disabled={submitting}
                        onClick={clearSelectedFile}
                      >
                        Remove
                      </Button>
                    </div>
                  )}
                </div>

                {uploading && uploadProgress && (
                  <div className="flex flex-col gap-1.5">
                    <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-ink-secondary">
                      <span>
                        {uploadPercent !== null && uploadPercent >= 100
                          ? "Checking video with server…"
                          : `Uploading video…${uploadPercent !== null ? ` ${uploadPercent}%` : ""}`}
                      </span>
                      <span className="font-mono tabular-nums">
                        {formatBytes(uploadProgress.loaded)}
                        {uploadProgress.total !== null
                          ? ` / ${formatBytes(uploadProgress.total)}`
                          : ""}
                      </span>
                    </div>
                    <div className="h-1.5 w-full overflow-hidden rounded-pill bg-surface-subtle">
                      <div
                        role="progressbar"
                        aria-label="Video upload progress"
                        aria-valuemin={0}
                        aria-valuemax={100}
                        aria-valuenow={uploadPercent ?? 0}
                        className="h-full rounded-pill bg-accent transition-[width] duration-150"
                        style={{ width: `${uploadPercent ?? 100}%` }}
                      />
                    </div>
                  </div>
                )}
              </div>
            )}

            {uploadNotice && (
              <FieldMessage role="status">{uploadNotice}</FieldMessage>
            )}
            {uploadError && (
              <FieldMessage id={`${fileId}-error`} role="alert" tone="error">
                {uploadError}
              </FieldMessage>
            )}
          </div>
        ) : (
          <div className="flex flex-col gap-2">
            <Label htmlFor={pathId} optional>
              VOD file on this computer
            </Label>
            <Input
              id={pathId}
              name="video_path"
              type="text"
              autoComplete="off"
              spellCheck={false}
              mono
              disabled={submitting}
              placeholder="/Users/you/Videos/vod.mp4"
              value={videoPath}
              onChange={(event) => {
                clearStalePending();
                setVideoPath(event.target.value);
                setPathError(null);
              }}
              aria-invalid={pathError !== null}
              aria-describedby={
                pathError
                  ? `${pathId}-error`
                  : `${pathId}-hint ${pathId}-examples`
              }
            />
            {pathError ? (
              <FieldMessage id={`${pathId}-error`} role="alert" tone="error">
                {pathError}
              </FieldMessage>
            ) : (
              <FieldMessage id={`${pathId}-hint`}>
                Paste the file&apos;s full path to skip copying a large VOD when
                the server runs on this same computer.
              </FieldMessage>
            )}
            <p id={`${pathId}-examples`} className="text-xs text-ink-tertiary">
              Examples:{" "}
              <code className="font-mono">/Users/you/Videos/vod.mp4</code>
              {" or "}
              <code className="font-mono">C:\Videos\vod.mp4</code>
            </p>
          </div>
        )}
      </fieldset>

      <div className="flex flex-col gap-4 border-t border-border-subtle px-5 py-5 sm:px-7">
        {formError && (
          <Notice role="alert" tone="danger">
            {formError}
          </Notice>
        )}
        <div className="flex flex-col-reverse gap-4 sm:flex-row sm:items-center sm:justify-between">
          <p className="text-sm text-ink-secondary">
            Several streams?{" "}
            <Link href="/channels" className={`whitespace-nowrap ${TEXT_LINK}`}>
              Browse a channel&apos;s VODs
            </Link>
          </p>
          <Button
            type="submit"
            size="lg"
            disabled={
              submitting || (sourceMode === "upload" && selectionError !== null)
            }
            className="w-full sm:w-auto"
          >
            {uploading
              ? "Uploading video…"
              : submitting
                ? "Starting…"
                : "Find the best moments"}
          </Button>
        </div>
      </div>
    </form>
  );
}
