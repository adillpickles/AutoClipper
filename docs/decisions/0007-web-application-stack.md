# ADR-0007: FastAPI backend + Next.js frontend

| | |
|---|---|
| **Status** | Accepted (2026-10-16). The initial setup runs locally; deployment can evolve with the product ([delivery defaults](0011-data-sources-and-local-delivery.md)). |
| **Date** | 2026-10-09 |
| **Related** | [Architecture § Interfaces](../architecture/overview.md#47-interfaces), [PRD § Interface](../product/prd.md#interface) |

## Context

The demo needs a polished, visual flow: paste a VOD link, watch progress, browse the ranked clips, preview, trim and download them or a highlights video. The pipeline and ML are Python. Team members already know Python web APIs and TypeScript/Next.js. Viewers, streamers and editors all use the same flow, so there is one app.

## Decision

- **Initial web app setup.** `autoclipper serve` starts the backend and the Next.js app runs in a browser. A packaged download and hosted deployment are options as the product develops.
- **Backend:** FastAPI in `src/autoclipper/api/`, wrapping the same library functions the CLI uses. Processing a VOD takes minutes, so long jobs run in a background worker, never inside the HTTP request. Pydantic models are shared with the CLI output schema.
- **Frontend:** Next.js + TypeScript (strict) + Tailwind in `web/`, talking to the API through one typed client. The highlight timeline is a custom component (canvas/SVG heat strip with markers).
- **Video preview:** the backend serves downloaded media and rendered clips through the API; the browser uses the returned URLs. Full VOD playback is optional.
- **Build early:** start the UI with synthetic (fake) candidates while the pipeline and model are being built. Don't wait for ML.
- **Delivery options.** Start with the laptop setup. A project website can offer the app download and link to a hosted instance; worker execution, storage and API extensions are chosen as needed ([delivery defaults](0011-data-sources-and-local-delivery.md)).

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
