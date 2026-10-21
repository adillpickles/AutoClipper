# Coding Conventions

| | |
|---|---|
| **Status** | Binding ([ADR-0002](../decisions/0002-language-and-tooling.md) and [ADR-0003](../decisions/0003-repository-layout.md) are accepted). Tool config in `pyproject.toml` is authoritative for anything automated. |
| **Last updated** | 2026-10-21 |

These conventions exist so that code written by different people and different agents looks like it was written by one careful person. **When in doubt, match the surrounding code.** If the surrounding code contradicts this doc, flag it.

## 1. Repository layout (planned)

```text
src/autoclipper/
├── __init__.py
├── cli.py                # Typer app; thin, no business logic
├── config.py             # Settings loaded from env (.env); single source of config
├── logging.py            # logging setup
├── ingest/               # twitch_api.py, chat.py, media.py, catalog.py
├── labels/               # moments.py, windows.py, splits.py
├── features/             # chat.py, audio.py, video.py, registry.py
├── models/               # base.py (interface), baselines.py, gbm.py, nn.py
├── eval/                 # metrics.py, harness.py, bootstrap.py
├── export/               # cut.py, reframe.py, captions.py, encode.py
└── api/                  # proposed FastAPI app, alongside pipeline development
tests/                    # mirrors src/autoclipper/ structure
experiments/              # configs + results write-ups (see docs/ml/experiments.md)
configs/                  # channels.yaml, lexicons, default params
notebooks/                # exploration only; see §8
scripts/                  # one-off/maintenance scripts (not imported by the package)
web/                      # proposed web frontend, early app prototype if chosen
```

Create directories only when a task needs them.

## 2. Python style

- **Python 3.12.** Use modern syntax: `list[str]`, `X | None`, `match` where it clarifies.
- **Formatting and linting:** Ruff (format + lint), line length **100**. Don't hand-format. Don't fight the formatter. `ruff format` also formats ` ```python ` code blocks in Markdown files, so a docs-only change can fail the format check; run `uv run ruff format .` before committing docs with Python examples. The rule selection in `pyproject.toml` enforces the mechanical parts of this doc (type hints, docstrings, absolute imports, pathlib, aware datetimes, no `print`, logging style); the comment next to each rule set names the section it enforces, where there is one.
- **Types:** type hints on every function signature and public attribute. Pyright in `standard` mode. `Any` needs a comment explaining why.
- **Naming:** `snake_case` for modules, functions and variables. `PascalCase` for classes. `UPPER_SNAKE` for constants. Names say what, not how (`merge_clips_into_moments`, not `process`).
- **Imports:** absolute (`from autoclipper.features import chat`). No wildcard imports. Ruff sorts them.
- **Docstrings:** Google style on public functions. Say *what* and *why*. Include units (`seconds`, `Hz`) and the meaning of edge cases.
- **Comments:** explain intent and non-obvious decisions, not mechanics. Link the doc when implementing a documented rule: `# See docs/data/labeling.md §2`.
- **Functions:** small and pure where possible. Feature and label functions take DataFrames and return DataFrames. No hidden I/O.
- **Data classes:** `dataclasses` or Pydantic models for structured records. Pydantic at I/O boundaries (API responses, config). Plain dataclasses internally.

## 3. Units, time and IDs

These rules prevent the most common silent bugs in this project:

| Rule | Example |
|---|---|
| VOD positions are `float` **seconds from VOD start**, with names ending `_s` | `vod_offset_s`, `start_s`, `duration_s` |
| Wall-clock times are timezone-aware **UTC** `datetime`s, with names ending `_at` | `created_at`, `fetched_at` |
| Never store or compare naive datetimes | `datetime.now(tz=UTC)` |
| Twitch IDs are `str`. Never cast them to `int`. | `video_id: str` |
| Sample rates and other units appear in names or docstrings | `sample_rate_hz` |
| Pseudonymized identifiers end with `_hash` and come from `autoclipper.privacy.pseudonymize` | `user_hash` |

## 4. Configuration

