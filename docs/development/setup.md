# Development Setup

| | |
|---|---|
| **Status** | Current Python and frontend setup. Tested on Windows 11 in PowerShell and Git Bash; CI runs Python on Ubuntu/Windows and frontend checks on Ubuntu. macOS is not yet tested. Background: [tooling](../decisions/0002-language-and-tooling.md). |
| **Last updated** | 2026-10-31 |

## Prerequisites

| Tool | Version | Why | Install |
|---|---|---|---|
| Git | recent | Version control | <https://git-scm.com/> |
| Python | 3.12 (pinned in `.python-version`) | Pipeline and ML | Nothing to install: `uv sync` uses an installed 3.12 or downloads one |
| uv | latest (the lockfile was made with 0.13) | Python package/env manager | Windows: `winget install --id astral-sh.uv -e` · macOS: `brew install uv` · [all options](https://docs.astral.sh/uv/getting-started/installation/) |
| FFmpeg | 6+ | Audio/video processing | Windows: `winget install Gyan.FFmpeg` · macOS: `brew install ffmpeg` · Linux: package manager |
| yt-dlp | Deferred to #19 | VOD audio download (unmerged draft) | Not installed by the current `uv sync`; follow #19 when it is ready to merge |
| TwitchDownloaderCLI | 1.56.5 | Chat replay | Download the pinned release from <https://github.com/lay295/TwitchDownloader/releases/tag/1.56.5> and put it on `PATH` |
| Node.js | 24 LTS (pinned in `web/.nvmrc`) | Web app in `web/` | <https://nodejs.org/> or a version manager (fnm, nvm, nvm-windows, Volta). npm comes with it. See [Frontend](#frontend-web). |
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

`uv run pytest` should report only passed tests (tests that need FFmpeg are skipped until it is installed; see [testing](testing.md#ffmpeg-tests)), and `autoclipper --help` prints the CLI usage. The everyday lint, format, type-check and test commands are in [`AGENTS.md` § 5](../../AGENTS.md#5-commands).

### Git hooks

After `pre-commit install`, the hooks run on the files you stage each time you `git commit`. The first commit takes a minute because pre-commit downloads the hook environments (needs network). Run all hooks on every file with `uv run pre-commit run --all-files`.

When a hook fixes files (Ruff, end-of-file or whitespace fixes, notebook output stripping), the commit stops. Review the changes, `git add` them and commit again. The large-file (500 KB) and private-key checks can't fix anything: unstage the file instead.

## Frontend (web/)

The Next.js app needs only Node.js. It doesn't call the Python backend yet. These steps were tested on Windows 11 (Node 24.19.0) in PowerShell and Git Bash. CI runs the same checks on Ubuntu; nobody has tried them on macOS or Linux by hand yet.

**Node version.** `web/.nvmrc` pins Node **24** ("Krypton"). It was the Active LTS line when pinned on 2026-10-19, moves to Maintenance LTS on 2026-10-20, and is supported until 2028-04-30 ([Node.js release schedule](https://github.com/nodejs/Release#release-schedule)). Node 26 becomes Active LTS on 2026-10-28; moving to it means changing `.nvmrc` and `engines` in `web/package.json` together. Check with `node --version` (should print `v24.x`).

Install and run the dev server (<http://localhost:3000>):

```bash
cd web
npm ci
npm run dev
```

```powershell
Set-Location web
npm ci
npm run dev
```

Run the same checks as CI before you push:

```bash
npm run lint && npm run typecheck && npm run format:check && npm run build
```

```powershell
# Windows PowerShell 5.1 has no &&, so this runs every check and you read each result.
npm run lint; npm run typecheck; npm run format:check; npm run build
```

`npm run format` fixes formatting. All scripts are listed in [`web/README.md`](../../web/README.md).

**Editor:** the ESLint, Prettier and Tailwind CSS IntelliSense extensions for VS Code, with Prettier as the formatter for files in `web/`.
## Configuration

`autoclipper` reads its settings once per run, from environment variables and from the `.env` file in the directory you run it in. Run it from the repository root, where you copied `.env.example`; parent directories are not searched. A variable set in the environment wins over the same variable in `.env`. Every variable is described in `.env.example`, and the code is `src/autoclipper/config.py`.

An untracked `.env` is convenient for local development. A shared worker or hosted deployment can supply the same variable names through its process environment or deployment secret configuration; no `.env` file is needed when those values are supplied. Keep Twitch credentials on the backend, rather than in browser-delivered frontend variables.

- An empty value (`TWITCH_CLIENT_ID=`) means unset. The Twitch credentials and the chat hash salt are only needed by the commands that use them; those stop with an error naming the missing variable.
- Relative paths in `AUTOCLIPPER_DATA_DIR` and `AUTOCLIPPER_ARTIFACTS_DIR`, and their defaults `./data` and `./artifacts`, are relative to the directory you run from. Use an absolute path to share one data folder between worktrees. `~` means your home directory.
- **Windows paths:** write them without quotes, even with spaces (`AUTOCLIPPER_DATA_DIR=D:\AutoClipper Data`), or with forward slashes. A network share works either way: `\\server\share\data` unquoted, or `//server/share/data`. Inside quotes python-dotenv decodes backslash escapes: in double quotes `"C:\new"` would contain a newline, and in single or double quotes `\\server` becomes `\server`, a folder on the current drive. AutoClipper rejects both instead of using them, and on Windows rejects any path that starts with a slash or backslash but has no drive letter.
- `AUTOCLIPPER_EXPORT_REGION` picks the part of the source frame vertical clips show, as `x,y,width,height` fractions of the frame (for example `0.25,0,0.5,1`). Empty means a center crop.
- **Malformed lines:** a line python-dotenv can't parse (for example an unclosed quote), or a `.env` that isn't UTF-8, stops every command with exit code 1 and the line number. AutoClipper never skips the line and falls back to a default.
- `.env` may only contain AutoClipper's variables, so a misspelled name is caught. An invalid value or an unknown name stops every command with `Error: invalid settings ...` and exit code 1. The message names the variable but never shows its value.

## Twitch credentials

1. Log in at <https://dev.twitch.tv/console> (two-factor auth must be enabled on the account).
2. **Register Your Application.** Name: anything unique. OAuth redirect URL: `http://localhost`. Category: *Analytics Tool*. Client type: *Confidential*.
3. Copy the **Client ID** and generate a **Client Secret**. Supply `TWITCH_CLIENT_ID` and `TWITCH_CLIENT_SECRET` through `.env` or the environment.
4. The team shares one registered app (already done, #4). Reuse its credentials on development machines and collection workers, sharing them privately and keeping them out of git and logs. Hosted instances can use deployment-managed environment variables.
5. **If the secret is ever committed or pasted somewhere public,** generate a new Client Secret in the console right away and update everyone's `.env`. Deleting it from git history doesn't undo the leak.

## Chat hash salt

The chat parser replaces every chatter's username and user ID with a salted hash, and the pseudonymized messages are kept in `data/interim/chat/` ([data handling](../data/data-governance.md#2-chat)). Hashes only need to be consistent within one VOD (for unique-chatter counts), so each machine can use its own salt. Use the same salt for all of your own ingests, and never commit it.

To make a salt, generate a long random value and set `AUTOCLIPPER_CHAT_HASH_SALT` in `.env` or the environment:

```bash
uv run python -c "import secrets; print(secrets.token_hex(32))"
```

Code that pseudonymizes gets the salt with `get_settings().require_chat_hash_salt()` and hashes with `autoclipper.privacy.pseudonymize()`; without a salt it stops with an error naming the variable.

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
- Task workflow: [AGENTS.md](../../AGENTS.md); publication and review: [CONTRIBUTING](../../CONTRIBUTING.md).

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `Error: invalid settings in the environment or …` | The lines below it name each bad variable. Fix the value, or remove or rename a line that isn't in `.env.example` ([Configuration](#configuration)). |
| `… .env has lines that can't be parsed (line N)` or `… is not valid UTF-8 text` | Fix that line of `.env` (usually an unclosed quote), or save the file as UTF-8. |
| `TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET are not set, but calling the Twitch API needs them` | Fill both in `.env` ([Twitch credentials](#twitch-credentials)), and run the command from the directory that holds `.env`. |
| `401 Unauthorized` from Helix | Wrong client ID/secret, or an expired token. The client should refresh automatically. Check `.env`. |
| `429 Too Many Requests` | Rate limit hit. The client should back off. If you see it often, lower concurrency. |
| yt-dlp fails on Twitch | Update it: `uv lock --upgrade-package yt-dlp && uv sync` |
| TwitchDownloader output schema changed | Pin the previous release and open a `bug` issue |
| `ffmpeg: command not found`, or AutoClipper reports `'ffmpeg' was not found on PATH` | Not on `PATH`. Restart the terminal after installing. |
| `uv: command not found` / `'uv' is not recognized` | Not on `PATH` yet. Open a new terminal after installing uv (see the Windows note above). |
| ``The lockfile at `uv.lock` needs to be updated, but `--locked` was provided`` (CI or locally) | `pyproject.toml` changed without a new lock. Run `uv lock` and commit `uv.lock` with `pyproject.toml`. |
| uv warns `Failed to hardlink files; falling back to full copy` | Harmless. The clone and uv's cache are on different drives. Set the environment variable `UV_LINK_MODE=copy` to silence it. |
| `uv run pytest -m live` reports `no tests ran` (exit code 5) | No live tests exist yet; there's nothing to select. |
| `npm warn EBADENGINE` in `web/` | You're not on Node 24. Switch versions (see [Frontend](#frontend-web)). |
| npm warns that `unrs-resolver` install scripts were blocked (seen with npm 11.19 and 12.2) | Harmless. Its native binary comes from an optional dependency; the blocked script is only a fallback. Don't approve it unless something breaks. |
| `npm audit` reports high-severity `braces` advisories in `web/` | Comes from the lint toolchain (`eslint-config-next` → `fast-glob`), not from app code, and has no fixed version yet. Don't run `npm audit fix --force`: it downgrades `eslint-config-next` to v14. |
