# Testing Strategy

| | |
|---|---|
| **Status** | Binding. pytest is configured in `pyproject.toml` (`[tool.pytest]`). The coverage target below is still a proposal. |
| **Last updated** | 2026-10-20 |

## Goals

Tests protect the parts where bugs are **silent**: a label shifted by 10 seconds or a z-score computed with test data won't crash anything. It will quietly make results wrong. Test those parts hardest.

## Test types

| Type | What | Where | Network? | Runs in CI |
|---|---|---|---|---|
| **Unit** | Pure functions: parsing, moment merging, windowing, features, metrics | `tests/<module>/` | ❌ never | ✅ always |
| **Contract** | A stage's output matches [data model](../architecture/data-model.md) (columns, dtypes, keys) | `tests/contracts/` | ❌ | ✅ |
| **Integration** | Several stages chained on a tiny synthetic VOD (generated audio + fake chat + fake clips) | `tests/integration/` | ❌ | ✅ |
| **Live API** | Authorized real Twitch API / tool calls | `tests/live/`, marked `@pytest.mark.live` and excluded by default | ✅ | ❌ (manual, needs `.env` and source authorization) |
| **Metric checks** | Eval metrics match hand-computed values on toy inputs | `tests/eval/` | ❌ | ✅ |

## Must-have tests (by area)

