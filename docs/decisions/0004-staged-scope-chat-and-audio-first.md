# ADR-0004: Staged scope: rank viewer clips first, chat + audio before video

| | |
|---|---|
| **Status** | Proposed (the working plan the project was pitched on; awaiting team ratification) |
| **Date** | 2026-10-09 |
| **Related** | [Roadmap](../product/roadmap.md), [Modeling plan](../ml/modeling-plan.md), [Risks](../project/risks.md) |

## Context

Early feedback from the course instructor called the idea **viable but complicated**, and raised three concerns:

1. **Multimodal is hard.** Combining text (chat), sound and video is a lot for one term.
2. **Assembling the dataset is a problem.**
3. **Windowed search over long VODs needs a lot of processing power.**

Prospective contributors raised the same worry: training on raw video is painful.

Meanwhile:

- Labels are free. The Twitch API reports every viewer clip and its offset into the VOD, so no hand-labeling is needed.
- Chat rate and audio loudness are cheap, per-second signals. Prior work on live-stream highlights found chat reactions alone to be strongly predictive.
- Audio-only downloads are small enough for laptops. Video is roughly 10× larger.

## Decision

Build in stages, each one a complete, demoable product:

1. **v1: Clip ranker.** Rank the moments viewers already clipped, using **chat + audio** features. There's no search over the full VOD, which also addresses concern 3 for v1.
2. **v2: Moment detector.** Score 10-second windows over the full VOD, still **chat + audio** only. Audio-only download plus chat logs keeps this laptop-scale.
3. **Stretch:** video/CV features (as a measured ablation), live mode, short-form performance model, online posting experiment.

Video is **required for export** (cutting and 9:16 reframing, using existing tools and detectors) but **optional for ranking**.

## Options considered

| Option | Pros | Cons |
|---|---|---|
| **Staged, chat + audio first** (chosen) | Low risk. Every stage is demoable. Directly answers the instructor's concerns. "Does video help?" becomes a measurable question. | Might leave visual-only highlights (quiet clutch plays) undetected in v1/v2 |
| Multimodal from the start | Potentially higher ceiling | High risk. Expensive data. Hard to debug. Might not finish. |
| Moment detector first | Most impressive capability | Hardest problem first, and no simpler model to compare against |

## Consequences

- **Positive:** a working product and real result early. Laptop-scale compute. A clear story for the course ("we measured what each signal adds").
- **Negative:** v1/v2 can't see purely visual moments. Video work is deferred and might not happen.
- **Follow-ups:** the roadmap is ordered accordingly. The feature catalog keeps video in a separate family for clean ablations.

## Revisit when

v2 is done ahead of schedule, or the ablations show audio + chat plateauing well below what's useful.
