# Glossary

Terms are used with exactly these meanings across docs, code and issues.

| Term | Meaning |
|---|---|
| **VOD** | A past broadcast saved by Twitch (Helix video `type=archive`). Identified by `video_id`. |
| **Viewer clip** / **clip** | A short (≤ ~60 s) excerpt of a stream created by a viewer with Twitch's clip feature. Has `vod_offset` (start within the VOD) and `view_count`. |
| **`vod_offset`** | Seconds from VOD start to the clip's start. Can be null. Stored as `vod_offset_s`. |
| **Moment** | One or more overlapping viewer clips merged into a single interval. The unit the v1 ranker ranks. See [labeling § 2](../data/labeling.md#2-from-clips-to-moments). |
| **Window** | A fixed-length slice of a VOD (proposal: 10 s). The unit the v2 detector scores. |
| **Candidate** | A moment AutoClipper proposes to the user, either from viewer clips (`source=viewer_clips`) or found by the model (`source=model`). |
| **Moment strength** | The graded relevance of a moment, e.g. number of independent clips (`n_clips`) or normalized views. |
| **Mature views** | Clip views observed at a fixed age (proposal: 7 days after the VOD). |
| **Chat lag** | The delay between an on-stream event and chat's reaction (several seconds). |
| **Lead / lag features** | Features taken from later (lead) or earlier (lag) windows relative to the current one. |
| **Confident negative** | A window far enough (proposal: ≥ 60 s) from any moment to treat as "not clip-worthy" in training. |
| **PU data** | Positive-unlabeled: we know some positives, but un-clipped does not mean negative. |
| **Baseline (B0–B4)** | Unlearned reference scorers every model must beat. See [evaluation § 2](../ml/evaluation.md#2-baselines). |
| **Ablation** | An experiment that removes or adds one signal family to measure its contribution. |
| **Zero-clip mode** | Running the detector on a VOD while hiding its clips, to simulate channels with no clips. |
| **Muted segment** | A span of a VOD whose audio Twitch muted (usually for copyrighted music). Audio features are unreliable there. |
| **Pseudonymization** | Replacing usernames/IDs with a salted HMAC hash at ingestion. |
| **Dataset version** | A frozen combination of VOD set, label rules, feature versions and split. See [data model](../architecture/data-model.md#dataset-versioning). |
| **Reframe** | Converting 16:9 footage to 9:16 vertical by cropping and/or stacking regions (e.g. facecam over gameplay). |
| **Clipper** | A person who finds stream highlights and posts them as short-form videos. AutoClipper's primary user. |
| **Short-form** | Vertical short video platforms: TikTok, Instagram Reels, YouTube Shorts. |
| **Helix** | Twitch's official REST API (`api.twitch.tv/helix`). |
| **ADR** | Architecture Decision Record. See [decisions](../decisions/README.md). |
| **Agent-ready** | An issue specified well enough for an AI agent to complete without extra context. See [writing tasks](../agents/writing-tasks.md). |
