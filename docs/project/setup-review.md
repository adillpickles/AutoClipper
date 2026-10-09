# Initial Documentation and Repository Setup Review

**Status:** Review findings; product and ML decisions remain open.
**Reviewed:** 2026-10-09
**Tracking:** [#1](https://github.com/adillpickles/AutoClipper/issues/1)

## Assessment

The documentation provides a useful foundation: a trained model on real data, explicit baselines, broadcaster-grouped splits, reproducible experiments and exportable clips. Most technology choices are appropriately marked Proposed. There is no application code or measured result to validate yet.

The supplied conversation establishes the motivation and changing early ideas; it is not an approved implementation specification. The supplied course design is more concrete, but differs materially from the repository drafts. Confirm the differences before building. Private conversations and source attachments are not copied into this repository.

## GitHub and attribution checks

- `git ls-remote origin` returned no refs. The initial documentation commit is local on `docs/initial-documentation`; the remote has no `main` branch or PRs. The settings are configured, but the repository has not been bootstrapped.
- Ruleset 24814209 is active for `refs/heads/main`, has no bypass actors and requires one approval, dismissal of stale approvals, approval after the last push, resolved review threads and squash-only merges. It also blocks deletion and non-fast-forward updates and requires linear history.
- Repository merge settings allow only squash merging and delete merged branches. Required CI checks are absent, correctly matching the absence of tooling and CI.
- There were no GitHub issues or milestones before this review. Issue #1 now tracks it; no implementation backlog was previously created. The collaborator API lists only the owner; teammate access/review routing still needs setup. A project board was not verified.
- Secret scanning and push protection are enabled. The vulnerability-alert endpoint explicitly reports alerts disabled; private vulnerability reporting is disabled. Automatic Dependabot security updates are also disabled. These settings remain unchanged under OQ-12.
- The two project commits (`dd1f5a5`, `b9b3191`) name the human author and have no AI attribution or co-author trailers. Separate app-owned local checkpoint refs have T3 Code authorship; they are not project branch history and must not be pushed as project commits or rewritten by an agent.
- No AI ownership or contributor credits were found in project files. Agent instructions mention tools to explain workflows. The Code of Conduct's third-party source acknowledgment is not AI authorship and is retained.

## Differences requiring team decisions (OQ-15)

| Topic | Supplied course design | Repository draft | Required decision |
|---|---|---|---|
| MVP | Score the whole VOD, show top 10, preview and export through a web app | Existing-clip ranker first (M3), detector M4, web app M5 | Ratify or amend ADR-0004; define the minimum demo flow |
| Audience | Streamer, clipper/editor and viewer who missed the stream | Clipper primary, streamer secondary; viewer explicitly excluded | Confirm the audience in the PRD |
| Ranking target | Viewer-clipped windows weighted by normalized views | Mature clip strength; early ranking only proposed in OQ-1 | Choose what is learned and available at inference; Twitch labels do not establish short-form view prediction |
| Evaluation | Precision@10 with ±15 s matching, NDCG@10, blind 1-5 ratings against chat-spike clips | Precision@5 for v1; Recall@5/h and IoU/±10 s for v2; yes/maybe/no unmatched-candidate audit | Select one protocol and record metric changes in an ADR |
| Data scale | Approximately 100-200 VODs | At least 150 for baselines, 400 for models across 20-40 channels | Confirm achievable targets; all are planning estimates, not collected data |
| Windows | Flow diagram uses approximately 20 s windows with 10 s stride | 10 s non-overlapping windows | Choose one before labeling; requires ADR and versioned label rules |
| Signal stages | Cheap chat/audio everywhere; speech and optional video on the shortlist | Basic audio first, pretrained audio optional; speech used mainly for captions | Confirm shortlist reranking and ablations without adding dependencies yet |
| Hosting / storage | Hosted frontend, separate API/worker, Postgres job state and object storage | Local files and in-process queue; no database server or cloud requirement | Reconcile ADR-0005/0007; isolate processing from HTTP requests |
| Privacy | Drop names; keep only aggregate chat statistics | Pseudonymized message text and persistent author hashes retained during project | Decide OQ-9 and retention purpose before ingestion; pseudonymization is not aggregate-only storage |
| Team process | GitHub Projects, Friday sync, official Slack and day-to-day Discord | Board optional, async check-in proposed | Confirm the actual team workflow |
| Schedule | Tentative weekly plan from Sep 21 through Dec 4-11, 2026 | Dates described as unknown; actual state remains M0 on Oct 9 | Confirm OQ-13 and replan rather than marking missed targets complete |

## Internal contradictions and implementation gaps

These are findings, not adopted schema, labeling or evaluation changes. Changes to those contracts need an ADR in the same PR.

1. **Chat lag can be applied twice.** Labeling §5 says labels stay in VOD time and lead chat features handle delay, but its Export bullet and architecture §4.6 shift export boundaries again. Viewer-clip intervals already describe source-video time. Recommended: preserve those intervals; align chat-derived detections once, with padding handled separately.
2. **Evaluation population disagrees.** Labeling §4 keeps buffer windows for evaluation; evaluation §4 instead says PR-AUC uses confident negatives plus positives. Choose the population and state its positive-unlabeled limitations. Precision based on viewer clips measures agreement with observed labels, not verified absence of highlights.
3. **Early ranking needs real snapshots.** OQ-1 proposes stream-end inputs, but collection only specifies roughly day-1 and day-7 snapshots. Mature clip clusters can also contain later-created clips. Save a true inference-time snapshot and restrict candidate membership/features to it before using mature outcomes as targets.
4. **Snapshots cannot fit a single-row clip table unchanged.** The clips table describes one row per `clip_id`, while collection needs multiple observations. A versioned snapshot layout or explicit observation key is needed; do not overwrite the earlier views.
5. **Clip count is not independent clipper count.** Labeling §2 calls `n_clips` the number of independent clippers; the schema counts distinct clips. Pick the intended meaning and keep the label definition and schema aligned.
6. **Moment splitting is incomplete.** Splitting long chained overlaps at the largest gap does not define what to do when all clips overlap, or which clip belongs to each split. Specify deterministic splitting and boundary cases before implementing.
7. **Normalization language conflicts.** Features explicitly allow full-VOD unsupervised normalization for offline inference, while labeling says never use test-split statistics. Distinguish per-input VOD normalization from fitted cross-dataset preprocessing; never fit learned preprocessing on held-out labels or pooled test data.
8. **Baseline coverage is inconsistent.** Evaluation defines B4 (chat + loudness) and says a learned model must beat it, but its principles and roadmap M2 mention only B0-B3. Align the reporting checklist with the existing baseline table.
9. **Expired media can block later export.** Collection deliberately retains audio/features, while export fetches source-video segments later. An expired VOD cannot supply video for preview/export. Define unavailable-media behavior and secure permitted demo clips while source media exists; features alone cannot recreate video.
10. **Collector coverage is not guaranteed.** Get Clips has an approximately 1,000-result query cap, sorts broadcaster results by views, and filters clip creation time. Date chunking and a later discovery horizon need an empirical coverage check, especially for low-view and later-created clips.
11. **Chat can expose the label.** The course design explicitly removes posted clip links, but the feature catalog does not define that filter. A clip URL or automated clip announcement can reveal that a clip was just created. Define filtering consistently for training and inference before comparing models; ordinary reactions such as "clip it" need a separately documented policy.

## Changes made in this review and why

- Renamed GitHub labels to plain names; updated all issue forms and label references. Combined the two `data` labels and expanded size names to `small`, `medium`, `large` to avoid ambiguity. Other group meanings and existing colors/descriptions are preserved except the combined data description.
- Strengthened the shared and Claude-specific instructions to explicitly prohibit AI contributor and ownership trailers as well as co-author credits. OQ-14 is resolved by the owner's instruction; assistance disclosure remains distinct from authorship.
- Corrected status and roadmap claims: documentation/templates are local drafts, and the protected branch does not yet exist remotely. Corrected the agent guide's claim about already-existing application code.
- Marked PRD scope and roadmap staging as unresolved relative to the supplied design, added OQ-15/OQ-16 and recorded the supplied tentative schedule under OQ-13. No competing product choice was accepted.
- Updated OQ-11/OQ-12 and status with verified collaborator and security-setting gaps, so configured protections are distinguished from missing team access and optional security setup. No collaborator or security setting was changed.
- Removed the data-model exception allowing nullable-column additions without an ADR because it contradicted AGENTS.md. No schema column or meaning changed.
- Replaced guaranteed clip-completeness language with retrieval limits and documented the verified pagination cap and later-created-clip caveat. Added the same qualification and scope-review note to proposed ADR-0004.

## Verification and remaining work

Verified live repository settings, plain label names, issue-template label existence, Markdown local links, LF endings, diff whitespace, project-commit authorship and absence of attribution trailers. Get Clips facts were checked against the [official endpoint reference](https://dev.twitch.tv/docs/api/reference/#get-clips). No application tests, lint or type checks exist yet; no Twitch collection or model experiment was run.

The owner must resolve remote bootstrap (OQ-16) before a documentation PR can target `main`. Normal branch protection stays intact. Review changes are on `docs/1-review-repository-setup`, not merged. Team choices above remain open; this review does not ratify proposed ADRs.
