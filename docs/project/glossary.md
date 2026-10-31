# Glossary

Terms are used with exactly these meanings across docs, code and issues.

| Term | Meaning |
|---|---|
| **VOD** | A past broadcast saved by Twitch (Helix video `type=archive`). Identified by `video_id`. |
| **Viewer clip** / **clip** | An excerpt created with Twitch's clip feature. Available `vod_offset` identifies its VOD start; `view_count` is observed popularity. Neither guarantees highlight quality. |
| **`vod_offset`** | Seconds from VOD start to the clip's start. Can be null. Stored as `vod_offset_s`. |
| **Moment** | One or more overlapping viewer clips merged into a single interval. The unit of evaluation ground truth: candidates are matched against moments. See [labeling § 2](../data/labeling.md#2-from-clips-to-moments). |
| **Window** | A fixed 10-second slice of a VOD. The unit the scorer scores. |
| **Candidate** | A clip-length interval (20–45 s by default) built from window scores and viewer clips. Candidate generation keeps a shortlist of the top 50 per VOD; the top 10 become the vertical clips and the highlights video. |
| **Moment strength** | How strong a moment's evidence is: its most-viewed clip's views relative to the channel's own clips in the same snapshot. Used as the training weight. |
| **Mature views** | Clip views read once, at ingest, from a VOD at least 7 days old, when counts have mostly settled. VODs younger than 7 days at capture are flagged and left out of view-weighted labels and metrics. |
| **Chat lag** | The delay between an on-stream event and chat's reaction (several seconds). |
| **Lead / lag features** | Features taken from later (lead) or earlier (lag) windows relative to the current one. |
| **Confident negative** | A training window at least 60 s from any viewer-clip moment; not proof that nothing good happened there. |
| **PU data** | Positive-unlabeled: we know some positives, but un-clipped does not mean negative. |
| **Baseline** | A simple reference scorer the model has to beat: random, biggest chat spike, loudest moment, and chat + loudness combined. See [evaluation § 2](../ml/evaluation.md#2-baselines). |
| **Ablation** | An experiment that removes or adds one signal family to measure its contribution. |
| **Shortlist** | The top 50 candidates per VOD (configurable). The top 10 are rendered and evaluated; the stretch Whisper features and LLM rerank work on the shortlist. |
| **Zero-clip mode** | Scoring a VOD with its viewer clips hidden from the scorer, as the app does for a VOD nobody clipped. Measures dependence on clips; it doesn't reproduce a small channel's quieter chat. |
| **Muted segment** | A span of a VOD whose audio Twitch muted (usually for copyrighted music). Audio features are unreliable there. |
| **Pseudonymization** | Replacing usernames/IDs with a salted HMAC hash at ingestion. |
| **Dataset version** | A frozen combination of VOD set, label rules, feature versions and split. See [data model](../architecture/data-model.md#dataset-versioning). |
| **Reframe** | Converting 16:9 footage to 9:16 vertical by cropping and/or stacking regions (e.g. facecam over gameplay). |
| **Clipper** | A person who finds stream highlights for short-form videos; one of the three users, alongside small streamers and viewers. |
| **Vertical clip** | A 9:16, 1080×1920 captioned MP4 of one moment, ready for TikTok, Reels or Shorts. |
| **Highlights video** | The top moments joined in stream order into one 16:9 MP4 to watch, for viewers who missed the stream. |
| **Short-form** | Vertical short video platforms: TikTok, Instagram Reels, YouTube Shorts. |
| **Helix** | Twitch's official REST API (`api.twitch.tv/helix`). |
| **ADR** | Architecture Decision Record. See [decisions](../decisions/README.md). |
