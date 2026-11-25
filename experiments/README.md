# Experiments

Reproducible runs behind every reported number ([running and recording experiments](../docs/ml/experiments.md)). Each experiment has a `config.yaml`, the summary its run wrote and a `README.md` with the question, the command and the conclusion. Large outputs stay in the git-ignored `artifacts/`.

| ID | Issue | Question | Result |
|---|---|---|---|
| [exp-001](exp-001-chat-lag-alignment/README.md) | #36 | How late does chat react, and do chat and audio spikes line up with viewer clips? | Chat peaks 12 s (3–16 s) after loud moments; viewer clips' reported positions sit after the event; spikes line up with clips only weakly |
| [exp-002](exp-002-clip-offset-check/README.md) | #36 | Are viewer clips where Twitch reports them? (audio match of 50 clips) | No: `vod_offset` is the clip's end (+0.7 s). Corrected, chat-spike lift rises ×1.5 (1.33 to 2.00, 13 VODs); 76 % of top moments still have no spike |
| [exp-003](exp-003-clip-end-confirmation/README.md) | #36 | Does "true clip end = `vod_offset`" hold on 50 new clips, top and low-view? (criteria committed locally before the run) | **PASS**: median error 0.685 s (≤ 2 s), 80 % within 2 s (≥ 70 %) |
