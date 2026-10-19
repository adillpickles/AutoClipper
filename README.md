# AutoClipper

> **Find the highlights in a long Twitch stream.**
> Paste a Twitch VOD link, get its best moments ranked, preview and trim them, and export vertical, captioned clips ready to post.

![Status: pre-alpha, foundations](https://img.shields.io/badge/status-foundations-lightgrey)
![License: not yet chosen](https://img.shields.io/badge/license-TBD-lightgrey)

> [!IMPORTANT]
> **Project status: foundations. There are no features yet.**
> The repo holds the scope, architecture, conventions and plan, plus the Python package scaffold with its tooling and CI. See [`docs/project/status.md`](docs/project/status.md) for what exists today.

---

## Start here (humans and AI agents)

**Before working on anything, read these in order:**

1. [`AGENTS.md`](AGENTS.md): the rules and step-by-step task process for everyone. AI agents load it automatically; humans should read it once.
2. [`docs/project/status.md`](docs/project/status.md): what exists right now and what's blocked.
3. [`docs/product/prd.md` § Scope at a glance](docs/product/prd.md#scope-at-a-glance): what we're building (must / should / could / won't) and what's still undecided.
4. [`docs/architecture/overview.md`](docs/architecture/overview.md): how the system fits together.
5. [`CONTRIBUTING.md`](CONTRIBUTING.md): how to pick up an issue, open a PR and get it merged.
6. The issue you're working on (planned work is drafted in [`docs/project/backlog.md`](docs/project/backlog.md)), plus the docs [`AGENTS.md` § 3](AGENTS.md#3-what-to-read-for-which-task) lists for that kind of task.

> **AI agents:** if you were pointed at this README, read `AGENTS.md` next and follow it. Don't start changing files until you've read items 1–3 and your issue.

## The problem

Twitch streams run 4–12+ hours, but the moments worth watching or posting add up to a few minutes. Finding them means scrubbing footage by hand.

| Who | Pain today | With AutoClipper |
|---|---|---|
| **Viewer** who missed the stream | Can't sit through a 7-hour VOD | Jump straight to the best moments |
| **Streamer** without an editor | Their best moments never get posted | Paste the VOD, export a few clips in minutes |
| **Clipper / editor** | Hours of scrubbing for 30 seconds of gold | Review a ranked shortlist instead of the whole VOD |

Viewer-made clips help, but they're duplicated, incomplete, missing for smaller streams, and their view counts measure popularity rather than whether a moment works as a highlight.

## What we're building

**The MVP:** paste a VOD link → the pipeline pulls viewer clips, chat and audio → chat and audio signals plus a trained model rank moments across the whole VOD (even when nobody clipped anything) → the web app shows the top moments with a timeline and preview → adjust start/end → export a 9:16 MP4 with captions.

The full scope, including what's explicitly out (live clipping, auto-posting, TikTok view prediction), is in the [PRD scope table](docs/product/prd.md#scope-at-a-glance).

## How it works

```text
Twitch VOD URL
   │
   ├─▶ 1. Collect        VOD info + viewer clips (Twitch API), chat replay, audio
   ├─▶ 2. Signals        chat activity and audio loudness in short windows across the VOD
   ├─▶ 3. Rank           baselines + trained model (logistic regression → LightGBM)
   ├─▶ 4. Review         web app: ranked shortlist, timeline, preview, trim
   └─▶ 5. Export         cut → 9:16 crop → burned-in captions → MP4
```

Viewer clips double as **noisy training labels**: roughly, a stretch of the VOD that viewers clipped counts as a good moment (the exact target is still open, OQ-1). The model is evaluated on streamers it never trained on and compared against simple baselines (random, biggest chat spike, loudest audio). Results get reported honestly, even if a baseline wins. See [evaluation](docs/ml/evaluation.md). No dataset or model exists yet.

## Tech stack

| Layer | Choice | Why | Decision |
|---|---|---|---|
| Pipeline and ML | Python 3.12, uv, pandas/Polars, scikit-learn, LightGBM | All the ML is Python. uv gives one-command, reproducible setup. LightGBM is a strong tabular model with a built-in ranking mode. | [ADR-0002](docs/decisions/0002-language-and-tooling.md) |
| Signals | Twitch Helix API, TwitchDownloader (chat), yt-dlp + librosa (audio) | Chat and audio are cheap per-second signals that run on a laptop. Video is only downloaded for clips we export. | [ADR-0004](docs/decisions/0004-staged-scope-chat-and-audio-first.md) |
| Data storage | Parquet files + DuckDB | Fast analytics on a laptop with no database server to run | [ADR-0005](docs/decisions/0005-local-storage-parquet-duckdb.md) |
| Export | FFmpeg, faster-whisper | Standard tool for cutting, cropping and burning in captions; Whisper captions run on CPU | [Architecture § Export](docs/architecture/overview.md#46-export) |
| Backend | FastAPI + background worker | Same language as the ML. Processing a VOD takes minutes, so jobs run outside the request. | [ADR-0007](docs/decisions/0007-web-application-stack.md) |
| Frontend | Next.js + TypeScript + Tailwind | The team knows it; supports a polished UI with a custom highlight timeline | [ADR-0007](docs/decisions/0007-web-application-stack.md) |
| Quality | Ruff, Pyright, pytest, pre-commit, GitHub Actions | Fast, deterministic feedback for humans and agents. Actions is free for this public repo. | [ADR-0002](docs/decisions/0002-language-and-tooling.md) |
| Hosting | Not decided; runs locally first | A laptop is enough for the demo. Hosted deployment is a stretch goal. | OQ-18 |

## Documentation

| If you want to… | Read |
|---|---|
| Know what we're building and why | [PRD](docs/product/prd.md) |
| See the plan, milestones and dates | [Roadmap](docs/product/roadmap.md) |
| Find something to work on | [Implementation backlog](docs/project/backlog.md) (planned issues in dependency order, with their GitHub issue numbers) |
| Understand the system design | [Architecture](docs/architecture/overview.md) · [Data model](docs/architecture/data-model.md) |
| Work with the data | [`docs/data/`](docs/data/README.md) |
| Work on the models | [`docs/ml/`](docs/ml/README.md) |
| Set up a dev environment | [Setup](docs/development/setup.md) |
| Contribute a change | [CONTRIBUTING](CONTRIBUTING.md) |
| Work with AI coding agents | [AGENTS.md](AGENTS.md) · [`docs/agents/`](docs/agents/README.md) |
| Understand why a choice was made | [Decisions (ADRs)](docs/decisions/README.md) |
| See every doc | [`docs/README.md`](docs/README.md) |

## Contributing

Pick an issue, branch, open a PR that says `Closes #N`, get one teammate's approval, and squash-merge. [`CONTRIBUTING.md`](CONTRIBUTING.md) walks through every step, including PR titles, labels and screenshots for UI work.

## Responsible use

AutoClipper works with other people's content. Follow [data governance](docs/data/data-governance.md): confirm source permissions before collecting real data, minimize chat data, keep media, logs and secrets out of Git, and never auto-post anything.

## Background

AutoClipper is built by a four-person team (4 FPS) in the [CUNY Tech Prep](https://cunytechprep.org/) Data Science track, Fall 2026. The project is not affiliated with or endorsed by Twitch, TikTok or any streamer.

## License

**Not yet chosen.** Until a license is added, all rights are reserved by default. See [ADR-0006](docs/decisions/0006-license.md) for the proposal.
