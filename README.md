# AutoClipper

> **Find the highlights in a long Twitch stream.**
> AutoClipper helps viewers, streamers, clippers and editors discover, review and export interesting moments from a Twitch VOD.

![Status: pre-alpha, design phase](https://img.shields.io/badge/status-design%20phase-lightgrey)
![License: not yet chosen](https://img.shields.io/badge/license-TBD-lightgrey)

> [!IMPORTANT]
> **Project status: design phase. There is no application code yet.**
> This repository holds the project's documentation, conventions and planning. Everything under "How it will work" describes the *planned* system. See [`docs/project/status.md`](docs/project/status.md) for what exists today.

---

## The problem

Long streams take time to review. A viewer may want to catch up, a streamer may want highlights from their broadcast, and an editor may want clips suitable for short-form video. Finding those moments is the shared problem:

- Scrubbing through hours of footage is slow.
- Viewer-made clips are useful starting points, but can be repetitive, incomplete or unavailable. View counts measure popularity, not whether a moment makes a good highlight.

AutoClipper brings these audiences into one workflow: browse suggested moments, preview them, adjust the trim, and export useful clips.

## How it will work

```text
Twitch VOD URL
   │
   ├─▶ 1. Gather information   available viewer clips + chat/audio signals
   ├─▶ 2. Discover and rank    API candidates, heuristics and useful trained ML
   ├─▶ 3. Review in the app    understandable highlights, previews and trimming
   └─▶ 4. Export               basic framing and captions for selected clips
```

Twitch's API supplies **viewer clips, available VOD offsets and views**. These can provide candidates, ranking signals and noisy training labels. Query limits and availability mean coverage must be checked ([data sources](docs/data/data-sources.md)). Chat and audio are proposed initial signals; speech or video can be added when experiments justify them. The analysis should remain useful when clips are scarce or absent. No dataset or model exists yet.

The proposed staging keeps an existing-clip prototype available without making it the finished semester deliverable ([ADR-0004](docs/decisions/0004-staged-scope-chat-and-audio-first.md)):

| Stage | What it does | Signals |
|---|---|---|
| **Early prototype** | Review and rank available viewer clips; exercise preview/export | API information; proposed chat + audio |
| **Semester product** | Discover across a supported VOD, including when clips are absent; review and export through an appealing app | Heuristics and trained ML, architecture to be agreed |
| **Optional later work** | Advanced video understanding, live mode, longer compilations, publishing integrations | Evidence and separate scope decisions |

Compare learned analysis with sensible baselines on a reproducible, agreed protocol. Improving on them is the research goal; unfavorable results must be reported honestly and need not halt app development. Predicting TikTok views and training two separate models are not requirements. See [`docs/ml/evaluation.md`](docs/ml/evaluation.md).

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

AutoClipper works with other people's content. Follow [`docs/data/data-governance.md`](docs/data/data-governance.md): record the applicable source permissions before real collection/media use, minimize chat data, keep media/logs/secrets out of Git, and publish only with the required permissions.

## Background

AutoClipper is being built by a four-person team in the [CUNY Tech Prep](https://cunytechprep.org/) Data Science track. The project is not affiliated with or endorsed by Twitch, TikTok or any streamer.

## License

**Not yet chosen.** Until a license is added, all rights are reserved by default. See [ADR-0006](docs/decisions/0006-license.md) for the proposal.