- **Duration parsing:** `"3h8m33s"`, `"45m"`, `"59s"`, `"1h0m0s"` → seconds.
- **Moment merging:** overlapping, gap-boundary, disjoint, long connected chains, null offsets, duplicate snapshot observations and deterministic ties under the adopted grouping rule.
- **Windowing:** exact boundaries, last partial window, muted-segment flags.
- **Labels:** window/moment overlap at edges (touching ≠ overlapping, unless the rule says so), confident-negative buffer.
- **Features:** empty chat window → raw counts/rates 0 not NaN (its z-score still follows the formula), zero-variance VOD → z-score 0, lead/lag at VOD edges.
- **Pseudonymization:** the same name + salt gives the same hash, a different salt gives a different hash, and the raw name never appears in output.
- **Splits:** no broadcaster appears in two splits. Deterministic given the seed.
- **Metrics:** NDCG, recall@k, IoU matching and bootstrap on tiny hand-worked examples.
- **Export:** FFmpeg command construction (as data, without running FFmpeg in unit tests). One integration test on a generated 5-second clip ([FFmpeg tests](#ffmpeg-tests)).

## Fixtures

- Live in `tests/fixtures/`. **Synthetic only**: fake usernames (`user_001`), invented messages, generated audio (sine bursts, noise).
- Keep them tiny (KB). Generate larger ones on the fly in a fixture function.
- Recorded API responses (for client tests) must be **scrubbed**: replace usernames and IDs with fakes.
- **Isolation from your machine:** an autouse fixture in `tests/conftest.py` runs every test with its own empty `tmp_path` as the working directory. It removes the environment variables behind AutoClipper's settings, clears the `get_settings()` cache before and after the test, and undoes `configure_logging()` afterwards. Tests therefore never read your `.env`, and the default data and artifacts dirs point into `tmp_path`. Set a value with `monkeypatch.setenv(...)` or by writing `tmp_path / ".env"`. Find fixture files relative to `__file__`, not the working directory. Explicitly selected `live` tests are the exception: they keep your working directory, `.env` and environment so they can use real credentials (the settings cache and logging are still reset).

## Conventions

- pytest, with plain `assert`s. Use `pytest.approx` for floats.
- File naming: `tests/<module>/test_<file>.py`. Test names describe behavior: `test_merge_clips_joins_clips_within_gap`.
- One behavior per test. Use parametrize for edge-case tables.
- No sleeps, no network, no reliance on wall-clock time in unit tests. Inject clocks and HTTP clients.
- Test **behavior, not implementation**. A refactor that keeps behavior shouldn't break tests.
- Bug fixes come with a regression test that fails before the fix.
- Live tests are deselected by default: a hook in `tests/conftest.py` adds `and not live` to every `-m` expression that doesn't name `live` (a marker alone doesn't exclude anything, and an `addopts` default would be replaced by any `-m` on the command line). So `-m "not slow"` still skips them; only an expression that names `live`, such as `-m live`, can select them. `tests/test_pytest_config.py` checks this. Explicit live runs need the appropriate credentials/authorization and never run in ordinary CI.

## Configuration

Set in `[tool.pytest]` in `pyproject.toml` (pytest 9 native TOML config):

- `testpaths = ["tests"]`, so a bare `uv run pytest` runs the whole suite.
- `--import-mode=importlib`: test folders need no `__init__.py`, and files in different folders may share a name (`tests/ingest/test_chat.py`, `tests/features/test_chat.py`).
- `strict = true`: an unregistered marker is an error, so a typo like `@pytest.mark.lvie` can't slip a live test into CI. Register new markers under `markers`. `xfail` is strict too: an unexpected pass fails.
- `filterwarnings = ["error"]`: warnings fail the test. If a third-party library warns and you can't fix the cause, add a targeted `ignore:` entry with a comment saying why.

## FFmpeg tests

FFmpeg is a system binary ([ADR-0002](../decisions/0002-language-and-tooling.md)), and every call to it goes through `autoclipper.ffmpeg`. Tests come in two kinds:

- **Unit tests** need no FFmpeg. Check the argument list that `ffmpeg_command` / `ffprobe_command` builds, or pass a fake `runner` to `run_command`, `probe` or `ffmpeg_version` that records the commands and returns canned `CommandResult`s (`RecordingRunner` in `tests/test_ffmpeg.py` is an example).
- **FFmpeg tests** run the real `ffmpeg`/`ffprobe`. Mark them `@pytest.mark.ffmpeg`; tests that use the `make_test_media` fixture get the marker automatically.

When `ffmpeg` or `ffprobe` is not on `PATH`, `tests/conftest.py` decides what happens to FFmpeg tests:

| Where | `CI` environment variable | FFmpeg tests |
|---|---|---|
| Locally | unset, or anything other than `true`/`1` | **Skipped**, with the reason in the `-ra` summary |
| CI | `true` or `1`, case-insensitive (GitHub Actions sets `CI=true`) | **Error**, so CI can't go green by skipping them |

CI installs FFmpeg on Ubuntu and Windows (`.github/workflows/ci.yml`) and prints its version. Locally, `CI=true uv run pytest` gives the CI behavior, and `uv run pytest -m "not live and not ffmpeg"` leaves FFmpeg tests out (a `-m` on the command line replaces the default `-m "not live"`, so keep `not live` in it).

`make_test_media` returns a function that generates a small file in the test's `tmp_path` from FFmpeg's `testsrc2` test pattern (H.264) and a constant `sine` tone. Annotate the fixture as `Callable[..., Path]`:

```python
def test_probe_reads_a_tone(make_test_media: Callable[..., Path]) -> None:
    video = make_test_media()  # test.mp4: 1 s, 320x180 at 25 fps, 48 kHz stereo AAC
    tone = make_test_media(
        "tone.wav", duration_s=5.0, video_size=None, sample_rate_hz=16_000, channels=1
    )
```

Keyword arguments: `duration_s`, `video_size` (`(width, height)`, or `None` for audio only), `frame_rate`, `sample_rate_hz`, `channels`, `tone_hz`. The file extension picks the container and its default audio codec. Generated media exists only in `tmp_path`; never commit it.

## Commands

```bash
uv run pytest                       # unit + contract + integration; live tests are deselected
uv run pytest tests/test_cli.py     # one file (or a folder, or file::test_name)
uv run pytest -m live               # only live tests (needs .env and source authorization); never in CI
uv run pytest -m "not live and not ffmpeg"   # also leave out tests that run the real FFmpeg
uv run pytest --cov=autoclipper     # with a coverage report (branch coverage, missing lines)
```

`uv run pytest -m live` exits with code 5 ("no tests ran") while no live tests exist.

Coverage target (proposal): ≥ 80% for `labels/`, `features/` and `eval/`. No hard target elsewhere. Meaningful tests beat a coverage number.
