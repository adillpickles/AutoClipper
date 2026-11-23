"use client";

import { useRouter } from "next/navigation";
import { useId, useState, type FormEvent } from "react";

import { ApiRequestError, getApiClient } from "@/lib/api/client";
import { looksAbsolute, parseVod, vodInputMessage } from "@/lib/api/vod";

const PATH_MESSAGE =
  "Use the full path of the file, like /Users/you/Videos/vod.mp4 or C:\\Videos\\vod.mp4.";

const VOD_EXAMPLES = [
  {
    label: "Full link",
    value: "https://www.twitch.tv/videos/2345678901",
  },
  { label: "Short link", value: "twitch.tv/videos/2345678901" },
  { label: "VOD ID", value: "2345678901" },
] as const;

const PATH_EXAMPLES = [
  { label: "macOS path", value: "/Users/you/Videos/vod.mp4" },
  { label: "Windows path", value: "C:\\Videos\\vod.mp4" },
] as const;

export function StartForm() {
  const router = useRouter();
  const vodId = useId();
  const pathId = useId();
  const [vod, setVod] = useState("");
  const [videoPath, setVideoPath] = useState("");
  const [vodError, setVodError] = useState<string | null>(null);
  const [pathError, setPathError] = useState<string | null>(null);
  const [formError, setFormError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const parsedId = parseVod(vod);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setFormError(null);
    const nextVodError = vodInputMessage(vod);
    const badPath = videoPath.trim() !== "" && !looksAbsolute(videoPath);
    setVodError(nextVodError);
    setPathError(badPath ? PATH_MESSAGE : null);
    if (nextVodError || badPath) return;
    setSubmitting(true);
    try {
      const { job } = await getApiClient().createJob({
        vod,
        video_path: videoPath.trim() || null,
      });
      router.push(`/jobs/${job.job_id}`);
    } catch (error) {
      setSubmitting(false);
      if (
        error instanceof ApiRequestError &&
        error.error.code === "invalid_vod_link"
      ) {
        setVodError(error.error.message);
      } else if (
        error instanceof ApiRequestError &&
        error.error.code === "invalid_video_path"
      ) {
        setPathError(error.error.message);
      } else if (error instanceof ApiRequestError) {
        setFormError(error.error.message);
      } else {
        setFormError(
          "Can't reach the server. Check that it's running and try again.",
        );
      }
    }
  }

  return (
    <form
      onSubmit={submit}
      noValidate
      aria-labelledby="analyze-heading"
      className="flex flex-col gap-6 rounded-panel border border-border-subtle bg-surface-panel p-5 shadow-panel sm:p-7"
    >
      <div className="flex flex-col gap-1">
        <h2
          id="analyze-heading"
          className="font-display text-lg font-semibold text-ink-primary"
        >
          Analyze a VOD
        </h2>
        <p className="text-sm text-ink-secondary">
          Paste the broadcast link. Add a local file only if you already
          downloaded the video.
        </p>
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
          placeholder="https://www.twitch.tv/videos/2345678901"
          value={vod}
          onChange={(event) => {
            setVod(event.target.value);
            setVodError(null);
          }}
          aria-invalid={vodError !== null}
          aria-describedby={
            vodError ? `${vodId}-error` : `${vodId}-hint ${vodId}-examples`
          }
          className="rounded-control border border-border-strong bg-surface-elevated px-4 py-3 text-base text-ink-primary transition-colors placeholder:text-ink-tertiary focus-visible:border-accent focus-visible:ring-2 focus-visible:ring-focus-ring focus-visible:outline-none aria-invalid:border-danger"
        />
        {vodError ? (
          <p
            id={`${vodId}-error`}
            role="alert"
            className="text-sm font-medium text-danger"
          >
            {vodError}
          </p>
        ) : (
          <p id={`${vodId}-hint`} className="text-sm text-ink-secondary">
            {parsedId
              ? `Ready to analyze VOD ${parsedId}. Chat and viewer clips come from this broadcast.`
              : "A past broadcast: a twitch.tv/videos/… link, or just its numeric ID."}
          </p>
        )}
        <div className="flex flex-col gap-2 pt-1">
          <p
            id={`${vodId}-examples`}
            className="text-xs font-medium text-ink-tertiary"
          >
            Fill the field with an example format
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
                onClick={() => {
                  setVod(example.value);
                  setVodError(null);
                }}
                className="rounded-pill border border-border-subtle bg-surface-elevated px-3 py-1 text-xs font-medium text-ink-secondary transition-colors hover:border-accent-border hover:text-ink-primary focus-visible:ring-2 focus-visible:ring-focus-ring focus-visible:outline-none"
              >
                {example.label}
              </button>
            ))}
          </div>
        </div>
      </div>

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
          placeholder="/Users/you/Videos/vod.mp4"
          value={videoPath}
          onChange={(event) => {
            setVideoPath(event.target.value);
            setPathError(null);
          }}
          aria-invalid={pathError !== null}
          aria-describedby={
            pathError ? `${pathId}-error` : `${pathId}-hint ${pathId}-examples`
          }
          className="rounded-control border border-border-strong bg-surface-elevated px-4 py-3 font-mono text-sm text-ink-primary transition-colors placeholder:text-ink-tertiary focus-visible:border-accent focus-visible:ring-2 focus-visible:ring-focus-ring focus-visible:outline-none aria-invalid:border-danger"
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
            Already downloaded the VOD? Paste the file&apos;s full path and
            clips are cut from it instead of downloading the video again. The
            link above is still required for chat and viewer clips.
          </p>
        )}
        <div
          className="flex flex-wrap gap-2"
          role="group"
          aria-label="Example file paths"
        >
          <p id={`${pathId}-examples`} className="sr-only">
            Example file paths
          </p>
          {PATH_EXAMPLES.map((example) => (
            <button
              key={example.label}
              type="button"
              onClick={() => {
                setVideoPath(example.value);
                setPathError(null);
              }}
              className="rounded-pill border border-border-subtle bg-surface-elevated px-3 py-1 text-xs font-medium text-ink-secondary transition-colors hover:border-accent-border hover:text-ink-primary focus-visible:ring-2 focus-visible:ring-focus-ring focus-visible:outline-none"
            >
              {example.label}
            </button>
          ))}
        </div>
      </div>

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
        disabled={submitting}
        className="self-start rounded-control bg-accent px-5 py-3 text-sm font-semibold text-accent-ink shadow-card transition-colors hover:bg-accent-hover focus-visible:ring-2 focus-visible:ring-focus-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface-panel focus-visible:outline-none disabled:cursor-not-allowed disabled:opacity-60"
      >
        {submitting ? "Starting…" : "Find the best moments"}
      </button>
    </form>
  );
}