- All settings come from `autoclipper.config` (Pydantic Settings reading env vars / `.env`). **Never read `os.environ` elsewhere.** `tests/test_config.py` fails if another module does.
- Entry points (CLI commands, the API app) call `get_settings()`, which returns one cached, immutable `Settings`. Library functions take what they need (`settings.data_dir`, credentials, the salt) as parameters instead of calling `get_settings()` themselves, so tests can pass values directly.
- Environment variables win over `.env`, which is read from the current working directory only (no parent search). Empty values count as unset. `data_dir` and `artifacts_dir` are absolute `Path`s: relative values and the defaults (`data`, `artifacts`) resolve against the working directory when settings load. [Setup § Configuration](setup.md#configuration) has the user-facing rules.
- Secrets (`TWITCH_CLIENT_ID`, `TWITCH_CLIENT_SECRET`, `AUTOCLIPPER_CHAT_HASH_SALT`) are `SecretStr`, which prints as `**********`. Call `.get_secret_value()` only where the value is sent or used.
- Get optional settings an operation needs through `settings.require_twitch_credentials()` or `settings.require_chat_hash_salt()`. They raise `MissingSettingError` naming the missing variables. Invalid values, or a `.env` that can't be read or parsed, make `get_settings()` raise `ConfigError` (the base class), and the CLI reports those with exit code 1 before any command runs. A command that calls a `require_*` method maps `MissingSettingError` to `ExitCode.USER_ERROR`. Error messages name variables but never show their values.
- A new setting is a `Settings` field with `validation_alias` set to its variable name, plus a line in `.env.example`, in the same PR. `tests/test_config.py` checks that the two match.
- No hard-coded paths, credentials or channel names in library code. Channels live in `configs/channels.yaml`.
- Algorithm parameters (window size, gap thresholds) are named constants or config values, documented where they're defined, and match [labeling](../data/labeling.md).

## 5. Data handling

- Tabular data is read and written as **Parquet**. Query it with DuckDB or Polars/pandas (pick one per module and stay consistent).
- Column names and types follow [data model](../architecture/data-model.md) exactly.
- **Never silently drop rows.** If you filter, log how many rows were dropped and why.
- Validate inputs at stage boundaries: required columns present, dtypes correct, IDs unique where expected. Fail with a clear message.
- Paths come from `pathlib.Path` and the config's data/artifact roots. They must work on Windows.

## 6. Errors and logging

- Raise specific exceptions (`TwitchAPIError`, `ChatDownloadError`), defined in the module that raises them or in `autoclipper.errors`.
- Don't catch broad `Exception` except at the top level (CLI/API) or to add context and re-raise.
- Retry **only** network operations, with exponential backoff and a cap.
- Use `logging.getLogger(__name__)` with key=value messages: `log.info("fetched clips video_id=%s n=%d", vid, n)`.
- Only entry points configure logging, by calling `autoclipper.logging.configure_logging()` once. The CLI's root callback does it for every subcommand. Library modules never call `basicConfig` or add handlers (`tests/test_logging.py` checks it).
- Each record is one line on stderr: `ts=2026-10-19T16:29:01.123Z level=INFO logger=autoclipper.ingest fetched clips video_id=123 n=5` (UTC, milliseconds). `AUTOCLIPPER_LOG_LEVEL` sets the level of the `autoclipper` loggers. Other libraries keep Python's default of WARNING.
- **Never log** secrets, tokens or raw usernames.

## 7. CLI

- Typer app in `cli.py`. Each subcommand is a thin call into a library function.
- Subcommands are verbs: `ingest`, `collect`, `build-dataset`, `features`, `train`, `eval`, `analyze`, `export`.
- Long-running commands show progress (`rich` progress bars) and are resumable/idempotent. `--force` re-runs.
- Exit codes: 0 success, 1 user/input error, 2 external failure (network/tool). Use `autoclipper.cli.ExitCode`. Command-line usage errors (unknown option or command, bad value) already exit with 1.

## 8. Notebooks

- Notebooks are for **exploration and visualization**, not for producing reported results or holding logic that matters.
- Name them `notebooks/YYYY-MM-DD-<author-initials>-<topic>.ipynb`.
- **Clear outputs before committing** (outputs can contain chat text or large images). The `nbstripout` pre-commit hook strips them.
- When logic in a notebook proves useful, move it into `src/autoclipper/` with tests.

## 9. Dependencies

- Add with `uv add <pkg>` (or `uv add --dev <pkg>` for dev tools), which updates `pyproject.toml` and `uv.lock`. Commit both.
- Justify every new dependency in the PR. Prefer what's already in [ADR-0002](../decisions/0002-language-and-tooling.md).
- Heavy optional stacks (PyTorch, video/CV models) go in optional dependency groups so the core pipeline stays light.

## 10. Frontend

The Next.js app in `web/` ([ADR-0007](../decisions/0007-web-application-stack.md)). Tool config in `web/` (`package.json`, `tsconfig.json`, `eslint.config.mjs`, `.prettierrc.json`) is authoritative for anything automated. Commands are in [`web/README.md`](../../web/README.md).

- **Stack:** Next.js App Router, React, TypeScript in `strict` mode, Tailwind CSS v4. Node version from `web/.nvmrc`. npm with a committed `package-lock.json`; install with `npm ci`.
- **Formatting and linting:** Prettier with default settings, plus `prettier-plugin-tailwindcss` to sort class names. ESLint uses `eslint-config-next` (core-web-vitals + TypeScript, including the `jsx-a11y` rules), with `eslint-config-prettier` last so the two never disagree. Don't hand-format. A lint warning fails CI.
- **Folder layout:**

  ```text
  web/src/
  ├── app/            # routes only: page.tsx, layout.tsx, loading/error files, globals.css
  ├── components/     # reusable UI, e.g. highlight-timeline.tsx
  └── lib/
      └── api/        # the one typed API client
  ```

  Only `app/` exists so far. Create the others when the first file needs them.
- **File names:** kebab-case (`highlight-timeline.tsx`) exporting a `PascalCase` component (`HighlightTimeline`). This matches Next.js's own file names and avoids case-only renames, which break on Windows and macOS.
- **Imports:** use the `@/` alias for anything under `src/` (`@/components/highlight-timeline`). Relative imports only within the same folder.
- **API access:** every call to the FastAPI backend goes through the typed client in `web/src/lib/api/`. Components never `fetch` the API directly. The client mirrors the job and candidate contract (ADR-0009, issue #13); until that's adopted, don't invent endpoints or types. It doesn't exist yet: the screen tasks (#29–#31) add it with a mock mode backed by fixture JSON, and #33 connects it to the real API.
- **Server and client components:** components are Server Components by default. Add `"use client"` only where you need state, effects or browser APIs (player, trim controls, job polling), and keep those components small. Cache Components is on (`cacheComponents` in `next.config.ts`, the create-next-app default).
- **Time and IDs:** §3 applies in the UI too. VOD positions stay `number` seconds (`start_s`) in state and are formatted (`1:02:03`) only when rendered. Twitch IDs stay `string`.
- **Styling:** Tailwind utility classes in JSX. Global CSS lives only in `src/app/globals.css` (Tailwind entry point and color tokens). Support light and dark (`dark:` variants follow the OS setting).
- **Accessibility:** semantic elements (`header`, `main`, `nav`, real `<button>`s), one `h1` per page, labelled form controls, visible focus, and WCAG AA text contrast in both color schemes.
- **Next.js version drift:** Next.js 16 changed many APIs. Before relying on memory, check the docs bundled with the installed version in `web/node_modules/next/dist/docs/`. `web/AGENTS.md` (maintained by `next dev`) tells agents the same.
- **Tests:** no frontend test runner yet. The first UI task with logic worth unit-testing (e.g. timeline math) should propose one.
- **Dependencies:** `npm install <pkg>` (or `--save-dev`) inside `web/`. Commit `package.json` and `package-lock.json` together and justify the dependency in the PR (§9).
