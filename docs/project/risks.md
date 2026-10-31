# Risk Register

Reviewed at each milestone. Likelihood and impact: **H**igh / **M**edium / **L**ow.

| # | Risk | L | I | Mitigation | Trigger to act |
|---|---|---|---|---|---|
| R1 | **VODs expire before analysis or video review** | H | H | Start the pilot promptly; keep the demo/evaluation media before expiry and report unavailability separately from saved features. | Source no longer available |
| R2 | **Unofficial chat tool (TwitchDownloader) breaks** | M | H | Pin versions. Fail loudly. Download the pilot early. | Schema/parse errors in the catalog |
| R3 | **yt-dlp breaks on Twitch** | M | M | Keep it updated. Audio already downloaded is safe. Export only needs short segments. | Download failures spike |
| R5 | **Label noise** (duplicates, clip-start bias, popularity, PU data) | H | M | Moment merging, confident negatives, per-channel normalization, ranking metrics, human audit | Spot checks show > 30% bad positives |
| R6 | **Get Clips incompleteness** for large channels | M | M | Chunk by time range. Spike to measure completeness. | Spike finds missing clips |
| R7 | **Model doesn't beat simple heuristics** | M | M | It's still a finding. Report it honestly. Lean on ablations and the small-streamer simulation. | M2 baselines already very strong |
| R8 | **Scope creep** (video ML, live mode, auto-posting) | H | H | [Staged scope](../decisions/0004-staged-scope-chat-and-audio-first.md): Should/Could items (`stretch` label) wait until the MVP runs end to end. | Stretch work starts before M4 |
| R9 | **Leakage inflates results** | M | H | Saved grouped splits, train-only fitted preprocessing, versioned chat filter, cutoff snapshots and clip-free discovery evaluation; per-input offline normalization is allowed. | Suspiciously high validation scores |
| R10 | **Storage/compute exceeds the initial machine** | M | M | Audio only, compressed raw, delete WAVs after features; use shared storage or a collection worker when useful | Disk use > 70% on the collection machine |
| R11 | **Agent-generated code drifts from conventions** | M | M | AGENTS.md, contracts, review checklist, docs-gap feedback loop | Repeated review comments of the same kind |
| R12 | **Team availability** | M | M | Small issues anyone can pick up, approvals optional, weekly sync to rebalance | An issue in progress > 5 days |
| R13 | **Demo fails live** (network, Twitch outage) | M | H | Pre-processed demo VODs cached locally. Recorded fallback video. | — (always prepare) |
| R14 | **Secrets leak** | L | H | `.env` ignored, secret scanning and push protection, [SECURITY.md](../../SECURITY.md) | Any secret in a diff |
