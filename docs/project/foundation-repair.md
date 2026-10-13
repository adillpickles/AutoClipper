# Foundation Repair: PR #2

**Date:** 2026-10-13
**Scope:** Local documentation/configuration repair on `docs/1-review-repository-setup`. No publication, merge, data collection or GitHub settings changes authorized.
**Comparison points:** Original PR head `c2ab5d36e8912b3cb5e1ef605a65e8a26e3525f7`; base `dd1f5a5c6883b0f841ed993d173ad4de41d02f3d` (empty initialization commit).

## Evidence and product alignment

Independently inspected the full repository and PR metadata, the complete F1–F14 audit, the original course design (including its graphical flow), relevant historical conversation, and current official Twitch references. Private attachments/conversations and technical inspiration remain outside this repository.

The source design includes viewers, streamers and editors, whole-VOD discovery, preview and export through an app. The owner's clarification preserves those goals while leaving application form and analysis strategy open. API clips can be useful candidates/signals/weak labels; views are popularity evidence, not objective quality. A clip-ranking prototype remains an option, not the finished MVP. One trained scorer may support discovery and ranking; neural networks, TikTok-view prediction, particular window sizes and arbitrary corpus counts are not requirements. UI/export development proceeds alongside ML.

## F1–F14 adjudication

| Finding | Independent evidence and disposition | Repair / remaining boundary |
|---|---|---|
| **F1 Scope authority** | **Verified and repaired:** README excluded viewers; canonical instructions fixed a ranker/detector sequence; modeling called short-form prediction the true target. | [PRD](../product/prd.md), README, AGENTS, roadmap, architecture and proposed ADR-0004/0007 now align with the clarified shared app/discovery workflow. Technical strategy remains open. |
| **F2 Double chat lag** | **Verified and repaired:** labeling preserved source-time labels yet export and candidate generation shifted them again. | [Labeling §5](../data/labeling.md#5-chat-lag) and export consume source-time intervals; raw chat-time heuristics align once before candidates. Padding/trim are separate. |
| **F3 Temporal observations** | **Verified and repaired; early-ranking risk conditional:** one-row-per-clip storage conflicted with repeated snapshots; day-one counts cannot establish stream-end inputs. | [Snapshot proposal](../architecture/data-model.md#clips) is immutable with manifests/lineage. Early-ranking membership/boundaries/features freeze at a real cutoff; target/cutoff mapping needs OQ-1 before that experiment. |
| **F4 Evaluation/normalization** | **Verified and repaired; interpretation narrowed:** populations conflicted and cross-dataset fitting was conflated with per-input normalization. Claimed postability lower bound/adjusted precision lacked support. | [Evaluation](../ml/evaluation.md) proposes all annotated windows, separates observed-clip agreement from human quality, and removes unspecified correction. Offline per-input normalization/look-ahead are valid; unseen-channel outcome transformation and protocol edge cases remain adoption decisions. |
| **F5 Clip-link leakage** | **Verified and repaired as a proposal:** original design removed giveaway links; feature rules omitted that filter. | [Feature preprocessing](../ml/features.md#chat-preprocessing-and-leakage) removes whole URL/recognized-announcement messages consistently before aggregates. Version and ratify initial patterns; ordinary "clip it" reactions remain eligible. |
| **F6 Expired video** | **Verified and repaired:** retained features/audio cannot supply later video preview/export. | Architecture/collection distinguish analytical/media availability, explain unavailable operations and require a small permitted demo/evaluation media set while available. Runtime/tool reliability is untested. |
| **F7 Permissions** | **Verified policy gap; applicability intentionally unresolved:** public/low-volume research was treated too loosely as a basis for retention/sharing. | [Governance](../data/data-governance.md#authorization-boundary-before-real-collection-oq-17) summarizes relevant official conditions and adds source-specific OQ-17. This is not a finding that the project is prohibited. Synthetic work remains possible. |
| **F8 Moment semantics** | **Verified, count repaired; grouping deliberately deferred:** distinct clips are not independent creators; "largest gap" cannot specify all overlapping chains/member assignments. | `n_clips` consistently counts clip IDs. The incomplete split rule is withdrawn. Retaining connected groups separately from export duration is proposed; adopt gap/tie/ID/splitting behavior before labeling. |
| **F9 Unlabeled inference** | **Verified and repaired as a schema proposal:** windows required labels/split even for unseen inputs. API empty/null cases lacked normalization. | Unlabeled [windows](../architecture/data-model.md#windows) plus separate training annotations; missing ground truth is not false. Document empty VOD-link and null muted-list normalization. |
| **F10 Privacy departure** | **Verified; shorthand repaired, choice deferred:** pseudonymized messages/hashes differ from source aggregate-only retention; downloads can transiently contain names. | OQ-9 explicitly chooses fields/purpose/linkage/redaction/deadline before real ingestion. Parsing schema, setup and salt example do not authorize persistent message retention. |
| **F11 Agent workflow** | **Verified and repaired:** shared loop instructed PR creation while Claude/prompts required permission; independent audit/verified repair was absent. | [AGENTS §7](../../AGENTS.md#7-how-to-do-a-task-the-standard-loop) is canonical. Plan appropriately, validate, independent audit, verify findings/repair, re-audit when needed, authorized publication, human review, separately authorized merge. Claude guidance is thin. |
| **F12 Fixture secrets** | **Reproduced and fixed:** the broad negation allowed `.env`, `.pem` and credential JSON under fixtures. No actual leaked credential was found. | Narrow tabular exceptions; place secret rules afterward. Positive and negative ignore probes cover nested paths and overlapping `.env.parquet`/`.env.npz` names. |
| **F13 Reviewer access** | **Verified and intentionally deferred:** collaborator API still lists only owner; invitations endpoint returns none. | OQ-11 remains an operational follow-up for qualifying teammate reviews. No invitations/permissions/settings changed. Bootstrap bypass is not technically PR-specific. |
| **F14 Research delivery gates** | **Verified and repaired:** compulsory model progression and baseline victories delayed later app work. | Roadmap and modeling separate delivery from research outcomes; report unfavorable comparisons honestly and add complexity only when justified. |

None of the numbered findings was wholly disproved. F3's early-ranking leakage is conditional; F4 does not prohibit valid offline normalization/look-ahead; F7 does not establish a legal prohibition; F8/F10/F13 require decisions or external action rather than invented fixes.

The earlier Get Clips warning is accurate and already documented: limits and incomplete coverage are empirical risks, not proof the collection strategy cannot work. B4 checklist drift, Node 20 EOL guidance, Windows-script line-ending exceptions and default live-test exclusion were also corrected. Actual Windows setup remains a scaffold deliverable.

## Substantive changes

- **Product/staging:** README, AGENTS, PRD/roadmap, architecture, modeling, ADR-0003/0004/0007 and glossary. Preserve the app/discovery goal and optional ranker-first sequencing without mandating two models or a neural progression.
- **Proposed contracts:** data model, labeling, features, evaluation and collection under [ADR-0009](../decisions/0009-analysis-and-evaluation-contract-repair.md). Immutable observations, source-time export, separate inference annotations and honest evaluation now agree; pending details have explicit implementation boundaries.
- **Governance/source facts:** data governance/sources, setup, `.env.example`, risks and open questions. Separate permissions from assumptions, aggregate-only from message retention, and saved analysis from playable video. No real data collected.
- **Development practice:** AGENTS, contributor/Git guides, agent guides/prompts and lightweight CLAUDE under [ADR-0010](../decisions/0010-agent-review-and-publication-workflow.md). This owner-directed workflow decision does not ratify product/ML proposals or alter ADR-0008 review protections.
- **Configuration/state:** `.gitignore`, testing/setup guidance, ADR/doc indexes, status/changelog and historical-review signpost. No code, dependency, CI, label or attribution change.

The repair exceeds the usual small-diff guideline because the same few contracts appeared across many documents. It preserves their structure and history rather than replacing the documentation set.

## Validation

Complete-branch checks passed on 2026-10-13:

- **62 files / 52 Markdown files:** UTF-8 decoding, LF endings, final newlines, no BOM/conflict markers and documentation/configuration-only inventory.
- **285 relative links/anchors:** existing destinations and heading slugs, plus ADR and OQ reference existence. Code-fence placeholders were excluded. No broken references found.
- **31 plain GitHub labels:** documented set equals live set; all five issue forms reference existing labels without duplicates. No label/template change was made.
- **38 ignore cases:** synthetic JSON/Parquet/NumPy and TypeScript examples remain trackable; root/nested environment files, private keys, credential/token JSON, secrets directories, media, chat dumps and weights remain ignored. Overlapping `.env.parquet`/`.env.npz` names remain protected. `git check-ignore --no-index -q -- <path>` was used without creating secrets/media.
- **Three baseline regressions:** an isolated temporary Git repository with the original PR `.gitignore` reproduced trackable fixture `.env`, `.pem` and credential JSON paths; the repaired rules ignore them. Temporary scripts/environment stayed outside the repository.
- **Whitespace:** `git diff --check` and `git diff --check dd1f5a5` passed; staged/committed checks are repeated at final handoff.
- **Scope/identity:** complete changed-file review, blank credential placeholders, private-key/token-pattern scan and project-commit metadata/trailer checks. No application, CI, dependencies, real data, source PDFs, private inspiration references or AI authorship trailers added; required disclosure/third-party acknowledgments retained.
- **Read-only remote checks:** PR head/base/body, labels, collaborators/invitations, ruleset and merge settings inspected. Head remains `c2ab5d3`, base `dd1f5a5`, PR is open and remote content/settings were not modified. Temporary admin PR-only bypass remains; no restoration was attempted.

Official endpoint/null/coverage facts, current VOD retention and Node support were checked against linked official references. Twitch legal page extraction required a direct read of the official HTML because the web reader exposed only navigation; the relevant Schedule 1/storage/player/research and download provisions were inspected. Applicability/authorization is still OQ-17, not a legal conclusion from this check.

**Unverified:** application behavior, actual tool/download performance, dataset coverage/alignment, model results, export quality, legal applicability/permissions, qualifying teammate approval and exact future Windows scaffold setup. No application tests, lint/type tooling, dataset, model or measured benchmark exists. Mermaid changes were reviewed as source; GitHub visual rendering was not exercised. Independent adversarial review of these repairs remains pending.

## Remaining choices and readiness

**Before dependent implementation:** adopt tooling/layout/storage subsets; choose application form/supported job flow; agree learning target and relevant ADR-0009 label/schema/evaluation subset; resolve OQ-9/OQ-17 before real ingestion/media use; assign collector/storage and confirm qualifying reviewer access; confirm presentation dates. A human-study rubric is needed before that study, not before synthetic app work.

**Optional/future:** neural temporal models, advanced video/speech analysis, smart crop, richer editing, longer compilations, external engagement prediction, live clipping, cloud services, tracking tools and publishing integrations. These are not prerequisites for the semester MVP.

The local branch is prepared for a **separate adversarial audit of the complete diff against the base**. This repair's own checks are not independent approval. Publication and merge remain separately authorized future steps; no remote changes belong to this repair.

## Repository handoff

The existing `docs/1-review-repository-setup` history is preserved. Repair commits append the fixture-ignore fix and the documentation/contract/workflow changes using the existing configured human identity, with no AI trailers. Final commit hashes and clean-working-tree verification are supplied in the accompanying handoff; embedding this report's own final commit hash would be self-referential. The remote PR remains at the original `c2ab5d3` head, with its body and GitHub settings unchanged. Review the full local branch against `dd1f5a5`, and the repair delta against `c2ab5d3`.
