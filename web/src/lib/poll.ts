// Status polling that eases off in a background tab (#241). While the page is hidden a poll waits
// at least `HIDDEN_POLL_INTERVAL_MS`, and it runs at once when the page is shown again, so a user
// coming back sees fresh status without the app polling a tab nobody is looking at every 2 s.

/** The slowest a hidden tab polls (api-contract § 9.1). */
export const HIDDEN_POLL_INTERVAL_MS = 10_000;

function pageHidden(): boolean {
  return (
    typeof document !== "undefined" && document.visibilityState === "hidden"
  );
}

/**
 * Run `callback` after `delayMs` (or `HIDDEN_POLL_INTERVAL_MS` if longer while the page is
 * hidden; a hidden page that becomes visible runs it right away). Returns a cancel function.
 */
export function schedulePoll(
  callback: () => void,
  delayMs: number,
): () => void {
  let done = false;
  const hidden = pageHidden();
  const timer = setTimeout(
    fire,
    hidden ? Math.max(delayMs, HIDDEN_POLL_INTERVAL_MS) : delayMs,
  );
  function onVisibilityChange() {
    if (!pageHidden()) fire();
  }
  function stop() {
    done = true;
    clearTimeout(timer);
    if (hidden)
      document.removeEventListener("visibilitychange", onVisibilityChange);
  }
  function fire() {
    if (done) return;
    stop();
    callback();
  }
  if (hidden) document.addEventListener("visibilitychange", onVisibilityChange);
  return stop;
}

/** `schedulePoll` as a promise; `track` receives the cancel function (the promise then never settles). */
export function waitForPoll(
  delayMs: number,
  track: (cancel: () => void) => void,
): Promise<void> {
  return new Promise<void>((resolve) => track(schedulePoll(resolve, delayMs)));
}
