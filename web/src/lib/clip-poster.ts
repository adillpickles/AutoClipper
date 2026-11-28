// Poster frames for finished videos: the results screen's clip cards (#217), the clip editor's
// rendered clip and the highlights player (`usePosterFrame`). A `<video preload="metadata">`
// shows black until the browser decodes a frame, and over HTTP/1.1 a browser opens only about
// six connections per host: ten preloading videos can starve each other (and the export polls),
// leaving the last cards black. Instead each card's video stays `preload="none"` with a poster
// grabbed here: one short-lived hidden video per clip, a few at a time, that seeks to a frame
// inside the clip, draws it to a canvas and lets go of its connection.

/** Where in a clip the poster comes from: a little way in, past any black first frame. */
export function posterTime(durationS: number): number {
  if (!Number.isFinite(durationS) || durationS <= 0) return 0;
  return Math.min(1.5, durationS * 0.3);
}

/** Runs async tasks with at most `limit` at once, in the order they were queued. */
export function createLimiter(limit: number) {
  let active = 0;
  const waiting: (() => void)[] = [];
  function next() {
    if (active >= limit) return;
    const start = waiting.shift();
    if (!start) return;
    active += 1;
    start();
  }
  return function run<T>(task: () => Promise<T>): Promise<T> {
    return new Promise<T>((resolve, reject) => {
      waiting.push(() => {
        task()
          .then(resolve, reject)
          .finally(() => {
            active -= 1;
            next();
          });
      });
      next();
    });
  };
}

const POSTER_WIDTH = 360;
const CAPTURE_TIMEOUT_MS = 15_000;

const limit = createLimiter(2);
const posters = new Map<string, Promise<string>>();

/** Grab one frame of `url` as a JPEG data URL. Rejects on any load, CORS or decode problem. */
function capture(url: string, maxWidth: number): Promise<string> {
  return new Promise<string>((resolve, reject) => {
    const video = document.createElement("video");
    video.crossOrigin = "anonymous";
    video.muted = true;
    video.playsInline = true;
    video.preload = "auto";
    let settled = false;
    const finish = (error: Error | null, poster?: string) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      // Drop the source so the browser closes the connection right away.
      video.removeAttribute("src");
      video.load();
      if (error) reject(error);
      else resolve(poster as string);
    };
    const timer = setTimeout(
      () => finish(new Error("poster timed out")),
      CAPTURE_TIMEOUT_MS,
    );
    video.addEventListener("error", () => finish(new Error("poster load")));
    video.addEventListener("loadedmetadata", () => {
      video.currentTime = posterTime(video.duration);
    });
    video.addEventListener("seeked", () => {
      try {
        const width = Math.min(maxWidth, video.videoWidth || maxWidth);
        const height = Math.round(
          (width * (video.videoHeight || 16)) / (video.videoWidth || 9),
        );
        const canvas = document.createElement("canvas");
        canvas.width = width;
        canvas.height = height;
        const context = canvas.getContext("2d");
        if (!context) throw new Error("no canvas");
        context.drawImage(video, 0, 0, width, height);
        finish(null, canvas.toDataURL("image/jpeg", 0.82));
      } catch (error) {
        finish(error instanceof Error ? error : new Error("poster draw"));
      }
    });
    video.src = url;
  });
}

/**
 * The poster for a finished video, captured once per URL (and width) and shared by every view
 * showing it. `width` caps the frame's width in pixels (default 360, enough for a clip card).
 */
export function clipPoster(
  url: string,
  { width = POSTER_WIDTH }: { width?: number } = {},
): Promise<string> {
  const key = `${width} ${url}`;
  let poster = posters.get(key);
  if (!poster) {
    poster = limit(() => capture(url, width));
    // A failure isn't cached: the next mount may try again.
    poster.catch(() => posters.delete(key));
    posters.set(key, poster);
  }
  return poster;
}
