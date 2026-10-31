# ADR-0006: License

| | |
|---|---|
| **Status** | Accepted (2026-10-31) |
| **Date** | 2026-10-09 (accepted 2026-10-31) |

## Context

The repository is public. Without a license file all rights are reserved by default, which makes the code awkward to show, reuse or build on, for example as a portfolio project.

## Decision

The code and documentation are under the **MIT License**, copyright "the AutoClipper contributors" ([`LICENSE`](../../LICENSE)).

## Options considered

| Option | Pros | Cons |
|---|---|---|
| **MIT** (chosen) | Simple, permissive, standard for student and portfolio projects | Anyone can reuse it, including commercially |
| Apache-2.0 | Permissive, with an explicit patent grant | Longer; NOTICE file process |
| AGPL-3.0 | Keeps derivatives open, even as a hosted service | Discourages reuse |
| No license | Keeps all options open | Confusing for a public repo |

## Consequences

- The package metadata declares `license = "MIT"`.
- Third-party tools we invoke (yt-dlp, TwitchDownloader, FFmpeg) keep their own licenses. We call them as external programs and don't vendor their code. A packaged download (#70) that bundles them must include their license files.
