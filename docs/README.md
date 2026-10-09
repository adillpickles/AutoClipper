# AutoClipper Documentation

Start with the [project README](../README.md) for the overview, and [AGENTS.md](../AGENTS.md) for the rules every contributor (human or AI) follows.

## Map

| Section | Docs | Answers |
|---|---|---|
| **Product** | [PRD](product/prd.md) · [Roadmap](product/roadmap.md) | What are we building, for whom, and in what order? |
| **Architecture** | [Overview](architecture/overview.md) · [Data model](architecture/data-model.md) | How does the system fit together? What are the contracts between stages? |
| **Data** | [Index](data/README.md) · [Governance](data/data-governance.md) · [Sources](data/data-sources.md) · [Collection plan](data/collection-plan.md) · [Labeling](data/labeling.md) | Where does data come from, how is it labeled, and what are the rules? |
| **ML** | [Index](ml/README.md) · [Modeling plan](ml/modeling-plan.md) · [Features](ml/features.md) · [Evaluation](ml/evaluation.md) · [Experiments](ml/experiments.md) | What do we model, and how do we know it works? |
| **Development** | [Setup](development/setup.md) · [Conventions](development/conventions.md) · [Testing](development/testing.md) · [Git workflow](development/git-workflow.md) | How do I set up, write code, test it and ship it? |
| **Agents** | [Overview](agents/README.md) · [Writing tasks](agents/writing-tasks.md) · [Reviewing agent PRs](agents/reviewing-agent-prs.md) · [Parallel work](agents/parallel-work.md) · [Prompts](agents/prompts.md) | How do we build this with AI agents without losing consistency? |
| **Decisions** | [ADR index](decisions/README.md) | Why are things the way they are? |
| **Project** | [Status](project/status.md) · [Open questions](project/open-questions.md) · [Risks](project/risks.md) · [Team process](project/team-process.md) · [Labels](project/labels.md) · [Glossary](project/glossary.md) | What's the current state, what's undecided, and how do we work? |

## Reading paths

**New contributor (human):** README → [PRD](product/prd.md) → [Architecture](architecture/overview.md) → [Setup](development/setup.md) → [CONTRIBUTING](../CONTRIBUTING.md) → [Status](project/status.md)

**AI agent starting a task:** [AGENTS.md](../AGENTS.md) → [Status](project/status.md) → the issue → the docs listed in AGENTS.md § 3 for that task type

**Working on the models:** [Labeling](data/labeling.md) → [Features](ml/features.md) → [Modeling plan](ml/modeling-plan.md) → [Evaluation](ml/evaluation.md) → [Experiments](ml/experiments.md)

**Reviewing a PR:** [CONTRIBUTING § Review](../CONTRIBUTING.md#5-pull-requests-and-review) → [Reviewing agent PRs](agents/reviewing-agent-prs.md)

## Doc conventions

- Every doc starts with a status line: **Proposal**, **Binding**, **Reference** or **Living**, plus a last-updated date.
- "Proposal" means a recommendation, not yet agreed. "Accepted" or "Binding" means follow it.
- ⚠️ marks a fact that still needs verification.
- No invented results. Metrics appear only with a link to an experiment.
- Diagrams use Mermaid. Links are relative.
