# AutoClipper

> **Find the 30 seconds worth posting in an 8-hour stream.**
> AutoClipper takes a Twitch VOD, finds and ranks the moments most worth turning into short-form clips, and exports them ready to post.

![Status: pre-alpha, design phase](https://img.shields.io/badge/status-design%20phase-lightgrey)
![License: not yet chosen](https://img.shields.io/badge/license-TBD-lightgrey)

> [!IMPORTANT]
> **Project status: design phase. There is no application code yet.**
> This repository holds the project's documentation, conventions and planning. Everything under "How it will work" describes the *planned* system. See [`docs/project/status.md`](docs/project/status.md) for what exists today.

---

## The problem

Clipping streams is a real side hustle. Many streamers allow fans to repost highlights, and some pay clippers for them. The hard part is finding the clips:

- A clipper either scrubs through hours of footage by hand, **or**
- picks from the hundreds of clips viewers made during the stream, without knowing which ones will perform as a standalone short.

AutoClipper is a tool **for clippers, not viewers**. It does the finding and ranking so the clipper can focus on posting.

## How it will work

```text
Twitch VOD URL
   │
   ├─▶ 1. Collect candidates   viewer-made clips (Twitch API) + moments the model flags
   ├─▶ 2. Extract signals      chat activity, audio energy, (later) video features
   ├─▶ 3. Score and rank       trained models predict which moments are worth posting
   └─▶ 4. Export               vertical 9:16, captioned clips, ready to upload
```

The data science rests on one fact: **Twitch's API reports every clip viewers created and where it starts in the VOD.** That gives us thousands of free, human-made labels for "this moment was worth clipping". We train models on chat, audio and (eventually) video signals to predict those moments in streams where no labels exist yet, and to rank candidates by how well they're likely to perform.

The project is staged to keep risk low (see [ADR-0004](docs/decisions/0004-staged-scope-chat-and-audio-first.md)):

| Stage | What it does | Signals |
|---|---|---|
| **v1: Clip ranker** | Rank the clips viewers already made for a VOD | Chat + audio |
| **v2: Moment detector** | Find clip-worthy moments in a full VOD, including ones nobody clipped | Chat + audio, 10-second windows |
| **Stretch** | Video/CV features, live mode, short-form performance model, posting experiment | + video, + external engagement data |

Every model is measured against simple baselines, such as sorting by Twitch view count or flagging chat spikes. The project only counts as a success if it beats them. See [`docs/ml/evaluation.md`](docs/ml/evaluation.md).

## Documentation

| Start here if you want to… | Read |
|---|---|
| Understand the product and why it exists | [`docs/product/prd.md`](docs/product/prd.md) |
| See the plan and milestones | [`docs/product/roadmap.md`](docs/product/roadmap.md) |
| Understand the system design | [`docs/architecture/overview.md`](docs/architecture/overview.md) |
| Work with the data | [`docs/data/`](docs/data/README.md) |
| Work on the models | [`docs/ml/`](docs/ml/README.md) |
| Set up a dev environment | [`docs/development/setup.md`](docs/development/setup.md) |
| Contribute a change | [`CONTRIBUTING.md`](CONTRIBUTING.md) |
| Work as (or direct) an AI coding agent | [`AGENTS.md`](AGENTS.md) and [`docs/agents/`](docs/agents/README.md) |
| See every doc | [`docs/README.md`](docs/README.md) |

## Proposed tech stack

> [!NOTE]
> **Proposal, not yet ratified.** See [ADR-0002](docs/decisions/0002-language-and-tooling.md) and [ADR-0007](docs/decisions/0007-web-application-stack.md).

- **Pipeline and ML:** Python 3.12, `uv`, pandas/Polars, DuckDB + Parquet, scikit-learn, LightGBM, PyTorch
- **Media:** FFmpeg, yt-dlp, librosa, PySceneDetect, faster-whisper (captions)
- **App:** FastAPI backend, Next.js + TypeScript frontend
- **Quality:** Ruff, Pyright, pytest, pre-commit, GitHub Actions

## Contributing

All changes go through pull requests against `main`. Each PR needs one approving review and is squash-merged. Read [`CONTRIBUTING.md`](CONTRIBUTING.md) before opening one. Most work on this project is done with AI coding agents, so [`AGENTS.md`](AGENTS.md) is the canonical guide to conventions for agents *and* humans.

## Responsible use

AutoClipper works with other people's content. Contributors must follow [`docs/data/data-governance.md`](docs/data/data-governance.md). In short: respect Twitch's terms, never commit media or raw chat logs, pseudonymize chat usernames, and only publish clips of streamers who allow it.

## Background

AutoClipper is being built by a four-person team in the [CUNY Tech Prep](https://cunytechprep.org/) Data Science track. The project is not affiliated with or endorsed by Twitch, TikTok or any streamer.

## License

**Not yet chosen.** Until a license is added, all rights are reserved by default. See [ADR-0006](docs/decisions/0006-license.md) for the proposal.
