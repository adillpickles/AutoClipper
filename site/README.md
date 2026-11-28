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
| `main.js` | Copy buttons for the setup commands (the page works without it) |
| `assets/screen-*.webp` | App screenshots, taken in mock mode with sample data and test-pattern video |
| `assets/og.png` | 1200×630 social preview image |
| `assets/fonts/` | Latin subsets of Space Grotesk, Plus Jakarta Sans and JetBrains Mono (SIL Open Font License 1.1, from `@fontsource-variable`) |

## Updating

- **Screenshots:** run the web app in mock mode (`cd web && npm run mock-media && npm run dev`), capture at 1440 px wide in dark mode, and save as WebP under ~300 KB. Never use frames from a real streamer's video.
- **Demo video:** replace the `demo-placeholder` block in `index.html`; the comment above it has a YouTube embed and a `<video>` snippet. Prefer a YouTube or other hosted link; a committed clip must be small (under ~5 MB).
- Keep images small and the page free of frameworks and third-party scripts.
