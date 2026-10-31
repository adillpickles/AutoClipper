# ML Documentation

| Doc | What it covers |
|---|---|
| [modeling-plan.md](modeling-plan.md) | The one window scorer: training target, model progression, candidate generation, training practices and ablations |
| [features.md](features.md) | Feature catalog by phase, normalization and leakage filtering |
| [evaluation.md](evaluation.md) | The four baselines, hit rate@10 and the secondary metrics, splits, confidence intervals, the small-streamer simulation and the taste test |
| [experiments.md](experiments.md) | How to run, record and report experiments |

**Quick facts**

- Labels: viewer clips → moments → window labels ([labeling](../data/labeling.md)).
- Signals: **chat + audio first**, video later as an ablation ([ADR-0004](../decisions/0004-staged-scope-chat-and-audio-first.md)).
- Splits: **leave-one-streamer-out** rotation over the five pilot streamers (3 train / 1 validation / 1 test per fold; fold 0 is primary). The test streamer is never seen in training.
- Scorer: **one window scorer**. The app ships whichever of the four baselines, logistic regression and LightGBM wins on validation hit rate@10.
- Every result is reported next to the baselines with 95% bootstrap CIs.
- **No results exist yet.**
