"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import {
  useEffect,
  useId,
  useRef,
  useState,
  type ChangeEvent,
  type DragEvent,
  type FormEvent,
} from "react";

import { Button } from "@/components/ui/button";
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

  useEffect(() => {
    return () => {
      const controller = abortControllerRef.current;
      abortControllerRef.current = null;
      controller?.abort();
    };
  }, []);

  function selectFile(file: File) {
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
      if (!controller.signal.aborted) router.push(`/jobs/${job.job_id}`);
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
      className="flex flex-col gap-6 rounded-panel border border-border-subtle bg-surface-panel p-5 shadow-panel sm:p-7"
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex flex-col gap-1">
          <h2
            id="analyze-heading"
            className="font-display text-lg font-semibold text-ink-primary"
          >
            Analyze a VOD
          </h2>
          <p className="text-sm text-ink-secondary">
            Paste the Twitch broadcast link. Add your own video file only if you
            already downloaded it.
          </p>
        </div>
        <Link
          href="/channels"
          className="inline-flex min-h-9 shrink-0 items-center gap-1.5 rounded-pill border border-accent-border bg-accent-soft px-3.5 py-1.5 text-xs font-semibold text-accent-soft-ink transition-colors hover:bg-accent hover:text-accent-ink focus-visible:ring-2 focus-visible:ring-focus-ring focus-visible:outline-none"
        >
          Browse a channel&apos;s VODs
          <span aria-hidden="true">&rarr;</span>
        </Link>
      </div>

      <div className="flex flex-col gap-2">
        <label
          htmlFor={vodId}
          className="text-sm font-semibold text-ink-primary"
        >
          Twitch VOD link
        </label>
        <input
          id={vodId}
          name="vod"
          type="text"
          inputMode="url"
          autoComplete="off"
          spellCheck={false}
          disabled={submitting}
          placeholder="https://www.twitch.tv/videos/2345678901"
          value={vod}
          onChange={(event) => {
            setVod(event.target.value);
            setFilledFromExample(false);
            setVodError(null);
          }}
          aria-invalid={vodError !== null}
          aria-describedby={
            vodError ? `${vodId}-error` : `${vodId}-hint ${vodId}-examples`
          }
          className="rounded-control border border-border-strong bg-surface-elevated px-4 py-3 text-base text-ink-primary transition-colors placeholder:text-ink-tertiary focus-visible:border-accent focus-visible:ring-2 focus-visible:ring-focus-ring focus-visible:outline-none disabled:cursor-not-allowed disabled:opacity-60 aria-invalid:border-danger"
        />
        {vodError ? (
          <div className="flex flex-col gap-1.5">
            <p
              id={`${vodId}-error`}
              role="alert"
              className="text-sm font-medium text-danger"
            >
              {vodError}
            </p>
            {parsedChannelLogin && (
              <p className="text-sm text-ink-secondary">
                Want to pick from this channel&apos;s recent broadcasts?{" "}
                <Link
                  href={`/channels?channel=${encodeURIComponent(parsedChannelLogin)}`}
                  className="font-semibold text-accent underline underline-offset-2 hover:text-accent-hover focus-visible:ring-2 focus-visible:ring-focus-ring focus-visible:outline-none"
                >
                  Open {parsedChannelLogin} in Channels
                </Link>
              </p>
            )}
          </div>
        ) : (
          <p id={`${vodId}-hint`} className="text-sm text-ink-secondary">
            {exampleOnly
              ? EXAMPLE_VOD_MESSAGE
              : parsedId
                ? `Ready to analyze VOD ${parsedId}. Chat and viewer clips come from this broadcast.`
                : "A past broadcast: a twitch.tv/videos/… link, or just its numeric ID."}
          </p>
        )}
        <div className="flex flex-col gap-2 pt-1">
          <p
            id={`${vodId}-examples`}
            className="text-xs font-medium text-ink-tertiary"
          >
            Example formats — replace the sample ID with your VOD
          </p>
          <div
            className="flex flex-wrap gap-2"
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
                className="rounded-pill border border-border-subtle bg-surface-elevated px-3 py-1 text-xs font-medium text-ink-secondary transition-colors hover:border-accent-border hover:text-ink-primary focus-visible:ring-2 focus-visible:ring-focus-ring focus-visible:outline-none disabled:cursor-not-allowed disabled:opacity-60"
              >
                {example.label}
              </button>
            ))}
          </div>
        </div>
      </div>

      <fieldset className="flex flex-col gap-3 rounded-card border border-border-subtle bg-surface-elevated/50 p-4">
        <div className="flex flex-col gap-1">
          <legend className="text-sm font-semibold text-ink-primary">
            Already have the VOD video?{" "}
            <span className="font-normal text-ink-tertiary">(optional)</span>
          </legend>
          <p className="text-sm text-ink-secondary">
            Your file only replaces the media download — the Twitch VOD link
            above is still required for metadata, chat, and viewer clips.
          </p>
        </div>

        <div
          role="group"
          aria-label="Video file source"
          className="flex flex-wrap gap-2"
        >
          <button
            type="button"
            disabled={submitting}
            aria-pressed={sourceMode === "upload"}
            onClick={() => {
              setSourceMode("upload");
              setPathError(null);
            }}
            className={`min-h-9 rounded-pill border px-3.5 py-1.5 text-xs font-semibold transition-colors focus-visible:ring-2 focus-visible:ring-focus-ring focus-visible:outline-none disabled:cursor-not-allowed disabled:opacity-60 ${
              sourceMode === "upload"
                ? "border-accent bg-accent text-accent-ink"
                : "border-border-strong bg-surface-panel text-ink-secondary hover:text-ink-primary"
            }`}
          >
            Upload or drop file
          </button>
          <button
            type="button"
            disabled={submitting}
            aria-pressed={sourceMode === "path"}
            onClick={() => {
              setSourceMode("path");
              setSelectionError(null);
              setRequestError(null);
              setUploadNotice(null);
            }}
            className={`min-h-9 rounded-pill border px-3.5 py-1.5 text-xs font-semibold transition-colors focus-visible:ring-2 focus-visible:ring-focus-ring focus-visible:outline-none disabled:cursor-not-allowed disabled:opacity-60 ${
              sourceMode === "path"
                ? "border-accent bg-accent text-accent-ink"
                : "border-border-strong bg-surface-panel text-ink-secondary hover:text-ink-primary"
            }`}
          >
            Use file path on this computer
          </button>
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
                className={`flex flex-col items-center justify-center gap-2.5 rounded-control border-2 border-dashed p-5 text-center transition-colors ${
                  isDragging
                    ? "border-accent bg-accent-soft text-accent-soft-ink"
                    : uploadError
                      ? "border-danger bg-danger-soft/40"
                      : "border-border-strong bg-surface-panel"
                }`}
              >
                <p className="text-sm font-medium text-ink-primary">
                  {isDragging
                    ? "Drop video file to attach it"
                    : "Drop a video file here, or choose one from your computer"}
                </p>
                <p id={`${fileId}-hint`} className="text-xs text-ink-secondary">
                  Supports .mp4, .mkv, .ts, .mov, .webm, .flv, and .avi. Large
                  4–6 hour VODs stream directly to the server.
                </p>
                <Button
                  variant="secondary"
                  size="sm"
                  disabled={submitting}
                  aria-describedby={
                    uploadError ? `${fileId}-error` : `${fileId}-hint`
                  }
                  onClick={() => fileInputRef.current?.click()}
                >
                  Choose video file
                </Button>
              </div>
            ) : (
              <div className="flex flex-col gap-3 rounded-control border border-border-strong bg-surface-panel p-4">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="min-w-0 flex-1">
                    <p className="truncate font-mono text-sm font-medium text-ink-primary">
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
                  {uploading ? (
                    <Button
                      variant="secondary"
                      size="sm"
                      onClick={cancelUpload}
                    >
                      Cancel upload
                    </Button>
                  ) : (
                    <div className="flex shrink-0 items-center gap-2">
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
                      <span className="font-mono">
                        {formatBytes(uploadProgress.loaded)}
                        {uploadProgress.total !== null
                          ? ` / ${formatBytes(uploadProgress.total)}`
                          : ""}
                      </span>
                    </div>
                    <div className="h-2 w-full overflow-hidden rounded-pill bg-surface-subtle">
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
              <p
                role="status"
                className="text-sm font-medium text-ink-secondary"
              >
                {uploadNotice}
              </p>
            )}
            {uploadError && (
              <p
                id={`${fileId}-error`}
                role="alert"
                className="text-sm font-medium text-danger"
              >
                {uploadError}
              </p>
            )}
          </div>
        ) : (
          <div className="flex flex-col gap-2">
            <label
              htmlFor={pathId}
              className="text-sm font-semibold text-ink-primary"
            >
              VOD file on this computer{" "}
              <span className="font-normal text-ink-tertiary">(optional)</span>
            </label>
            <input
              id={pathId}
              name="video_path"
              type="text"
              autoComplete="off"
              spellCheck={false}
              disabled={submitting}
              placeholder="/Users/you/Videos/vod.mp4"
              value={videoPath}
              onChange={(event) => {
                setVideoPath(event.target.value);
                setPathError(null);
              }}
              aria-invalid={pathError !== null}
              aria-describedby={
                pathError
                  ? `${pathId}-error`
                  : `${pathId}-hint ${pathId}-examples`
              }
              className="rounded-control border border-border-strong bg-surface-panel px-4 py-3 font-mono text-sm text-ink-primary transition-colors placeholder:text-ink-tertiary focus-visible:border-accent focus-visible:ring-2 focus-visible:ring-focus-ring focus-visible:outline-none disabled:cursor-not-allowed disabled:opacity-60 aria-invalid:border-danger"
            />
            {pathError ? (
              <p
                id={`${pathId}-error`}
                role="alert"
                className="text-sm font-medium text-danger"
              >
                {pathError}
              </p>
            ) : (
              <p id={`${pathId}-hint`} className="text-sm text-ink-secondary">
                Paste the file&apos;s full path to skip copying a large VOD when
                the server runs on this same computer.
              </p>
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

      {formError && (
        <p
          role="alert"
          className="rounded-control border border-danger-border bg-danger-soft px-4 py-3 text-sm text-danger-soft-ink"
        >
          {formError}
        </p>
      )}

      <button
        type="submit"
        disabled={
          submitting || (sourceMode === "upload" && selectionError !== null)
        }
        className="self-start rounded-control bg-accent px-5 py-3 text-sm font-semibold text-accent-ink shadow-card transition-colors hover:bg-accent-hover focus-visible:ring-2 focus-visible:ring-focus-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface-panel focus-visible:outline-none disabled:cursor-not-allowed disabled:opacity-60"
      >
        {uploading
          ? "Uploading video…"
          : submitting
            ? "Starting…"
            : "Find the best moments"}
      </button>
    </form>
  );
}
