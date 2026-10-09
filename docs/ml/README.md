# ML Documentation

| Doc | What it covers |
|---|---|
| [modeling-plan.md](modeling-plan.md) | The two ML problems (v1 ranker, v2 detector), model progression, ablations, training practices |
| [features.md](features.md) | Feature catalog and naming. **The contract for feature names.** |
| [evaluation.md](evaluation.md) | Baselines, metrics, splits, confidence intervals, the online experiment design |
| [experiments.md](experiments.md) | How to run, record and report experiments |

**Quick facts**

- Labels: viewer clips → moments → window labels ([labeling](../data/labeling.md)).
- Signals: **chat + audio first**, video later as an ablation ([ADR-0004](../decisions/0004-staged-scope-chat-and-audio-first.md)).
- Splits: **grouped by broadcaster**. The test split holds channels never seen in training.
- Every result is reported next to the baselines with 95% bootstrap CIs.
- **No results exist yet.**
