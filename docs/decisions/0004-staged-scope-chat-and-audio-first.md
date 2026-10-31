# Staged scope: chat + audio before video

| | |
|---|---|
| **Status** | Accepted (2026-10-16). Sets implementation order and initial signals, not a reduced semester MVP. |
| **Date** | 2026-10-09 |
| **Related** | [Roadmap](../product/roadmap.md), [Modeling plan](../ml/modeling-plan.md), [Evaluation](../ml/evaluation.md) |

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

The product is the full flow in the [PRD](../product/prd.md#scope-at-a-glance): discovery across the whole VOD (including VODs with no viewer clips), ranked vertical clips and a highlights video, previewed and exported through the app. This note sets the build order and the initial signals; delivery can evolve with the product.

Build in stages, with interface and export work proceeding alongside analysis:

1. **Chat + audio scoring.** Score every window of the VOD from chat and audio and turn the peaks into candidates. Start with the baselines, then logistic regression and LightGBM; the app ships one window scorer, whichever wins on validation. The same scorer ranks discovered candidates and any viewer-clip candidates. Window size and candidate rules are defaults tuned in experiments ([labeling](../data/labeling.md), [modeling plan § 3](../ml/modeling-plan.md#3-candidate-generation)).
2. **Polish:** pretrained audio-event and Whisper speech features and an LLM rerank, all on the shortlist of top candidates.
3. **Stretch:** video/CV features (as a measured ablation). Live mode and short-form (TikTok/Reels) performance prediction are out of scope this semester ([PRD scope](../product/prd.md#scope-at-a-glance)).

Video is **required for export** (cutting and 9:16 reframing, using existing tools and detectors) but **optional for ranking**.

## Options considered

| Option | Pros | Cons |
|---|---|---|
| **Staged, chat + audio first** (chosen) | Low initial cost; early review/export integration; video can be measured later | Can miss quiet visual highlights |
| Multimodal from the start | Potentially higher ceiling | High risk. Expensive data. Hard to debug. Might not finish. |
| Rank existing viewer clips first | Easiest start; reuses Twitch's clips | Fails on VODs with few or no clips; sorting by known views is trivial, so there's little to learn |

## Consequences

- **Positive:** a useful integration prototype early, with laptop-scale experiments and signal ablations.
- **Negative:** the chat + audio scorer can't see purely visual moments. Video work is deferred and might not happen.
- **Follow-ups:** the learning target (independent discovery/scoring) and candidate rules are in the [modeling plan](../ml/modeling-plan.md). UI and export work don't wait for favorable ML results. Keep optional signals separate for ablations.

## Revisit when

The chat + audio scorer is done ahead of schedule, or the ablations show audio + chat plateauing well below what's useful.
