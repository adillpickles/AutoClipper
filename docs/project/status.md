# Project Status

> **The living "what exists right now" document.** Every agent session reads it first. Update it **in the same PR** that changes the state. Don't mark anything done unless it's merged to `main` and tested.

**Last updated:** 2026-10-13
**Current phase:** M0 Foundations (design / pre-code)
**Next milestone:** M1 Data collection ([roadmap](../product/roadmap.md))

## Summary

The project idea, scope and plan are documented. **There is no application code, no dataset and no model yet.** No results exist.

## What exists

| Area | State | Notes |
|---|---|---|
| Documentation | Initial set in this PR | Product, architecture, data, ML, development, agent workflow, ADRs; see [setup review](setup-review.md) |
| Foundation repair | Local, not published | Owner-clarified product alignment and proposed contract repairs (ADR-0009); shared workflow (ADR-0010). Separate adversarial audit pending. |
| Repo config | Initial set in this PR | `.gitignore`, `.gitattributes`, `.editorconfig`, `.env.example`, issue/PR templates |
| GitHub settings | Configured; unchanged by repair | Plain labels; active `main` ruleset (PR + 1 approval, squash only). Temporary admin PR-only bypass was intended for this bootstrap PR but applies to admin PR merges generally while present; separately authorize merge/restoration and verify protections afterward. |
| GitHub branches / PRs | Bootstrapped | Remote `main` contains the empty initialization commit; initial documentation and review are combined on `docs/1-review-repository-setup` for one PR. |
| GitHub issues | Review tracking started | [#1](https://github.com/adillpickles/AutoClipper/issues/1); implementation backlog still needs team decisions |
| Team access / security | Partly configured | Only owner listed as collaborator; pending-invitations API returned none. Scanning/push protection enabled; vulnerability alerts and private reporting disabled (OQ-11/OQ-12). Invitations do not block this documentation PR. |
| Python scaffold | ❌ Not started | Waiting on [ADR-0002](../decisions/0002-language-and-tooling.md) ratification |
| CI | ❌ Not started | |
| Twitch developer app | ❌ Not registered | OQ-8 |
| Data collection | ❌ Not started | Prompt authorized pilot needed; OQ-9/OQ-17 apply ([collection plan](../data/collection-plan.md)) |
| Labels / features / models / eval | ❌ Not started | |
| Export | ❌ Not started | |
| Application | ❌ Not started | Form open (ADR-0007); prototype alongside analysis, integrate/polish at M5 |

## Decisions pending

See [open questions](open-questions.md). The ones that block upcoming work:

1. **Ratify the tooling and layout ADRs** (0002, 0003, 0005), which block the scaffold.
2. **Channel list and collection targets** (OQ-4), which block collection.
3. **Shared data storage and collector host** (OQ-5), which block collection at scale.
4. **First learning target and coherent dataset/evaluation contract** (OQ-1/OQ-15, ADR-0009), before claimed training/evaluation.
5. **Application form and supported flow** (ADR-0007), before interface scaffold; dates/team process remain open.
6. **Source authorization and chat retention** (OQ-9/OQ-17), before real collection. Synthetic development can proceed.

## Suggested next tasks

1. Scaffold the Python project (ADR-0002/0003) with CI.
2. Select application form; prototype review/trim/export with generated media and synthetic candidates alongside scaffold work.
3. Resolve source-specific permissions/retention and select a small pilot; register Twitch app credentials privately.
4. Implement/mock the Helix client and investigate authorized retrieval coverage and chat/media alignment.
5. Start the authorized pilot, measure cost/coverage and adopt the first target/evaluation contract before scaling.

## Known issues / caveats

- Chat replay and VOD download rely on unofficial tools ([data sources](../data/data-sources.md)).
- Empirical coverage, alignment/chat lag and clip-start bias still need a pilot. Current API/null/retention facts were checked in the repair; recheck time-sensitive references during implementation.
- [Setup review](setup-review.md) is historical; repaired proposals and remaining adoption boundaries are in ADR-0009 and [open questions](open-questions.md). Product direction is in the current PRD. No technical proposal was ratified by this repair.

## Changelog of this file

- 2026-10-09: initial documentation drafted locally and GitHub settings configured.
- 2026-10-09: setup audit; corrected publication state, simplified labels, and recorded unresolved design differences. Review changes are local pending a PR.
- 2026-10-09: owner authorized one combined documentation PR and bootstrap of empty `main`; push succeeded without disabling protection. Temporary admin PR-only bypass must be removed after the owner-approved squash merge.
- 2026-10-13: local foundation repair aligns the clarified product, reconciles proposed contracts and shared audit/publication workflow, and fixes fixture secret ignores. No application, data, CI or remote change; independent audit and publication remain separate next steps.
