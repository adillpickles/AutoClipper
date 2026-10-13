# ADR-0010: Shared agent review and publication workflow

| | |
|---|---|
| **Status** | Accepted by the owner's explicit 2026-10-13 repair instruction for repository development practice. Technical/product proposals remain unratified. |
| **Date** | 2026-10-13 |
| **Related** | [AGENTS.md §7](../../AGENTS.md#7-how-to-do-a-task-the-standard-loop), [Agent guidance](../agents/README.md), [Git workflow](../development/git-workflow.md), [ADR-0008](0008-trunk-based-development-squash-merge.md) |

## Context

Shared instructions directed agents to publish, while tool-specific guidance prohibited it without a request. The substantial-task independent audit/repair loop was missing. A four-student team needs one general practice across environments, with enough autonomy for routine work and explicit publication boundaries.

## Decision

Use the canonical loop in AGENTS.md: planning → implementation/validation → independent audit → independently verify findings and repair → re-audit as necessary → authorized push/PR → required human review → separately authorized merge. Substantial/ambiguous plans need appropriate approval unless already authorized; routine well-specified tasks need no repeated approval. Review actual complete work against the correct base, with evidence for defect dispositions. An unavailable independent reviewer is an explicit handoff limitation, not invented approval.

Local task authorization does not authorize external comments, pushing or PR writes. Merge requires separate approval. Preserve configured human authorship and truthful AI-assistance disclosure. CLAUDE.md remains a thin entry point; detailed review guidance expands the canonical loop without overriding it.

## Consequences

ADR-0008's normal one-human-approval and squash policy is unchanged. Independent agent audit supplements human review. No reviewer service, CI, GitHub settings or permissions are introduced by this decision. The owner-authorized bootstrap exception remains historical and requires separately authorized restoration; this repair does not extend it.
