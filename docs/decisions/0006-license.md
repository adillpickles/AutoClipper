# ADR-0006: License

| | |
|---|---|
| **Status** | Proposed. **Needs a decision from the repository owner and team.** |
| **Date** | 2026-10-09 |

## Context

The repository is public but has **no license file**, so by default all rights are reserved and others may not legally reuse the code. The team may want the code to be reusable (portfolio, open source), or may want to keep the option to commercialize it later. All contributors' agreement matters, because each contributor holds copyright in their contributions unless agreed otherwise.

## Decision (proposed)

Adopt the **MIT License** for code and documentation, with copyright held by "the AutoClipper contributors".

## Options considered

| Option | Pros | Cons |
|---|---|---|
| **MIT** (proposed) | Simple, permissive, standard for portfolio projects | Anyone, including competitors, can reuse it commercially |
| Apache-2.0 | Permissive, with an explicit patent grant | Longer. Slightly more process (NOTICE file). |
| AGPL-3.0 | Keeps derivatives open, even as a hosted service | Discourages reuse. Complicates later commercialization. |
| No license (status quo) | Keeps all options open | Not truly open source. Confusing for a public repo. |

## Consequences

- Choosing a license is a **legal, outward-facing decision**. Agents must not add a `LICENSE` file without explicit owner approval.
- Third-party tools we invoke (yt-dlp, TwitchDownloader, FFmpeg) have their own licenses. We call them as external programs and don't vendor their code. Check compatibility before vendoring anything.

## Revisit when

Before the first public demo, or before accepting contributions from outside the team.
