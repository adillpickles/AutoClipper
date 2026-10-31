# Running and Recording Experiments

| | |
|---|---|
| **Status** | Current. Tracking uses plain files in `experiments/`. |
| **Last updated** | 2026-10-31 |

Experiments are how this project produces claims. They have to be reproducible by someone else, or by an agent, from the repo alone.

## Lifecycle

1. **Record the question and comparison.** Before a reported run, write the hypothesis, primary metric, success criterion and baselines in the existing issue or experiment README. Exploratory checks don't need a separate issue.
2. **Branch** `exp/<issue#>-<slug>` for a tracked experiment, or use the current task's branch for supporting exploration.
3. **Write a config** (`experiments/<id>/config.yaml`): dataset version, split, feature families, model + hyperparameters, seed(s).
4. **Run** through the CLI, e.g. `uv run autoclipper train --config experiments/<id>/config.yaml`. Notebooks are for exploration, not for producing reported numbers.
5. **Record** results: copy the summary `metrics.json` from `artifacts/reports/<run_id>/` into `experiments/<id>/` and write `experiments/<id>/README.md` (template below).
6. **Publish when requested** with config, code and README ([CONTRIBUTING § 6](../../CONTRIBUTING.md#6-open-a-pull-request)). Large artifacts stay out of git.
7. **Record the conclusion:** supported, refuted or inconclusive. Link the experiment from its issue when one exists.

## Experiment IDs

`exp-NNN`, sequential, zero-padded (`exp-001`). Take the next free number when you open the PR. The issue number goes in the README.

## Directory layout (created with the first experiment)

```text
experiments/
├── README.md                 # index table: id, question, result, link
└── exp-001-chat-only-baseline/
    ├── config.yaml
    ├── metrics.json          # summary metrics from the evaluation run
    └── README.md             # results write-up and notes (template below)
```

Model weights, predictions and full reports go to `artifacts/` (git-ignored) under the same run ID.

## Results README template

```markdown
# exp-NNN: <short title>

- **Issue:** #
- **Question / hypothesis:**
- **Primary metric + success criterion (set in advance):**
- **Dataset version / split:**
- **Git SHA:**
- **Seeds:**
- **Command:** `uv run autoclipper train --config experiments/exp-NNN/config.yaml`

## Results

| Model | Metric (95% CI) | vs. reference |
|---|---|---|

## Conclusion

Supported / refuted / inconclusive, and why.

## Notes and caveats

Anything surprising, threats to validity, follow-ups.
```

## Tracking

Plain files: config, `metrics.json` and the README in `experiments/<id>/`. It's agent-friendly and needs no service. Add MLflow (local file store) only if comparing many runs stops being manageable this way.

## Rules

- **Never edit a past experiment's results.** If something was wrong, add a correction note and re-run as a new ID.
- **Don't cherry-pick seeds.** Report all seeds run.
- **Never put numbers in docs, slides or the README** unless they link back to an `experiments/` entry.
