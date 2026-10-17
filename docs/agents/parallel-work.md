# Running Agents in Parallel

Several agents (and humans) working at once is the fastest way to move, and also the fastest way to create merge conflicts and inconsistent code. These rules keep parallel work clean.

## 1. Partition by module, not by layer

The pipeline is split into modules with file-based contracts ([architecture](../architecture/overview.md#3-pipeline)), so parallel tasks should map to **different modules**:

| Good parallel set | Bad parallel set |
|---|---|
| `ingest/twitch_api.py`, `labels/moments.py`, `eval/metrics.py` | Two tasks both editing `features/chat.py` |
| Chat features + audio features (separate files) | "Add logging everywhere" + any feature task |

Before starting parallel work, check each issue's "Likely files touched" section for overlap.

## 2. One worktree per agent

Use git worktrees so each agent has its own checkout and branch:

```bash
git fetch origin
git worktree add ../AutoClipper-12 -b feat/12-chat-features origin/main
git worktree add ../AutoClipper-14 -b feat/14-audio-features origin/main
# run one agent session in each directory
# when merged:
git worktree remove ../AutoClipper-12
```

Each worktree needs its own `.venv` (`uv sync`) and a copy of `.env`. `data/` can be shared by pointing `AUTOCLIPPER_DATA_DIR` at a common absolute path.

## 3. Shared files need a single owner

Some files are touched by almost everything. Change them in **small, dedicated PRs** merged quickly, not inside feature PRs:

- `pyproject.toml` / `uv.lock` (dependencies)
- `src/autoclipper/config.py`
- `src/autoclipper/cli.py` (prefer one subcommand module per feature)
- `docs/architecture/data-model.md`, `docs/ml/features.md` (contracts)
- `AGENTS.md`

If a feature task needs a new dependency or config key, merge that first as its own tiny PR, or coordinate in the issue.

## 4. Contracts first

When two parallel tasks connect (e.g. "windows" produces what "chat features" consumes), **merge the contract first**: the schema in `data-model.md`, plus a stub or fixture. Then both sides build against it.

## 5. Merge order

- Merge in dependency order. Rebase the others onto the new `main` before review.
- If two PRs conflict, the second one to merge resolves the conflict.
- Re-run tests after every rebase.

## 6. Status hygiene

- Assign the issue when an agent starts on it.
- Keep `docs/project/status.md` current. It's the shared "what exists now" that every new session reads. Update it in the PR that changes the state, not in a separate pass.
