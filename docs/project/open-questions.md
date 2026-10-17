# Open Questions and Pending Decisions

Each item has a stable ID (`OQ-N`) that issues, PRs and docs refer to. When an item is decided, record the answer here (and in an ADR if it's significant), then move it to **Resolved**.

> **Agents:** don't decide an open item yourself. If your task depends on one, stop and ask your human; keep working on the parts that don't depend on it. If your human explicitly says to build a throwaway prototype using the proposal below, note that assumption in the PR.

## Open

| ID | Question | Proposal | Blocks | Owner |
|---|---|---|---|---|
| OQ-1 | **What exactly does the model learn to predict?** (first training target and what's available at inference) | Independent discovery/scoring from chat + audio, using viewer clips as noisy positives. Early ranking is only possible with real cutoff snapshots. Decide the outcome horizon, how views are normalized for unseen channels, and what information exists at inference. See [modeling plan § OQ-1](../ml/modeling-plan.md#oq-1-the-v1-target-needs-a-decision). | Training and reporting a model | Team |
| OQ-2 | **License** | MIT ([ADR-0006](../decisions/0006-license.md)) | Public reuse | Repo owner + team |
| OQ-3 | **Use external short-form engagement data** (clip-account videos + views)? | Out of scope this semester ([PRD scope](../product/prd.md#scope-at-a-glance)). Revisit only after the semester, after checking the tool's and platforms' terms. | — | Team |
| OQ-4 | **Which channels, and how much data?** | Small pilot first, then scale from measured cost and coverage. The design doc's ~100–200 VODs is a target, not a gate ([collection plan](../data/collection-plan.md)). | Real pilot/scale | Team |
| OQ-5 | **Where does shared data live, and which machine runs the collector?** | An always-on team machine or small VM for the collector. A shared cloud folder for processed snapshots. | Collection at scale | Team |
| OQ-6 | **Experiment tracking tool** | Plain files first; MLflow (local) if needed ([experiments](../ml/experiments.md)) | M2 | Team |
| OQ-8 | **Twitch app credentials:** one shared team app or one per member? | One team app, credentials shared privately | M1 | Repo owner |
| OQ-9 | **Keep only aggregate chat stats, or sanitized messages too?** | The design doc keeps aggregates only. If messages are kept, decide the fields, hashing, redaction, purpose and deletion deadline. Raw downloads stay on the collector and are deleted after validation ([governance](../data/data-governance.md#2-privacy-chat-data)). | Real chat ingestion/storage | Team |
| OQ-10 | **Online posting experiment:** go/no-go, which streamer(s), which accounts | Decide after M3. Needs written permission and a pre-registered plan. | Stretch | Team |
| OQ-11 | **Teammate access and review routing** | Only the owner is currently a collaborator, with no pending invitations. Add teammates (write access) so they can be assigned issues and approve PRs. No CODEOWNERS for now. | Teammates picking up issues; normal PR review | Repo owner |
| OQ-12 | **Repository security settings** | Secret scanning and push protection are on. Proposal: also enable Dependabot alerts and private vulnerability reporting. | — | Repo owner |
| OQ-15 | **Adopt the remaining analysis contracts** | Ratify the relevant [ADR-0009](../decisions/0009-analysis-and-evaluation-contract-repair.md) pieces before building them: window size and moment grouping, clip snapshot coverage, evaluation matching/population/budget, the human taste-test rubric (before the study), candidate output shape (before app integration). | Implementing those contracts (not synthetic prototypes) | Team |
| OQ-17 | **Which real data sources are we allowed to collect, keep and show?** | Record source-specific evidence under [governance](../data/data-governance.md#authorization-boundary-before-real-collection-oq-17): research approval, storage/deletion and player rules. Public availability alone isn't enough. Creator-provided media is another option. | Real data collection and media use | Owner + team |
| OQ-18 | **Hosting and the app's database** | Run everything locally first; a laptop is the demo-night fallback either way. If time allows, host per the design doc: Vercel (frontend), Render/Railway (API + worker), Supabase Postgres (jobs/results), S3-compatible bucket (exports). | Hosted deployment only | Team |

## Resolved

| ID | Question | Decision | Date | Record |
|---|---|---|---|---|
| — | Branching and merge policy | Trunk-based, 1 approval, squash only | 2026-10-09 | [ADR-0008](../decisions/0008-trunk-based-development-squash-merge.md) |
| OQ-7 | Ratify core ADRs | Accepted 0002 (tooling), 0003 (layout), 0004 (staged scope), 0005 (pipeline storage), 0007 (FastAPI + Next.js), matching the team's project design doc. License (0006) stays open as OQ-2; hosting moves to OQ-18. | 2026-10-16 | [ADR index](../decisions/README.md) |
| OQ-13 | Course deliverables and dates | Course syllabus: Demo of Demos **Fri Dec 4, 2026**; final presentations and demo night **Fri Dec 11, 2026**. Week-by-week plan is replanned at the weekly sync. | 2026-10-16 | [Roadmap](../product/roadmap.md) |
| OQ-14 | AI attribution policy | No AI co-author, contributor or ownership trailers, attribution lines or generated-by footers. No AI-assistance disclosure required in PRs. | 2026-10-16 | [AGENTS.md](../../AGENTS.md#11-notes-for-specific-agents), [ADR-0010](../decisions/0010-agent-review-and-publication-workflow.md) |
| OQ-16 | Bootstrap remote `main` | The empty init commit was pushed as `main` with protection active. A temporary admin bypass lets the owner merge initial setup PRs; remove it once teammates are reviewing. | 2026-10-09 | [Git workflow](../development/git-workflow.md#main-branch-rules-enforced-by-a-repository-ruleset) |
