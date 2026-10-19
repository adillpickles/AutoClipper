# Development Setup

| | |
|---|---|
| **Status** | Binding for the Python setup. Tested from a fresh clone on Windows 11 in PowerShell and Git Bash; CI runs the same commands on Ubuntu and Windows. Not yet tried on a macOS or Linux machine. Prerequisites follow [ADR-0002](../decisions/0002-language-and-tooling.md) (*Accepted*). Frontend steps arrive with the Next.js scaffold (#6). |
| **Last updated** | 2026-10-19 |

## Prerequisites

| Tool | Version | Why | Install |
|---|---|---|---|
| Git | recent | Version control | <https://git-scm.com/> |
| Python | 3.12 (pinned in `.python-version`) | Pipeline and ML | Nothing to install: `uv sync` uses an installed 3.12 or downloads one |
| uv | latest (the lockfile was made with 0.13) | Python package/env manager | Windows: `winget install --id astral-sh.uv -e` · macOS: `brew install uv` · [all options](https://docs.astral.sh/uv/getting-started/installation/) |
| FFmpeg | 6+ | Audio/video processing | Windows: `winget install Gyan.FFmpeg` · macOS: `brew install ffmpeg` · Linux: package manager |
| yt-dlp | latest | VOD audio/segment download | Installed as a Python dependency via `uv`, so no global install is needed |
| TwitchDownloaderCLI | latest release | Chat replay | Download a release binary from <https://github.com/lay295/TwitchDownloader/releases> and put it on `PATH` |
| Node.js | Supported Active/Maintenance LTS; pin at frontend scaffold time | Proposed web UI | [Release status](https://nodejs.org/en/about/previous-releases) (Node 20 is EOL as checked 2026-10-13) |
| GitHub CLI | optional | Issues/PRs from the terminal | <https://cli.github.com/> |

Check each required tool individually in the shell used by your editor:

```bash
git --version
uv --version
ffmpeg -version
TwitchDownloaderCLI --version
```

> **Windows:** winget adds tools to `PATH` for terminals opened *after* the install. If `uv` or `ffmpeg` is "not recognized" right after installing, open a new terminal (and restart VS Code so its terminals pick up the new `PATH`).

## First-time setup

Only Git and uv are needed for these steps. FFmpeg and TwitchDownloaderCLI are needed once you run media or chat tasks.

**macOS, Linux, Git Bash:**

```bash
git clone https://github.com/adillpickles/AutoClipper.git
cd AutoClipper

# Create .venv with Python 3.12 and install the locked dependencies, dev tools included
uv sync

# Local config: fill in values privately (see below); never commit .env
cp .env.example .env

# Git hooks: Ruff, notebook output stripping, large-file and private-key checks
uv run pre-commit install

# Smoke test
uv run pytest
uv run autoclipper --help
```

**PowerShell:**

```powershell
git clone https://github.com/adillpickles/AutoClipper.git
cd AutoClipper
uv sync
Copy-Item .env.example .env
uv run pre-commit install
uv run pytest
uv run autoclipper --help
```

`uv run pytest` should report only passed tests, and `autoclipper --help` prints the CLI usage. The everyday lint, format, type-check and test commands are in [`AGENTS.md` § 5](../../AGENTS.md#5-commands).

### Git hooks

After `pre-commit install`, the hooks run on the files you stage each time you `git commit`. The first commit takes a minute because pre-commit downloads the hook environments (needs network). Run all hooks on every file with `uv run pre-commit run --all-files`.

When a hook fixes files (Ruff, end-of-file or whitespace fixes, notebook output stripping), the commit stops. Review the changes, `git add` them and commit again. The large-file (500 KB) and private-key checks can't fix anything: unstage the file instead.

## Twitch credentials

1. Log in at <https://dev.twitch.tv/console> (two-factor auth must be enabled on the account).
2. **Register Your Application.** Name: anything unique. OAuth redirect URL: `http://localhost`. Category: *Analytics Tool*. Client type: *Confidential*.
3. Copy the **Client ID** and generate a **Client Secret** into `.env`.
4. **Proposal:** the team shares one app's credentials privately, never in chat history that might go public and never in git, or each member registers their own app.

## Chat hash salt

If OQ-9 approves persisted hash-based joins across machines, use the same privately shared `AUTOCLIPPER_CHAT_HASH_SALT` for that dataset. Aggregate-only retention may need hashes only transiently for unique-chatter counts. Decide linkage scope before real ingestion; never commit the salt.

## Editor setup

- **VS Code:** accept the recommended extensions when you open the folder (listed in `.vscode/extensions.json`): Python, Pylance, Ruff, EditorConfig, Even Better TOML and markdownlint. Pylance reads the Pyright settings in `pyproject.toml`.
- Use the project's `.venv` interpreter (`uv sync` creates it): *Python: Select Interpreter* → `.venv`.
- Format on save with Ruff.

## Windows notes

- Use **Git Bash** or **PowerShell**. The setup steps above are given and tested for both.
- `.gitattributes` enforces LF except Windows `.bat`, `.cmd` and `.ps1` scripts (CRLF). Match `.editorconfig`; do not reset/re-checkout unrelated work to fix a warning.
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
| `uv: command not found` / `'uv' is not recognized` | Not on `PATH` yet. Open a new terminal after installing uv (see the Windows note above). |
| `uv sync --locked` fails because the lockfile needs to be updated (CI or locally) | `pyproject.toml` changed without a new lock. Run `uv lock` and commit `uv.lock` with `pyproject.toml`. |
| uv warns `Failed to hardlink files; falling back to full copy` | Harmless. The clone and uv's cache are on different drives. Set the environment variable `UV_LINK_MODE=copy` to silence it. |
| `uv run pytest -m live` reports `no tests ran` (exit code 5) | No live tests exist yet; there's nothing to select. |
