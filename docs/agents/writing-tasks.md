# Writing Issues

Use the same plain-English format for tasks, so a teammate or coding agent can finish one from the issue plus the repo. Name the actual work and link the relevant docs directly.

## Title

A short action in plain words: **"Compute chat features for each time window"**.

## Body template

```markdown
### Goal
One or two sentences: what will be true when this is done, and why it matters for the app.

### What to build
- The pieces, in plain words, with likely files (`src/autoclipper/...`).

### Done when
- [ ] Concrete, checkable outcomes (inputs → expected outputs, edge cases).
- [ ] Tests that prove it.

### Depends on
- #N Short title (or "Nothing")

### Not included
- What's deliberately left for another issue.

### How to check
`the command that proves it works`

### Background reading
- `docs/...` (only the sections that matter)
```

## Rules of thumb

- **One PR per issue.** Aim for one module plus its tests and a little wiring. Split anything that would take several days.
- **Depends on** lists only real prerequisites. Anything whose dependencies are merged can be picked up.
- **UI issues** describe the screen states (empty, loading, error, done) and need a screenshot in the PR.
- **External services** (Twitch API, downloads): unit tests use recorded, scrubbed responses; real calls go in `live` tests.
- **Experiments** state the question and the metric before running.
- **Labels:** one or two categories ([labels](../project/labels.md)), plus `stretch` for anything after the MVP.

## Example

> **Title:** Compute chat features for each time window
>
> ### Goal
> Turn a VOD's chat into numbers for every 10-second window (how busy chat is, how much it's laughing or hyped), so the baselines and the model have something to score.
>
> ### What to build
> - `compute_chat_features(windows, messages)` in `src/autoclipper/features/chat.py`, returning one row per window.
>
> ### Done when
> - [ ] Message rate, unique chatters, laugh/hype/shock rates and their z-scores per window.
> - [ ] Empty windows get 0, not blank. A message exactly on a window boundary goes to the later window.
> - [ ] Tests for empty chat, a single message, a boundary message and a VOD where every window is the same.
>
> ### Depends on
> - #22 Split each VOD into time windows
>
> ### Not included
> - Audio features; the training command.
>
> ### How to check
> `uv run pytest tests/features/test_chat.py`
>
> ### Background reading
> - `docs/ml/features.md` § Chat features
