# Running and Recording Experiments

| | |
|---|---|
| **Status** | Proposal (process). The tracking tool is undecided. See OQ-6. |
| **Last updated** | 2026-10-09 |

Experiments are how this project produces claims. They have to be reproducible by someone else, or by an agent, from the repo alone.

## Lifecycle

1. **Open an Experiment issue** (template) with the hypothesis, primary metric, success criterion and baselines **before running anything**.
2. **Branch** `exp/<issue#>-<slug>`.
3. **Write a config** (`experiments/<id>/config.yaml`): dataset version, split, feature families, model + hyperparameters, seed(s).
4. **Run** through the CLI, e.g. `uv run autoclipper train --config experiments/<id>/config.yaml`. Notebooks are for exploration, not for producing reported numbers.
5. **Record** results in `experiments/<id>/README.md` (template below).
6. **Open a PR** with the config, any code changes and the README. Large artifacts stay out of git.
7. **Close the issue** with a one-paragraph conclusion: supported, refuted or inconclusive.

## Experiment IDs

`exp-NNN`, sequential, zero-padded (`exp-001`). Take the next free number when you open the PR. The issue number goes in the README.

## Directory layout (planned)

```text
experiments/
├── README.md                 # index table: id, question, result, link
└── exp-001-chat-only-baseline/
    ├── config.yaml
    └── README.md             # results write-up (template below)
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

## Tracking tool (OQ-6)

**Proposal:** start with the plain-file approach above (config + README + `metrics.json`). It's agent-friendly and needs no service. Add MLflow (local file store) or Weights & Biases only if comparing many runs becomes painful.

## Rules

- **Never edit a past experiment's results.** If something was wrong, add a correction note and re-run as a new ID.
- **Don't cherry-pick seeds.** Report all seeds run.
- **Never put numbers in docs, slides or the README** unless they link back to an `experiments/` entry.
