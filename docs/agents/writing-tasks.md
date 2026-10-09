# Writing Agent-Ready Tasks

A task is **agent-ready** when an agent with no prior context can finish it correctly using only the issue and the repo. Most bad agent output comes from vague tasks, not weak agents.

## The checklist

An issue gets the `agent-ready` label only when it has:

- [ ] **A goal** in 1–3 sentences: what will be true, and why.
- [ ] **Links to the governing docs**: the exact sections, e.g. `docs/data/labeling.md §2`.
- [ ] **Concrete acceptance criteria**: inputs → expected outputs, including edge cases.
- [ ] **Scope boundaries**: likely files touched, and what's explicitly **out of scope**.
- [ ] **A verification step**: a command or check that proves it works.
- [ ] **No open decisions**: anything undecided is resolved in the issue or named as "use the documented proposal".
- [ ] **A size of S or M.** L tasks get split first.
- [ ] **Dependencies** listed (`Depends on #12`) and already merged.

## Good vs. bad

**Bad:**

> Add chat features.

The agent will guess the window size, invent feature names, maybe add a library, and test nothing in particular.

**Good:**

> **Goal:** Compute the core chat features per window so baselines B2/B4 can run.
>
> **Context:** Feature definitions: `docs/ml/features.md` § Chat features (only `chat_msg_rate`, `chat_msg_rate_z`, `chat_unique_rate`, `chat_emote_rate`). Inputs: `chat_messages` and `windows` tables per `docs/architecture/data-model.md`. Depends on #12 (merged).
>
> **Acceptance criteria:**
> - [ ] `autoclipper.features.chat.compute_chat_features(windows, messages) -> DataFrame` keyed by `window_id`, with exactly those 4 columns + `feature_version="chat-v0"`
> - [ ] Windows with no messages → 0 (not NaN). Zero-variance VOD → `_z` = 0.
> - [ ] A message at exactly `end_s` belongs to the *next* window
> - [ ] Unit tests for: empty chat, single message, boundary message, zero variance
>
> **Out of scope:** lead/lag features, emote lexicons, CLI wiring (separate issues).
>
> **Verify:** `uv run pytest tests/features/test_chat.py`

## Sizing

| Size | Rough effort | Agent suitability |
|---|---|---|
| S | < 2 h human-equivalent; one module | Ideal |
| M | Half a day to a day; a module + tests + small wiring | Good, with a plan step first |
| L | Multiple days or several modules | **Split it.** Agents drift on long tasks. |

## Task types that need extra care

- **Schema / label / metric changes:** the issue must say "ADR required". Usually a human writes the ADR first.
- **Anything with external services** (Twitch API, downloads): include the exact endpoints and the documented behavior, and link `docs/data/data-sources.md`. Tell the agent to write tests against recorded, scrubbed responses, not live calls.
- **Exploratory or research work:** use the Research spike template with a time box and a written deliverable, not a Task.
- **UI work:** include a sketch or a description of the screen states (empty, loading, error, success).

## Writing the issue with an agent

It's fine to have an agent draft issues: "Read docs/product/roadmap.md M2 and draft agent-ready issues for each item using the Task template." **A human must review every drafted issue** before labeling it `agent-ready`, especially the acceptance criteria.
