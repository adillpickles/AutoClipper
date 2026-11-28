# Landing page

The public landing page at <https://adillpickles.github.io/AutoClipper/>. Plain HTML, CSS and a few lines of JavaScript; no build step. [`.github/workflows/pages.yml`](../.github/workflows/pages.yml) publishes this folder to GitHub Pages on every push to `main` that touches it.

## Preview locally

```bash
python -m http.server 8765 --directory site   # then open http://127.0.0.1:8765
```

Opening `index.html` directly also works, but some browsers block the fonts over `file://`.

## Files

| Path | What it is |
| --- | --- |
| `index.html` | The page, including the Open Graph tags |
| `styles.css` | Styles; colors and fonts follow `web/src/app/globals.css` (dark theme) |
| `main.js` | Copy buttons for the setup commands, and the demo clip's play-when-visible (the page works without it) |
| `assets/screen-*.webp` | App screenshots, taken in mock mode with sample data and test-pattern video |
| `assets/demo.mp4`, `assets/demo.webm`, `assets/demo-poster.webp` | The silent 14-second demo clip (H.264 and VP9) and its poster, recorded in mock mode; `.gitignore` allows exactly these two video files |
| `assets/og.png` | 1200×630 social preview image |
| `assets/fonts/` | Latin subsets of Space Grotesk, Plus Jakarta Sans and JetBrains Mono (SIL Open Font License 1.1, from `@fontsource-variable`) |

## Updating

- **Screenshots:** run the web app in mock mode (`cd web && npm run mock-media && npm run dev`), capture at 1440 px wide in dark mode, and save as WebP under ~150 KB. Never use frames from a real streamer's video. The README's screenshots live in `docs/img/screenshots/` and are taken the same way.
- **Demo clip:** record the demo path in mock mode (start → progress → results → clip editor), speed it up to 15 s or less, and encode it silent as H.264 MP4 (`-movflags +faststart`) and VP9 WebM, each under 500 KB (the `check-added-large-files` hook limit), plus a WebP poster frame. The `<video>` has no `autoplay` attribute and `preload="none"`: `main.js` plays it while it's on screen and leaves it paused for visitors who prefer reduced motion.
- Keep images small and the page free of frameworks and third-party scripts.
