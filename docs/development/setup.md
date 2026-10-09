# Development Setup

| | |
|---|---|
| **Status** | Partly proposal. There's no code to run yet. Prerequisites follow [ADR-0002](../decisions/0002-language-and-tooling.md) (*Proposed*). **The PR that scaffolds the project must turn this into exact, tested steps.** |
| **Last updated** | 2026-10-09 |

## Prerequisites

| Tool | Version | Why | Install |
|---|---|---|---|
| Git | recent | Version control | <https://git-scm.com/> |
| Python | 3.12 | Pipeline and ML | Installed and managed by `uv` (below) |
| uv | latest | Python package/env manager | <https://docs.astral.sh/uv/getting-started/installation/> |
| FFmpeg | 6+ | Audio/video processing | Windows: `winget install Gyan.FFmpeg` · macOS: `brew install ffmpeg` · Linux: package manager |
| yt-dlp | latest | VOD audio/segment download | Installed as a Python dependency via `uv`, so no global install is needed |
| TwitchDownloaderCLI | latest release | Chat replay | Download a release binary from <https://github.com/lay295/TwitchDownloader/releases> and put it on `PATH` |
| Node.js | 20 LTS+ | Web UI (later) | <https://nodejs.org/> (only needed for `web/`) |
| GitHub CLI | optional | Issues/PRs from the terminal | <https://cli.github.com/> |

Check them:

```bash
git --version && uv --version && ffmpeg -version | head -1 && TwitchDownloaderCLI --version
```

## First-time setup (planned)

```bash
git clone https://github.com/adillpickles/AutoClipper.git
cd AutoClipper

# Python env + deps (creates .venv, installs Python 3.12 if needed)
uv sync

# Local config
cp .env.example .env        # then fill in values (see below)

# Git hooks
uv run pre-commit install

# Smoke test
uv run pytest
uv run autoclipper --help
```

## Twitch credentials

1. Log in at <https://dev.twitch.tv/console> (two-factor auth must be enabled on the account).
2. **Register Your Application.** Name: anything unique. OAuth redirect URL: `http://localhost`. Category: *Analytics Tool*. Client type: *Confidential*.
3. Copy the **Client ID** and generate a **Client Secret** into `.env`.
4. **Proposal:** the team shares one app's credentials privately, never in chat history that might go public and never in git, or each member registers their own app.

## Chat hash salt

Every team member must use **the same** `AUTOCLIPPER_CHAT_HASH_SALT`, or pseudonymized IDs won't match across machines. Get it from the team's private channel. Never commit it.

## Editor setup

- **VS Code:** recommended extensions are Python, Pylance, Ruff, EditorConfig, Even Better TOML and markdownlint. A `.vscode/extensions.json` will be added with the scaffold.
- Use the project's `.venv` interpreter (`uv sync` creates it).
- Format on save with Ruff.

## Windows notes

- Use **Git Bash** or **PowerShell**. Commands in the docs are written for a POSIX shell. PowerShell equivalents are usually obvious (`cp` → `Copy-Item`).
- `.gitattributes` enforces LF endings. If git warns about CRLF, run `git config core.autocrlf false` in this repo and re-checkout.
- Long paths: `git config --global core.longpaths true` if you hit path-length errors.
- Make sure `ffmpeg` and `TwitchDownloaderCLI` are on `PATH` in the shell your editor uses.

## AI agent setup

- **Claude Code:** reads `CLAUDE.md` (which imports `AGENTS.md`) automatically. Run it from the repo root.
- **Codex and others:** read `AGENTS.md`.
- See [docs/agents/](../agents/README.md) for the workflow.

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `401 Unauthorized` from Helix | Wrong client ID/secret, or an expired token. The client should refresh automatically. Check `.env`. |
| `429 Too Many Requests` | Rate limit hit. The client should back off. If you see it often, lower concurrency. |
| yt-dlp fails on Twitch | Update it: `uv lock --upgrade-package yt-dlp && uv sync` |
| TwitchDownloader output schema changed | Pin the previous release and open a `bug` issue |
| `ffmpeg: command not found` | Not on `PATH`. Restart the terminal after installing. |
