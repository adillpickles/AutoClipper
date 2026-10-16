# ADR-0004: Staged scope: rank viewer clips first, chat + audio before video

| | |
|---|---|
| **Status** | Accepted (2026-10-16). Sets implementation order and initial signals, not a reduced semester MVP. |
| **Date** | 2026-10-09 |
| **Related** | [Roadmap](../product/roadmap.md), [Modeling plan](../ml/modeling-plan.md), [Risks](../project/risks.md) |

## Context

Early feedback from the course instructor called the idea **viable but complicated**, and raised three concerns:

1. **Multimodal is hard.** Combining text (chat), sound and video is a lot for one term.
2. **Assembling the dataset is a problem.**
3. **Windowed search over long VODs needs a lot of processing power.**

Prospective contributors raised the same worry: training on raw video is painful.

Meanwhile:

- Viewer clips supply weak labels with offsets into the VOD. API query limits and later-created clips mean coverage must be measured; manual auditing is still needed ([data sources](../data/data-sources.md)).
- Chat rate and audio loudness are cheap, per-second signals. Prior work on live-stream highlights found chat reactions alone to be strongly predictive.
- Audio-only downloads are small enough for laptops. Video is roughly 10× larger.

## Decision

The original design and owner's clarification require useful VOD discovery, preview and export through an appealing application, including a discovery path when viewer clips are absent. This ADR proposes implementation order and inexpensive initial signals; it does not replace those product goals. The exact strategy remains open under OQ-1/OQ-15.

Build in stages, with interface and export work proceeding alongside analysis:

1. **Optional early clip-ranking prototype.** Use available clips to exercise deduplication, review and export. Establish a useful target before training a ranker; sorting known views is a baseline.
2. **Independent discovery.** Analyze chat/audio across the VOD, either scoring windows or shortlisting candidates for learned reranking. Window size and candidate rules remain proposals. One trained scorer can serve discovery and ranking; two models are not required.
3. **Stretch:** video/CV features (as a measured ablation), online posting experiment. Live mode and short-form (TikTok/Reels) performance prediction are out of scope this semester ([PRD scope](../product/prd.md#scope-at-a-glance)).

Video is **required for export** (cutting and 9:16 reframing, using existing tools and detectors) but **optional for ranking**.

## Options considered

| Option | Pros | Cons |
|---|---|---|
| **Staged, chat + audio first** (proposed) | Low initial cost; early review/export integration; video can be measured later | Can miss quiet visual highlights |
| Multimodal from the start | Potentially higher ceiling | High risk. Expensive data. Hard to debug. Might not finish. |
| Moment detector first | Most impressive capability | Hardest problem first, and no simpler model to compare against |

## Consequences

- **Positive:** a useful integration prototype early, with laptop-scale experiments and signal ablations.
- **Negative:** v1/v2 can't see purely visual moments. Video work is deferred and might not happen.
- **Follow-ups:** agree the first learning target and discovery strategy. UI/export development and delivery gates are independent of favorable ML results. Keep optional signals separate for ablations.

## Revisit when

v2 is done ahead of schedule, or the ablations show audio + chat plateauing well below what's useful.
