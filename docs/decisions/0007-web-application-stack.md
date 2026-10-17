# ADR-0007: FastAPI backend + Next.js frontend

| | |
|---|---|
| **Status** | Accepted (2026-10-16, matches the team's project design doc). Hosting is deferred to OQ-18. |
| **Date** | 2026-10-09 |
| **Related** | [Architecture § Interfaces](../architecture/overview.md#47-interfaces), [PRD § Interface](../product/prd.md#interface) |

## Context

The demo needs a polished, visual flow: paste a VOD link, watch progress, browse the best moments on a timeline, preview, trim and export. The pipeline and ML are Python. Team members already know Python web APIs and TypeScript/Next.js. Viewers, streamers and editors all use the same flow, so there is one app.

## Decision

- **Web app**, not desktop.
- **Backend:** FastAPI in `src/autoclipper/api/`, wrapping the same library functions the CLI uses. Processing a VOD takes minutes, so long jobs run in a background worker, never inside the HTTP request. Pydantic models are shared with the CLI output schema.
- **Frontend:** Next.js + TypeScript (strict) + Tailwind in `web/`, talking to the API through one typed client. The highlight timeline is a custom component (canvas/SVG heat strip with markers).
- **Video preview:** serve approved retained/exported clips from the backend, or embed Twitch's player under its rules. Pick the route allowed under OQ-17 before building preview. Full VOD playback is optional.
- **Build early:** start the UI with synthetic (fake) candidates while the pipeline and model are being built. Don't wait for ML.
- **Hosting is not decided here.** The project design doc proposes Vercel for the frontend, Render/Railway for the API and worker, Supabase Postgres and an S3-compatible bucket. Until OQ-18 is decided, the app must run locally on a laptop, which is also the demo-night fallback.

## Options considered

| Option | Pros | Cons |
|---|---|---|
| **FastAPI + Next.js** (chosen) | Polished UI. Team experience. Clean separation. Same language as the ML. | Two toolchains |
| Streamlit / Gradio | Very fast to build. Python only. | Looks generic. A custom timeline is hard. Weaker demo polish. |
| FastAPI + server-rendered templates | One language | Interactive timeline is harder |
| Desktop application | Local media and processing stay together | Packaging and updates add work; team has less experience |

## Consequences

- **Positive:** the best demo quality, and reusable API endpoints.
- **Negative:** Node tooling, plus a second set of conventions to maintain.
- **Follow-ups:** frontend conventions doc and a CI job for `web/` when the frontend is scaffolded.

## Revisit when

After the first UI prototype, if integration cost is much higher than expected.
