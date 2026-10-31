# AutoClipper

> **Find the highlights in a long Twitch stream.**
> Paste a Twitch VOD link, get its best moments ranked, preview and trim them, and export vertical, captioned clips ready to post, or one highlights video to watch.

![Status: pre-alpha, foundations](https://img.shields.io/badge/status-foundations-lightgrey)
![License: MIT](https://img.shields.io/badge/license-MIT-blue)

> [!IMPORTANT]
> **Project status: foundations.** The app doesn't work end to end yet.
> The repo holds the scope, architecture and plan, the Python tooling and CI, and the first building blocks: config and logging, an FFmpeg runner, the Twitch API client, the chat replay parser and a synthetic dataset generator. See [`docs/project/status.md`](docs/project/status.md) for what exists today.

---

## Start here (humans and AI agents)

Start with the task guide and current state, then read what's relevant:

1. [`AGENTS.md`](AGENTS.md): task workflow and pointers to the docs for your area.
2. [`docs/project/status.md`](docs/project/status.md): what exists right now and what's next.
3. [`docs/product/prd.md` § Scope at a glance](docs/product/prd.md#scope-at-a-glance): what we're building (must / should / could / won't).
4. [`docs/architecture/overview.md`](docs/architecture/overview.md): how the system fits together.
5. [`CONTRIBUTING.md`](CONTRIBUTING.md): how to pick up an issue, open a PR and get it merged.
6. The issue you're working on ([GitHub issues](https://github.com/adillpickles/AutoClipper/issues), ordered by week in the [roadmap](docs/product/roadmap.md)), plus the docs [`AGENTS.md` § 3](AGENTS.md#3-what-to-read-for-which-task) lists for that kind of task.

## The problem

Twitch streams run 4–12+ hours, but the moments worth watching or posting add up to a few minutes. Finding them means scrubbing footage by hand.

| Who | Pain today | With AutoClipper |
|---|---|---|
| **Viewer** who missed the stream | Can't sit through a 7-hour VOD | Watch one highlights video of the best moments |
| **Small streamer** without an editor or clippers | Their best moments never get posted | Paste the VOD, download captioned vertical clips in minutes |
| **Clipper / editor** | Hours of scrubbing for 30 seconds of gold | Get the top 10 moments as ready-to-post vertical clips |

Viewer-made clips help, but they're duplicated, incomplete, missing for smaller streams, and their view counts measure popularity rather than whether a moment works as a highlight.

## What we're building

**The MVP:** paste a VOD link (and optionally the path to a VOD you already downloaded) → the pipeline pulls viewer clips, chat and audio → chat and audio signals plus a trained model rank moments across the whole VOD (even when nobody clipped anything) → the app shows the top 10 as ranked, captioned 9:16 clips → nudge start/end and download them, or join them into one 16:9 highlights video.

The full scope, including what's explicitly out (live clipping, auto-posting, TikTok view prediction), is in the [PRD scope table](docs/product/prd.md#scope-at-a-glance).

## How it works

```text
Twitch VOD URL
   │
   ├─▶ 1. Collect        VOD info + viewer clips (Twitch API), chat replay, audio
   ├─▶ 2. Signals        chat activity and audio loudness in short windows across the VOD
   ├─▶ 3. Rank           baselines + trained model (logistic regression → LightGBM)
   ├─▶ 4. Clips          top 10 moments auto-cut to 9:16 with captions
   ├─▶ 5. Highlights     the top moments joined into one 16:9 video, on request
   └─▶ 6. App            ranked clip cards: preview, nudge start/end, download
```

Viewer clips double as **noisy training labels**: roughly, a stretch of the VOD that viewers clipped counts as a good moment ([modeling plan](docs/ml/modeling-plan.md#0-the-plan-in-plain-english)). The model is evaluated on streamers it never trained on and compared against simple baselines (random, biggest chat spike, loudest audio). Results get reported honestly, even if a baseline wins. See [evaluation](docs/ml/evaluation.md). No dataset or model exists yet.

## Tech stack

| Layer | Choice | Why | Background |
|---|---|---|---|
| Pipeline and ML | Python 3.12, uv, pandas/Polars, scikit-learn, LightGBM | All the ML is Python. uv gives one-command, reproducible setup. LightGBM learns combinations of tabular signals. | [Tooling](docs/decisions/0002-language-and-tooling.md) |
| Signals | Twitch Helix API, TwitchDownloader (chat), yt-dlp + librosa (audio) | Chat and audio are cheap per-second signals that run on a laptop. Video is only downloaded for clips we export. | [Build order](docs/decisions/0004-staged-scope-chat-and-audio-first.md) |
| Data storage | Parquet files + DuckDB | Fast analytics on a laptop with no database server to run | [Storage](docs/decisions/0005-local-storage-parquet-duckdb.md) |
| Export | FFmpeg, faster-whisper | Standard tool for cutting, cropping and burning in captions; Whisper captions run on CPU | [Architecture § Export](docs/architecture/overview.md#46-export) |
| Backend | FastAPI + background worker | Same language as the ML. Processing a VOD takes minutes, so jobs run outside the request. | [App stack](docs/decisions/0007-web-application-stack.md) |
| Frontend | Next.js + TypeScript + Tailwind | The team knows it; supports a polished UI with a custom highlight timeline | [App stack](docs/decisions/0007-web-application-stack.md) |
| Quality | Ruff, Pyright, pytest, pre-commit, GitHub Actions | Fast, deterministic feedback for humans and agents. Actions is free for this public repo. | [Tooling](docs/decisions/0002-language-and-tooling.md) |
| Delivery | Local setup first; website + download (stretch) | A laptop is enough to start. Shared collection/storage and hosted deployment are options as the product develops. | [Data and delivery](docs/decisions/0011-data-sources-and-local-delivery.md) |

## Documentation

| If you want to… | Read |
|---|---|
| Know what we're building and why | [PRD](docs/product/prd.md) |
| See the plan, milestones and dates | [Roadmap](docs/product/roadmap.md) |
| Find something to work on | [GitHub issues](https://github.com/adillpickles/AutoClipper/issues), ordered by week in the [roadmap](docs/product/roadmap.md) |
| Understand the system design | [Architecture](docs/architecture/overview.md) · [Data model](docs/architecture/data-model.md) |
| Work with the data | [Sources](docs/data/data-sources.md) · [Collection](docs/data/collection-plan.md) · [Labeling](docs/data/labeling.md) |
| Work on the models | [Modeling plan](docs/ml/modeling-plan.md) · [Evaluation](docs/ml/evaluation.md) |
| Set up a dev environment | [Setup](docs/development/setup.md) |
| Contribute a change | [CONTRIBUTING](CONTRIBUTING.md) |
| Work with AI coding agents | [AGENTS.md](AGENTS.md) · [Parallel work](docs/agents/parallel-work.md) |
| Understand why a choice was made | [Technical choices](docs/decisions/README.md) |
| See every doc | [`docs/README.md`](docs/README.md) |

## Contributing

Pick an issue, branch, open a PR that says `Closes #N`, and squash-merge once CI is green (a teammate's review is welcome but optional). [`CONTRIBUTING.md`](CONTRIBUTING.md) walks through every step, including PR titles, labels and screenshots for UI work.

## Data handling

Follow [data handling](docs/data/data-governance.md): hash chatter names, and keep media, data and secrets out of git.

## Background

AutoClipper is built by a four-person team (4 FPS) in the [CUNY Tech Prep](https://cunytechprep.org/) Data Science track, Fall 2026.

## License

[MIT](LICENSE).
