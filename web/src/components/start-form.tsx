"use client";

import { useRouter } from "next/navigation";
import { useId, useState, type FormEvent } from "react";

import { ApiRequestError, getApiClient } from "@/lib/api/client";
import { INVALID_VOD_MESSAGE, looksAbsolute, parseVod } from "@/lib/api/vod";

const PATH_MESSAGE =
  "Use the full path of the file, like /Users/you/Videos/vod.mp4 or C:\\Videos\\vod.mp4.";

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

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setFormError(null);
    const badVod = parseVod(vod) === null;
    const badPath = videoPath.trim() !== "" && !looksAbsolute(videoPath);
    setVodError(badVod ? INVALID_VOD_MESSAGE : null);
    setPathError(badPath ? PATH_MESSAGE : null);
    if (badVod || badPath) return;
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
    <form onSubmit={submit} noValidate className="flex flex-col gap-6">
      <div className="flex flex-col gap-2">
        <label htmlFor={vodId} className="font-medium">
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
          onChange={(event) => setVod(event.target.value)}
          aria-invalid={vodError !== null}
          aria-describedby={vodError ? `${vodId}-error` : `${vodId}-hint`}
          className="rounded-lg border border-zinc-300 bg-white px-4 py-3 text-base outline-none focus-visible:ring-2 focus-visible:ring-amber-500 aria-invalid:border-red-600 dark:border-zinc-700 dark:bg-zinc-900 dark:aria-invalid:border-red-400"
        />
        {vodError ? (
          <p
            id={`${vodId}-error`}
            role="alert"
            className="text-sm text-red-700 dark:text-red-300"
          >
            {vodError}
          </p>
        ) : (
          <p
            id={`${vodId}-hint`}
            className="text-sm text-zinc-600 dark:text-zinc-400"
          >
            A past broadcast, or just its numeric ID.
          </p>
        )}
      </div>

      <div className="flex flex-col gap-2">
        <label htmlFor={pathId} className="font-medium">
          VOD file on this computer{" "}
          <span className="font-normal text-zinc-500 dark:text-zinc-400">
            (optional)
          </span>
        </label>
        <input
          id={pathId}
          name="video_path"
          type="text"
          autoComplete="off"
          spellCheck={false}
          placeholder="/Users/you/Videos/vod.mp4"
          value={videoPath}
          onChange={(event) => setVideoPath(event.target.value)}
          aria-invalid={pathError !== null}
          aria-describedby={pathError ? `${pathId}-error` : `${pathId}-hint`}
          className="rounded-lg border border-zinc-300 bg-white px-4 py-3 font-mono text-sm outline-none focus-visible:ring-2 focus-visible:ring-amber-500 aria-invalid:border-red-600 dark:border-zinc-700 dark:bg-zinc-900 dark:aria-invalid:border-red-400"
        />
        {pathError ? (
          <p
            id={`${pathId}-error`}
            role="alert"
            className="text-sm text-red-700 dark:text-red-300"
          >
            {pathError}
          </p>
        ) : (
          <p
            id={`${pathId}-hint`}
            className="text-sm text-zinc-600 dark:text-zinc-400"
          >
            Already downloaded the VOD? Paste the file&apos;s full path and
            clips are cut from it instead of downloading the video again.
          </p>
        )}
      </div>

      {formError && (
        <p
          role="alert"
          className="rounded-lg bg-red-50 px-4 py-3 text-sm text-red-800 dark:bg-red-950/50 dark:text-red-200"
        >
          {formError}
        </p>
      )}

      <button
        type="submit"
        disabled={submitting}
        className="self-start rounded-lg bg-amber-500 px-5 py-3 font-semibold text-zinc-950 hover:bg-amber-400 focus-visible:ring-2 focus-visible:ring-amber-700 focus-visible:ring-offset-2 focus-visible:outline-none disabled:opacity-60 dark:focus-visible:ring-offset-zinc-950"
      >
        {submitting ? "Starting…" : "Find the best moments"}
      </button>
    </form>
  );
}
