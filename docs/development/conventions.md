# Coding Conventions

| | |
|---|---|
| **Status** | Binding ([ADR-0002](../decisions/0002-language-and-tooling.md) and [ADR-0003](../decisions/0003-repository-layout.md) are accepted). Tool config in `pyproject.toml` is authoritative for anything automated. |
| **Last updated** | 2026-10-19 |

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
| Pseudonymized identifiers end with `_hash` | `user_hash` |

## 4. Configuration

- All settings come from `autoclipper.config` (Pydantic Settings reading env vars / `.env`). **Never read `os.environ` elsewhere.**
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

## 10. Frontend (proposal)

TypeScript strict mode, Next.js App Router, ESLint + Prettier, components in `web/src/components/`, API calls through one typed client module. Detailed conventions will be added when `web/` is scaffolded (ADR-0007).
