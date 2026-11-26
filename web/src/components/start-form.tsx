"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useId, useState, type FormEvent } from "react";

import { parseChannel } from "@/lib/api/channel";
import { ApiRequestError, getApiClient, isMockMode } from "@/lib/api/client";
import {
  isVodFormatExample,
  looksAbsolute,
  parseVod,
  VOD_FORMAT_EXAMPLES,
  vodInputMessage,
} from "@/lib/api/vod";

const PATH_MESSAGE =
  "Use the full path of the file, like /Users/you/Videos/vod.mp4 or C:\\Videos\\vod.mp4.";

const VOD_EXAMPLES = [
  { label: "Full link", value: VOD_FORMAT_EXAMPLES[0] },
  { label: "Short link", value: VOD_FORMAT_EXAMPLES[1] },
  { label: "VOD ID", value: VOD_FORMAT_EXAMPLES[2] },
] as const;

const EXAMPLE_VOD_MESSAGE =
  "That's an example of the format. Replace 2345678901 with your own VOD before analyzing.";

export function StartForm() {
  const router = useRouter();
  const vodId = useId();
  const pathId = useId();
  const [vod, setVod] = useState("");
  const [filledFromExample, setFilledFromExample] = useState(false);
  const [videoPath, setVideoPath] = useState("");
  const [vodError, setVodError] = useState<string | null>(null);
  const [pathError, setPathError] = useState<string | null>(null);
  const [formError, setFormError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const parsedId = parseVod(vod);
  const parsedChannelLogin = parsedId === null ? parseChannel(vod) : null;
  const exampleOnly =
    filledFromExample && isVodFormatExample(vod) && !isMockMode();

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setFormError(null);
    const nextVodError = exampleOnly
      ? EXAMPLE_VOD_MESSAGE
      : vodInputMessage(vod);
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
      <div className="flex flex-wrap items-start justify-between gap-3">
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
          className="rounded-control border border-border-strong bg-surface-elevated px-4 py-3 text-base text-ink-primary transition-colors placeholder:text-ink-tertiary focus-visible:border-accent focus-visible:ring-2 focus-visible:ring-focus-ring focus-visible:outline-none aria-invalid:border-danger"
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
                onClick={() => {
                  setVod(example.value);
                  setFilledFromExample(true);
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
        <p id={`${pathId}-examples`} className="text-xs text-ink-tertiary">
          Examples: <code className="font-mono">/Users/you/Videos/vod.mp4</code>
          {" or "}
          <code className="font-mono">C:\Videos\vod.mp4</code>
        </p>
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
