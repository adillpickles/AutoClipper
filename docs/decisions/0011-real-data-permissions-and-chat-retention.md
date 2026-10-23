# ADR-0011: Real-data permissions and chat retention

| | |
|---|---|
| **Status** | Proposed. A decision brief for #7: every recommendation here awaits team approval. **Nothing in this ADR authorizes collecting, keeping, showing or exporting real Twitch data.** OQ-9 and OQ-17 stay open until the team accepts a version of it. |
| **Date** | 2026-10-23 |
| **Deciders** | Team (OQ-9); repo owner (@adillpickles) and team (OQ-17), per [open questions](../project/open-questions.md). Course staff advise; only Twitch and rights holders can grant permissions. |
| **Related** | #7 · OQ-9, OQ-17, OQ-8 (#4), OQ-5, OQ-18 · #8, #17–#20, #23, #24, #31, #32, #34, #35, #37, #40, #46 · [data governance](../data/data-governance.md) · [data sources](../data/data-sources.md) · [API contract § 5](../architecture/api-contract.md) · ADR-0004, ADR-0007, ADR-0009 |

> **Not legal advice.** This summarizes what the primary sources say about our planned uses, so the team can decide and know what to ask. Where a source is ambiguous, this ADR says so instead of guessing. Re-read the sources before relying on them: Twitch can change them at any time ([Developer Agreement § VIII](https://legal.twitch.com/en/legal/developer-agreement/)).

## Context

AutoClipper plans to use real Twitch data for four things ([PRD](../product/prd.md)): viewer clips as noisy labels, chat activity and audio loudness as signals over the whole VOD, and video for preview, trimming and 9:16 export. Today it only uses synthetic data. On `main`, the Helix client (app token, Get Users, Get Videos) and the chat replay parser (in memory, pseudonymized, raw file deleted after parsing) exist but have never run against real Twitch. Audio download (#19, PR #59) and vertical export (#20, PR #60) are unmerged drafts.

#7 needs, for each source, the allowed use, storage location, retention and deletion, sharing and evidence; a preview/display route; and an OQ-9 answer for chat. This ADR prepares those answers. It doesn't make them.

### Sources read (2026-10-23)

| Source | Version | Used for |
|---|---|---|
| [Twitch Developer Services Agreement](https://legal.twitch.com/en/legal/developer-agreement/) ("DA") | Last modified 12/04/2024 | Sections IV, VI, VII, XI, XIII and Schedule 1 (§§ A–D) |
| [Twitch Terms of Service](https://legal.twitch.com/en/legal/terms-of-service/) ("ToS") | Last modified 08/12/2026 | §§ 7 (License), 8 (User Content), 10 (Prohibited Conduct) |
| [Twitch API reference](https://dev.twitch.tv/docs/api/reference) | As published | Get Videos, Get Clips, Get Clips Download |
| [Embedding Twitch](https://dev.twitch.tv/docs/embed) and [Embedding video and clips](https://dev.twitch.tv/docs/embed/video-and-clips) | As published | Player requirements and API |
| [Data sources § VOD retention](../data/data-sources.md#vod-retention) | Checked 2026-10-13 | VOD lifetimes (7/14/60 days) |

## What the sources say

### Key provisions

| Provision | What it says | Why it matters here |
|---|---|---|
| DA § VI.A–B (Data Policy) | "Twitch Data", which includes data from the Twitch API "including any insights derived from that data", "may not be used for purposes other than" five listed purposes, starting with "(a) creating compelling benefits that improve the end user experience". Purpose (e) needs end-user permission **and** "Twitch provides prior written permission". | Training and evaluating a model on API data isn't a listed purpose in plain terms. Whether (a) covers it is unclear. |
| DA § VI.C | Twitch Data "may not be sold, licensed, monetized, distributed for commercial purposes, made available to, or otherwise shared with, any affiliates or third parties for any purpose without Twitch's prior written permission." | Sharing a dataset outside the Developer needs permission. Whether teammates are "third parties" depends on who the Developer is (see OQ-8). |
| DA § VI.F, Schedule 1 § C | Delete Twitch Data "upon termination of this Agreement, revocation, or reduction in scope of end user authorization, or upon Twitch's or the end user's request". | Every source needs a working deletion path. |
| DA § VII.C | Give "a clear path to the source from displays" and attribute Twitch Content. | The app links each moment back to its VOD on Twitch. |
| DA § IV.C | Don't exceed or circumvent rate limits. | Already followed by `HelixClient` ([data sources § 1](../data/data-sources.md#1-twitch-helix-api-official)). |
| DA § XI.I | Without Twitch's prior written permission, you "will only access Program Materials documented on the Twitch Developer Site". | TwitchDownloader relies on "Twitch's undocumented internal API" ([data sources § 2](../data/data-sources.md#2-chat-replay-twitchdownloader-unofficial)). yt-dlp isn't a documented Twitch API either. |
| DA § XIII.B | With access to end users' personal data: give privacy notices, get necessary consent, help Twitch with deletion and access requests. Don't re-identify data Twitch provides as anonymous. | Chat contains user IDs and names. |
| Schedule 1 § A | Request only the data needed for current features. Publish a privacy policy or notice saying what you collect, use, store and retain. | The text sets no threshold. Whether a local course demo needs a published notice is unclear; a hosted app (OQ-18) would have end users. |
| Schedule 1 § B | Twitch "will consider authorizing" some uses "only after entering into a separate agreement and running through an approval process", including "Research - educational and non-commercial projects that examine trends over specific time periods." | The only research or educational language in either document. Neither contains an educational or academic exemption. |
| Schedule 1 § C | "Do not store copies of Twitch Content or Program Materials, unless you: (a) obtain prior written authorization from Twitch (through these terms or otherwise); (b) control the rights associated with such content; or (c) cache such information for only a twenty-four hour time period without further sharing it with third parties." Also: "Re-syndication and re-distribution of Program Materials or data as available from a Twitch API is prohibited", and delete content "when Twitch reports them as deleted or expired". | The storage rule and its three exceptions, including the 24-hour cache ([below](#specific-questions-7-raises)). Its text covers "Twitch Content or Program Materials"; Schedule 1 applies when you use the Twitch APIs. |
| Schedule 1 § D.1 | "You must adopt and integrate our embeddable player for all Twitch video content. You may not use any other player or system to display Twitch video content without Twitch's prior written permission." Don't obscure or block player ads or Twitch marks. Embeds are "not an express or implied license to the content from Twitch." | Preview through Twitch's player is the documented route. Serving our own copy of Twitch video needs written permission. |
| Schedule 1 § D.2 | "You must only retain chat logs as long as necessary for the operation of Your Services or to improve Your Services; do not do so for the purpose of creating public databases or websites, or, in general, to collect information about Twitch's end users. You must enable, and process, all requests by end users to block, discontinue, delete, or otherwise opt-out of any retention of chat logs". | Applies to chat we receive through Twitch APIs. Requires a deletion and opt-out process. |
| ToS § 7 | The license is "for your personal use or internal business use only". It does not permit "(d) use of any data mining, robots, or similar data gathering or extraction methods" or "(e) downloading (except page caching) of any portion of the Twitch Services, the Materials, or any information contained in them, except as expressly permitted on the Twitch Services". | Downloading VOD audio, video or chat through unofficial tools appears to fall here. § 7's "Materials" excludes User Content, but (e) also names "any portion of the Twitch Services". |
| ToS § 8 | Users who post User Content (streams, chat) license it to Twitch, and to other users only "solely as necessary to enable" Twitch's sharing features. They warrant they have the consents of third parties in their content (§ 8(a)(iii)). The DA adds that "Twitch does not own all Twitch Content" (Sch. 1 § D.1). | A streamer's permission covers the streamer's own rights only: not chatters' messages, music, game footage or guests. |
| ToS § 10 (vi), (xi) | Don't circumvent "features that prevent or restrict use or copying of any content", or access Twitch "by means of any robot, spider, scraper, crawler, or other automated means for any purpose". | Unofficial download tools are automated access. |
| [Get Clips Download](https://dev.twitch.tv/docs/api/reference/#get-clips-download) | "Requires an app access token or user access token that includes the editor:manage:clips or channel:manage:clips scope". `editor_id` "must match the user_id in the user access token"; `403` when "The user is not an editor for the specified broadcaster". Up to 10 clips per call, 100 requests/minute; links "are temporary". | An official media route, for existing clips only. The parameters point to a token authorized by the broadcaster or one of their editors. How an app access token meets the `editor_id` rule isn't explained, so confirm before building. |
| [Embed docs](https://dev.twitch.tv/docs/embed) | The embedding domain needs SSL and a `parent` parameter. Player not obscured, at least 400×300. The VOD player takes a start `time` and has `seek`, `getCurrentTime` and `pause`. The clip embed has no JavaScript API. Twitch may revoke embed use. | Preview can start at a moment and pause at its end. No hard end-cut parameter is documented. |

### Classification

| | Activities |
|---|---|
| **1. Explicitly permitted** (within the DA's other rules) | Calling documented Helix endpoints (Get Users, Get Videos, Get Clips) with an app access token, within rate limits. Caching their results "for only a twenty-four hour time period without further sharing" (Sch. 1 § C(c)), while keeping § C's update and deletion duties. Showing Twitch VODs and clips through Twitch's embeddable player, unobscured, with ads and marks intact, on an HTTPS domain (Sch. 1 § D.1, embed docs). |
| **2. Needs authorization or controlled rights** | Storing API data or Twitch Content longer than 24 hours (Sch. 1 § C(a)/(b)). Research use under a separate agreement and approval (Sch. 1 § B). Sharing Twitch Data with third parties (DA § VI.C). Using undocumented Twitch APIs (DA § XI.I). Downloading content other than page caching (ToS § 7(e)). Displaying Twitch video in any player other than Twitch's (Sch. 1 § D.1). Downloading clip files (Get Clips Download's scopes and `editor_id` rule). |
| **3. Prohibited** under the standard terms (a separate agreement with Twitch can change some) | Re-syndicating or redistributing API data (Sch. 1 § C). Selling or distributing metadata or social content, "absent a separate agreement with Twitch" (Sch. 1 § B). Keeping chat logs to build public databases or "to collect information about Twitch's end users" (Sch. 1 § D.2). Obscuring or blocking player ads or Twitch marks (Sch. 1 § D.1). Accessing Twitch by "robot, spider, scraper, crawler, or other automated means" (ToS § 10 (xi)). Circumventing copy restrictions (ToS § 10 (vi)). Exceeding rate limits (DA § IV.C). |
| **4. Unclear: needs confirmation** | Whether model training on API data is a permitted purpose under DA § VI.B(a), or needs research approval (Sch. 1 § B). Whether derived features and labels count as stored "copies" under Sch. 1 § C, or only as "insights" under § VI. Whether Sch. 1 § D.2's "as long as necessary" limit on chat logs is itself an authorization "through these terms" under § C(a), or the 24-hour cache still applies to them. How an app access token can meet Get Clips Download's `editor_id` rule. Whether a local course demo needs a published privacy notice (Sch. 1 § A). Whether teammates are "third parties" (depends on who registers the app, OQ-8). Whether creator-provided recordings downloaded from Twitch are still "Twitch video content" under Sch. 1 § D.1. Whether processing a stream transiently, without saving, differs from "downloading" under ToS § 7(e). Whether a creator's written permission counts as "control the rights" under Sch. 1 § C(b). |

### Specific questions #7 raises

- **24-hour caching.** Schedule 1 § C is a restriction with three exceptions, not a ban on all storage. The restriction: don't store copies of Twitch Content or Program Materials. The exceptions: (a) prior written authorization from Twitch, "through these terms or otherwise"; (b) controlling the rights in the content; (c) caching for at most 24 hours without sharing with third parties.
  - Only (c) needs nothing more from anyone. Within (c) the rest of § C still applies: update cached results, delete what Twitch reports as deleted or expired, and don't re-syndicate or redistribute. None of the three exceptions lifts that last rule; for designs that break § C, it points to Twitch's separate commercial arrangements instead.
  - (a) and (b) aren't prohibited. They need evidence we don't have yet.
  - For our plans: Get Clips results kept for the planned day-1 and day-7 snapshots ([collection plan](../data/collection-plan.md)), or a stored dataset, go past 24 hours, so they need (a) or (b). On-demand analysis that refetches what it needs and discards it within 24 hours fits (c).
  - Unclear: whether "through these terms" covers anything we'd do, e.g. chat logs kept "as long as necessary" under § D.2.
- **Educational research.** Neither document has an educational or academic exemption. Schedule 1 § B lists research ("educational and non-commercial projects that examine trends over specific time periods") among uses that need a separate agreement and approval. Course approval, low volume and public availability don't replace that ([governance § OQ-17](../data/data-governance.md#authorization-boundary-before-real-collection-oq-17)).
- **Chat deletion requests.** Schedule 1 § C, § D.2 and DA § VI.F require us to delete a user's data and chat-log retention when they ask, and to delete content Twitch reports as deleted or expired. So we need a published contact point and a way to find a person's data. Per-message data keyed by a salted hash can only be found again by hashing the requester's user ID with the same salt.
- **Creator permission isn't enough on its own.** A streamer can license what they own (ToS § 8). They can't license chatters' messages, music, game footage or guests, and they can't waive our obligations under the Developer Agreement for data we get through Twitch's APIs. Schedule 1 § D.1 also says embeds aren't a content license. Creator-provided media needs the creator's written terms **and** a check of any third-party material in it.

## Permission matrix by source

"Retention" below is a request to make or a team proposal, **not** a period the sources allow, unless the cell cites the source.

### 1. Twitch Helix metadata and clips

| | |
|---|---|
| **Intended use** | Resolve channels; list VODs (duration, muted segments); list viewer clips with `vod_offset` and view counts as candidates and weak labels ([labeling](../data/labeling.md)). |
| **Collection and auth** | Documented endpoints Get Users, Get Videos and Get Clips, with an app access token from the client-credentials flow (`HelixClient`; Get Clips is #17). Needs the app from #4. |
| **Required permissions and evidence** | Developer Agreement accepted when the app is registered (#4). Within 24 hours: none beyond the DA. Longer storage: Twitch's written authorization (Sch. 1 § C(a)), possibly as research approval (Sch. 1 § B). **Evidence on file: none.** |
| **Storage permitted?** | Up to 24 hours, unshared: yes (Sch. 1 § C(c)). Longer: only with authorization. |
| **Proposed storage location** | Collector machine, `data/raw/` and `data/interim/` (git-ignored). |
| **Proposed maximum retention** | 24 hours without authorization. With authorization: what the authorization says. Proposal to request: until the project ends (Dec 11, 2026), deletion deadline TBD. |
| **Deletion obligations** | When Twitch reports a VOD or clip deleted or expired (Sch. 1 § C). On a user's or Twitch's request, or when the DA ends (DA §§ VI.F, IX). |
| **Sharing / redistribution** | No re-syndication or redistribution (Sch. 1 § C). No third parties without written permission (DA § VI.C). Within the team: depends on OQ-8. |
| **Display / export** | Show metadata in the app with a link to the VOD on Twitch and attribution (DA § VII.C). |
| **Unresolved** | Model training as a purpose (§ VI.B). Whether stored labels and features count as copies. Whether day-7 snapshots are possible without authorization. Teammates as third parties. |
| **If not permitted** | On-demand analysis only, cached for at most 24 hours. No stored real training corpus; models train on synthetic or otherwise authorized data, or the project reports baselines only. |

### 2. Chat replay

| | |
|---|---|
| **Intended use** | Per-window chat features: message rate, unique chatters, emotes, bits ([features](../ml/features.md)). |
| **Collection and auth** | Today: TwitchDownloaderCLI 1.56.5, which relies on Twitch's undocumented internal API ([data sources § 2](../data/data-sources.md#2-chat-replay-twitchdownloader-unofficial)). The API reference has no endpoint for a past VOD's chat (checked 2026-10-23). Documented alternative: live chat through IRC or EventSub, recorded during the broadcast ([data sources § 4](../data/data-sources.md#4-live-chat-official-twitch-chat-backup-and-live-mode)). |
| **Required permissions and evidence** | Replay: Twitch's prior written permission (DA § XI.I; ToS §§ 7(d)–(e), 10 (xi)). Live chat: the DA, plus Schedule 1 §§ C and D.2. Neither streamer nor course staff can grant Twitch's permission. **Evidence on file: none.** |
| **Storage permitted?** | Replay: no basis found without permission. Live chat: Sch. 1 § D.2 limits chat-log retention to "as long as necessary for the operation of Your Services or to improve Your Services". Whether that limit is itself an authorization under § C(a), or the 24-hour cache still applies, is unclear. |
| **Proposed storage location** | Raw file only on the collecting machine (`data/raw/chat/`), deleted after a successful parse, which `autoclipper.ingest.chat` already does. Aggregates in `data/interim/`. |
| **Proposed maximum retention** | Raw: deleted right after parsing (implemented). Messages: none, if aggregate-only is adopted. Aggregates: the source's limit, then the OQ-9 deadline. |
| **Deletion obligations** | Process users' requests to delete or opt out of chat-log retention (Sch. 1 § D.2, § C; DA § VI.F). Delete when Twitch reports the VOD deleted or expired (§ C). |
| **Sharing / redistribution** | Never raw chat. No public chat database (§ D.2). Aggregates only, subject to DA § VI.C. |
| **Display / export** | Aggregates only, in the app, reports and slides. Never chat logs with identities ([governance § 2](../data/data-governance.md#2-privacy-chat-data)). |
| **Unresolved** | Would Twitch grant replay access? Is live capture feasible for the pilot (it needs the channels chosen before their streams, OQ-4)? What consent does DA § XIII.B require for public chat? |
| **If not permitted** | Live chat logger for future streams of chosen channels, or no chat signals (audio and clips only). The parser (#18) stays useful; its input changes. ADR-0004 ("chat + audio before video") may need revisiting. |

### 3. Twitch VOD audio downloads

| | |
|---|---|
| **Intended use** | Loudness and other audio features over the whole VOD ([features](../ml/features.md)). |
| **Collection and auth** | yt-dlp, audio only (unmerged draft, #19 / PR #59). Not a documented Twitch API. |
| **Required permissions and evidence** | Twitch's written permission for downloading and storage (ToS § 7(e); Sch. 1 § C(a); DA § XI.I), or media whose rights we control (Sch. 1 § C(b)), e.g. creator-provided (source 6). **Evidence on file: none.** |
| **Storage permitted?** | From Twitch: no basis found without permission. |
| **Proposed storage location** | If authorized: collector machine, `data/raw/audio/`, compressed; WAV only while extracting. |
| **Proposed maximum retention** | None without authorization. With it: what the authorization says. Proposal to request: until features are extracted and validated ([governance § 4](../data/data-governance.md#4-data-retention-proposal)). |
| **Deletion obligations** | When Twitch reports the VOD deleted or expired (Sch. 1 § C). On the rights holder's request. |
| **Sharing / redistribution** | None. |
| **Display / export** | None: analysis only. |
| **Unresolved** | Is transient processing without saving still "downloading" (ToS § 7(e))? Does a creator's permission plus their own copy cover music and other third-party audio? |
| **If not permitted** | Audio features only from creator-provided recordings, or no audio features for real VODs. Affects #19, #24, #34 and #35. |

### 4. Video segments for preview and export

| | |
|---|---|
| **Intended use** | Preview a moment, trim it, export a 9:16 captioned MP4 ([architecture § Export](../architecture/overview.md)). |
| **Collection and auth** | Preview: Twitch's embedded player, no download. Export: needs a video file. Today that would be yt-dlp segments (#20 / PR #60), which aren't a documented Twitch API. |
| **Required permissions and evidence** | Preview through the embed: the DA and embed rules only (Sch. 1 § D.1). Export, or showing a copy in our own player: Twitch's written permission (Sch. 1 §§ C, D.1; ToS § 7), or controlled rights, plus third-party rights in the footage. **Evidence on file: none.** |
| **Storage permitted?** | Embed: nothing is stored. Copies of Twitch video: not without permission. |
| **Proposed storage location** | If authorized: `artifacts/exports/` for exports; a small authorized demo set as agreed under OQ-5. |
| **Proposed maximum retention** | Embed: n/a. Copies: what the authorization says. Proposal to request: delete exports after the demo unless published with permission ([governance § 4](../data/data-governance.md#4-data-retention-proposal)). |
| **Deletion obligations** | Embed: nothing to delete; it can only play a VOD that's still on Twitch. Copies: delete on Twitch's report of deletion or expiry, or on request (Sch. 1 § C). |
| **Sharing / redistribution** | Embed: Twitch's player handles playback. Copies: no redistribution; publishing needs the creator's permission and the platform's ([governance § 3](../data/data-governance.md#publishing-clips)). |
| **Display / export** | Embed: VOD player starting at `time`, paused at the moment's end with `getCurrentTime`/`pause`. Ads and marks stay visible; at least 400×300; HTTPS domain with `parent`. Export: none from the embed. |
| **Unresolved** | Does the embed work on `localhost` during development? (Not documented; to test.) Is pausing at the end good enough as a trim preview? |
| **If not permitted** | Preview through the embed; export only from creator-provided media or authorized clip downloads; otherwise the app returns `not_permitted` for export ([API contract § 5](../architecture/api-contract.md)). The export code (#20, #21) stays usable for synthetic and authorized media. |

### 5. Official Twitch clip downloads

| | |
|---|---|
| **Intended use** | Video of existing viewer clips for preview, export and human evaluation. |
| **Collection and auth** | [Get Clips Download](https://dev.twitch.tv/docs/api/reference/#get-clips-download) with a token carrying `editor:manage:clips` or `channel:manage:clips`, where `editor_id` matches the token's user. In practice that points to the broadcaster or one of their editors authorizing our app through a user OAuth flow, which AutoClipper doesn't have (to confirm). Links are temporary. |
| **Required permissions and evidence** | The broadcaster's or editor's authorization of our app, plus written terms for keeping, editing and exporting. Third-party rights in the clip. **Evidence on file: none.** |
| **Storage permitted?** | Unclear. The endpoint provides files to authorized callers. Whether that authorizes storage under Sch. 1 § C isn't stated. |
| **Proposed storage location** | If agreed: same as authorized exports. |
| **Proposed maximum retention** | As the broadcaster's written terms say, never past the project end; TBD. |
| **Deletion obligations** | On the broadcaster's or Twitch's request; when the clip is deleted (Sch. 1 § C). |
| **Sharing / redistribution** | Only as the broadcaster's terms allow; no redistribution of API data (Sch. 1 § C). |
| **Display / export** | Export within the clip's own interval only; it has no surrounding footage. Preview can use the clip embed (no JavaScript API, no start time). |
| **Unresolved** | Does the endpoint's authorization cover editing and re-export? Do clip creators (viewers) have rights in the clip? Can an app access token meet the `editor_id` rule? |
| **If not permitted** | Clip embeds for preview; no clip-based export. |

### 6. Creator-provided media

| | |
|---|---|
| **Intended use** | Recordings a consenting creator gives us, for analysis, preview, export and the demo. |
| **Collection and auth** | The creator sends files they hold rights in (e.g. a local recording), privately. A written agreement records the scope, retention, sharing and deletion. |
| **Required permissions and evidence** | The creator's written permission, stored privately and referenced from this ADR when accepted. Check third-party material (music, game footage, guests, on-screen chat). If the file came from Twitch, Twitch's terms for that copy still apply. **Evidence on file: none.** |
| **Storage permitted?** | As the creator's terms allow, within any platform limits that apply. |
| **Proposed storage location** | Shared team storage (OQ-5), access-limited, never git. |
| **Proposed maximum retention** | As the creator's terms say; proposal: no later than project end, with a dated deletion. |
| **Deletion obligations** | On the creator's request or at the agreed date. |
| **Sharing / redistribution** | Within the team only, unless the creator agrees otherwise in writing. |
| **Display / export** | Our own player and FFmpeg export. Whether Sch. 1 § D.1 still requires Twitch's player for a creator's own copy of a Twitch broadcast is unclear. |
| **Unresolved** | Does a creator's permission count as "control the rights" (Sch. 1 § C(b))? Which creators would agree? Who stores the signed permission? |
| **If not permitted / nobody agrees** | Synthetic media for export demos; preview of real VODs through the embed only. |

## OQ-9: chat retention options

Both options start from the existing parser: `autoclipper.ingest.chat` parses replay JSON in memory, replaces commenter IDs with `HMAC-SHA256(salt, user_id)` (`autoclipper.privacy.pseudonymize`), redacts `@mentions`, logins, display names and gift-sub names, and deletes the raw file after a successful parse. Its docstring lists what the redaction misses (names typed without `@` by people who don't chat in that VOD, names glued to other letters).

| | **A. Aggregate-only** (proposed default, R1) | **B. Sanitized messages** |
|---|---|---|
| **What's stored** | Per-window aggregates only, e.g. message count and rate, unique chatters, emote counts, sub/mod shares, bits (`features_chat`, [data model](../architecture/data-model.md)). | `chat_messages` rows per VOD: `video_id`, `offset_s`, `user_hash`, redacted `text`, `emote_ids`, `n_emotes`, `is_subscriber`, `is_moderator`, `bits`, `source`. |
| **Hash linkage scope** | One parsing run of one VOD, in memory. No hash is written to disk. | Every stored VOD that shares a salt. A per-VOD salt would limit linkage to one VOD. |
| **Mention redaction** | Applied in memory (implemented). Text isn't kept, so redaction is extra protection. | Required, and must be reviewed: text stays potentially identifying (parser docstring). |
| **User linkability** | Low. Small windows can show that *someone* chatted, not who. | Higher. Stable hashes allow cross-VOD profiles, which [governance § 2](../data/data-governance.md#2-privacy-chat-data) forbids, and text can identify people. |
| **Deletion requests** | Nothing per user remains after parsing. During the short raw window, delete the raw file. Whether aggregates must be recomputed is unclear. | Hash the requester's user ID with the stored salt and rewrite the partitions. The salt must stay available and secret for as long as data is kept. |
| **Fit with Sch. 1 § D.2** | Keeps no chat logs past parsing. | Keeps chat logs, which must be "necessary" and never a database "to collect information about Twitch's end users". |
| **Data model** | No schema change: `chat_messages` stays a transient parsing representation (as documented). | `chat_messages` becomes stored data: a data-model change that needs this ADR to say so (AGENTS.md rule 4), plus a deletion procedure. |
| **Cost** | Adding a feature later means downloading the chat again, which is only possible while the VOD exists and if the source is authorized. Text features must be chosen before collection (#11). | More storage and privacy work; features can be recomputed. |
| **Deletion deadline** | Aggregates: the source's limit, then project end (date TBD). | Proposal if chosen: a fixed date after the feature freeze (TBD), and never past the source's limit. |

## Preview and export options

| | **Twitch player embed** (proposed default for preview, R2) | **Serve retained video segments** | **Creator-provided media** |
|---|---|---|---|
| **Legal basis** | Explicitly provided (Sch. 1 § D.1), subject to the embed rules. The page must offer "additional content or data of substantial value"; our shortlist and timeline are meant to. | Needs Twitch's written permission to store and to use another player (Sch. 1 §§ C, D.1), plus third-party rights. | The creator's written terms, plus third-party rights. Whether Sch. 1 § D.1 still applies is unclear. |
| **Technical** | Embed the VOD at the moment's start; pause at its end with `getCurrentTime`/`pause`. Ads may play. Needs HTTPS and `parent`; at least 400×300; not obscured. | Download segments, then serve them as `video_file` from the backend. | Same as served media, from files the creator sends. |
| **Architecture** | A new `CandidateMedia` kind for the Twitch player, added by #46 as a minor API change ([API contract § 5](../architecture/api-contract.md), decision D6). Not changed here. | Fits the current `video_file` kind. | Fits `video_file`. |
| **Storage** | None. | Video files under authorized retention. | Files under the creator's terms. |
| **Availability** | Only while the VOD is public and unexpired (7/14/60 days, [data sources](../data/data-sources.md#vod-retention)). | While the authorization allows. | While the creator's terms allow. |
| **Export** | Not possible. | Possible if authorized. | Possible if the terms allow. |

## Proposed decision (recommendations awaiting team approval)

These are the proposals put forward for #7. **None of them is accepted.** Until the team accepts a version of this ADR, OQ-9 and OQ-17 stay open and real data collection stays blocked.

1. **R1 (proposed). Aggregate-only chat retention (OQ-9, option A).** Keep per-window chat aggregates only. Hash user IDs only in memory within one VOD's parsing run. Keep the existing redaction, and delete raw files right after parsing. This settles retention only; it doesn't authorize *getting* chat replay (source 2).
2. **R2 (proposed). Official Twitch player embeds for preview.** Preview real VODs only through Twitch's embedded player, under Schedule 1 § D.1 and the embed rules.
3. **R3 (proposed). No VOD audio downloading or export from Twitch** unless the necessary permissions and rights have been verified and recorded in this ADR for that source. Until then: synthetic media, and creator-provided media under written terms with third-party material checked.
4. **R4 (proposed). Ask before storing (follows from the evidence; not in the original proposal list).** Don't build a stored real-data corpus, including clip snapshots kept past 24 hours, until written authorization (Sch. 1 §§ B, C(a)) or controlled rights are recorded. Until then: synthetic development, and on-demand Helix use within the 24-hour cache.

### Answering #7's check question

*"may I download and keep this VOD's audio for two weeks?"* Under these proposals: **no**, not from Twitch, unless this ADR records Twitch's written permission (or our control of the rights) for that source with a retention period of at least two weeks. Audio from a creator-provided recording may be kept as long as the creator's written terms say.

## Options considered

| Option | Pros | Cons |
|---|---|---|
| **A. Authorized-data strategy** (proposed: R1–R4) | Matches what the sources explicitly allow. Synthetic work continues. Real data arrives as permissions do. | The real corpus may be small or late. Labels and models may stay synthetic or baseline-only. |
| B. Unofficial tools at low volume, as the current governance doc's "proposed operating limits" describe | Fast and large-scale. Existing code works. | Appears to conflict with the text of ToS §§ 7 and 10 and DA § XI.I. Public availability isn't permission. |
| C. Creator partnerships only | Clear rights for media. Export possible. | Few creators; Helix and chat data still fall under the DA; third-party rights remain. |
| D. Synthetic data for the whole semester | No permission risk. | No real evaluation; weak demo and results. |

## Questions for decision

### For the owner and team (OQ-9, OQ-17)

1. Adopt R1–R4 as written, adjust them, or choose other options?
2. Ask Twitch for research approval or written storage authorization (Sch. 1 §§ B, C(a))? Who writes the request, and which uses, data, volume and deletion terms does it cover?
3. Who is the "Developer": one person or the team? This affects whether teammates count as "third parties" under DA § VI.C. Decide together with OQ-8 (#4).
4. Replace chat replay with live chat for future streams (needs channels chosen ahead, OQ-4), or go without chat signals for real VODs?
5. Approach creators for recordings and written permission? Who leads it, and what does the permission template include (scope, retention, sharing, deletion, third-party material)?
6. Where is a deletion request received (a published contact), and who handles it?
7. Is pausing at the end of the moment acceptable as the trim preview? Is export of real VODs out of scope until authorized?
8. Which deletion dates apply at project end (Dec 11, 2026, or later for grading)?

### For course staff (guidance on course policy, not platform permission)

1. Does the course require research-ethics review (e.g. IRB or human-subjects review) for chat data from public streams?
2. Is a demo using synthetic media plus Twitch embeds, and creator-provided media if any, acceptable for the course?
3. Does the program have any existing agreement with Twitch, or a process for requesting one?
4. Is the team expected to stop at what Twitch's documents allow, even where enforcement seems unlikely? (This ADR assumes yes, per [governance § OQ-17](../data/data-governance.md#authorization-boundary-before-real-collection-oq-17).)

Course staff can say what the course requires or accepts. They can't grant permissions that only Twitch or rights holders can give.

### Only Twitch or rights holders can grant

- **Twitch:** written authorization to store API data or Twitch Content beyond 24 hours (Sch. 1 § C(a)). A research agreement and approval (Sch. 1 § B). Permission to access undocumented APIs (DA § XI.I) or to download content (ToS § 7). Permission to display Twitch video in another player (Sch. 1 § D.1). Permission to share Twitch Data with third parties (DA § VI.C).
- **Streamers:** written permission for their recordings, clips and exports, including through Get Clips Download.
- **Other rights holders** (music, game publishers, guests): rights the streamer can't give. Where they can't be cleared, choose content without that material.

## Consequences

- **Positive:** a clear, cited line between what's allowed, what needs permission and what's prohibited. The app and export work continues on synthetic data and Twitch embeds. Chat privacy work stays small, because aggregate-only needs no schema change and matches the parser's in-memory design.
- **Negative:** real collection (#34, #35, #37, #40) likely waits on outside permission and may never reach the planned scale. Chat replay and audio, the two v1 signals in ADR-0004, may be unavailable for real VODs. Export of real VODs may stay disabled.
- **Follow-ups** (after the team decides; none done here):
  - **Docs:** update [data governance](../data/data-governance.md) §§ 2–4 (the retention table becomes binding) and [data sources](../data/data-sources.md) per source. Move OQ-9 and OQ-17 to Resolved in [open questions](../project/open-questions.md). Update [status](../project/status.md).
  - **Issues:** remove `needs-decision` from #8, the only issue waiting on #7 alone. Re-check #34 and #46, which also wait on other decisions. Update #19, #20, #23, #24, #31, #32, #35, #37 and #40 if a source is ruled out.
  - **Other decisions:** #13/#46 decide the Twitch-player media kind (API contract D6). OQ-18 hosting needs an HTTPS domain for embeds and a privacy notice (Sch. 1 § A). ADR-0004 needs revisiting if chat or audio is unavailable.

## Revisit when

- Twitch answers a request for authorization, or changes the Developer Agreement or Terms of Service (check the "last modified" dates above).
- Course staff give guidance that changes an assumption here.
- A creator agrees to provide media, or a source is ruled out.
