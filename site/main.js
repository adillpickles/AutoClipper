// Copy buttons for the setup commands. The page works without JavaScript; the
// buttons stay hidden unless the Clipboard API is available.
(() => {
  if (!navigator.clipboard) return;
  for (const button of document.querySelectorAll("[data-copy]")) {
    const code = button.parentElement.querySelector("code");
    if (!code) continue;
    button.hidden = false;
    button.setAttribute("aria-label", `Copy command: ${code.textContent.split("\n")[0]}`);
    button.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(code.textContent.trim());
        button.textContent = "Copied";
        button.dataset.done = "";
      } catch {
        button.textContent = "Select and copy";
      }
      setTimeout(() => {
        button.textContent = "Copy";
        delete button.dataset.done;
      }, 1800);
    });
  }
})();

// Demo clip: play it while it's on screen, and only when the visitor hasn't asked for reduced
// motion. The markup has no autoplay attribute and preload="none", so nothing downloads until
// the clip is about to scroll into view; without JavaScript it's a poster with controls.
(() => {
  const video = document.querySelector("video[data-autoplay]");
  if (!video || !("IntersectionObserver" in window)) return;
  const reduce = window.matchMedia("(prefers-reduced-motion: reduce)");
  let visible = false;
  let pausedByUser = false;
  const sync = () => {
    if (visible && !reduce.matches && !pausedByUser) {
      video.play().catch(() => {});
    } else if (!video.paused) {
      video.pause();
    }
  };
  video.addEventListener("pause", () => {
    if (visible && !reduce.matches && document.visibilityState === "visible") pausedByUser = true;
  });
  video.addEventListener("play", () => {
    pausedByUser = false;
  });
  new IntersectionObserver(
    (entries) => {
      visible = entries.some((entry) => entry.isIntersecting);
      sync();
    },
    { rootMargin: "200px 0px", threshold: 0.25 },
  ).observe(video);
  reduce.addEventListener("change", sync);
})();
