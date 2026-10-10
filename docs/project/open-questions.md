# Open Questions and Pending Decisions

Each item has a stable ID (`OQ-N`) that issues, PRs and docs refer to. When an item is decided, record the answer here (and in an ADR if it's significant), then move it to **Resolved**.

> **Agents:** if your task depends on an open item, don't decide it yourself. Use the documented proposal if there is one, say so in the PR, or stop and ask.

## Open

| ID | Question | Proposal | Blocks | Owner |
|---|---|---|---|---|
| OQ-1 | **What exactly is the v1 ranking target?** Twitch views of existing clips are known, so predicting them is trivial. | "Early ranking": predict mature (7-day) moment strength from signals available at stream end. See [modeling plan § OQ-1](../ml/modeling-plan.md#oq-1-the-v1-target-needs-a-decision). | M3 modeling | Team |
| OQ-2 | **License** | MIT ([ADR-0006](../decisions/0006-license.md)) | Public reuse | Repo owner + team |
| OQ-3 | **Use external short-form engagement data** (clip-account videos + views) for a short-form performance model? Under what terms? | Stretch only. Check the tool's and platforms' terms first. | Stretch model | Team |
| OQ-4 | **Which channels, and how many VODs?** | 20–40 English channels, mixed categories and sizes; ≥ 150 VODs for M2 ([collection plan](../data/collection-plan.md)) | M1 | Team |
| OQ-5 | **Where does shared data live, and which machine runs the collector?** | An always-on team machine or small VM for the collector. A shared cloud folder for processed snapshots. | M1 at scale | Team |
| OQ-6 | **Experiment tracking tool** | Plain files first; MLflow (local) if needed ([experiments](../ml/experiments.md)) | M2 | Team |
| OQ-7 | **Ratify proposed ADRs** 0002, 0003, 0004, 0005, 0007 | Accept as written, or amend | Scaffold (0002/0003/0005) | Team |
| OQ-8 | **Twitch app credentials:** one shared team app or one per member? | One team app, credentials shared privately | M1 | Repo owner |
| OQ-9 | **Chat data retention:** delete raw replay after parsing? Redact @mentions? | Yes to both ([governance](../data/data-governance.md#2-privacy-chat-data)) | M1 | Team |
| OQ-10 | **Online posting experiment:** go/no-go, which streamer(s), which accounts | Decide after M3. Needs written permission and a pre-registered plan. | Stretch | Team |
| OQ-11 | **Roles and review routing:** who reviews what? Use CODEOWNERS? | Only the owner is currently listed as a collaborator, and pending-invitations API returned none. Owner reports teammates invited; verify access separately without blocking this documentation PR. No CODEOWNERS proposed for now. | Future team PR review | Team |
| OQ-12 | **Repository security settings:** secret scanning push protection, Dependabot alerts, private vulnerability reporting | Scanning and push protection verified enabled; alerts and private reporting verified disabled. Proposal: enable the remaining two. | — | Repo owner |
| OQ-13 | **Course deliverables and dates:** PRD due date, checkpoints, demo date | Supplied design has a tentative weekly schedule ending Dec 4-11, 2026. Confirm dates and replan from current M0 state; see [setup review](setup-review.md). | Planning | Team |
| OQ-15 | **Reconcile supplied course design and repo proposals** | Review the differences and internal contradictions in [setup review](setup-review.md); record scope/hosting choices in the relevant proposed ADRs and label/schema/metric changes in an ADR. | Implementation of affected contracts | Team |

## Resolved

| ID | Question | Decision | Date | Record |
|---|---|---|---|---|
| — | Branching and merge policy | Trunk-based, 1 approval, squash only | 2026-10-09 | [ADR-0008](../decisions/0008-trunk-based-development-squash-merge.md) |
| OQ-14 | AI attribution policy | Owner confirmed: no AI co-author, contributor or ownership trailers, attribution lines or generated-by footers. Keep the PR template's assistance disclosure. | 2026-10-09 | [AGENTS.md](../../AGENTS.md#11-notes-for-specific-agents) |
| OQ-16 | Bootstrap remote `main` | Owner authorized the empty init commit as the remote base. Push succeeded with protection active. One combined documentation PR uses a temporary admin PR-only bypass; remove it immediately after owner-confirmed squash merge. Normal work retains the no-direct-push and one-review rules. | 2026-10-09 | [Setup review](setup-review.md#publication-follow-up) |
