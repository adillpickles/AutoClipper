# Experiments

Reproducible runs behind every reported number ([running and recording experiments](../docs/ml/experiments.md)). Each experiment has a `config.yaml`, the summary its run wrote and a `README.md` with the question, the command and the conclusion. Large outputs stay in the git-ignored `artifacts/`.

| ID | Issue | Question | Result |
|---|---|---|---|
| [exp-001](exp-001-chat-lag-alignment/README.md) | #36 | How late does chat react, and do chat and audio spikes line up with viewer clips? | Chat peaks 12 s (3–16 s) after loud moments; viewer clips' reported positions sit after the event; spikes line up with clips only weakly |
