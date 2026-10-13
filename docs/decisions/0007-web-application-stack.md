# ADR-0007: FastAPI backend + Next.js frontend

| | |
|---|---|
| **Status** | Proposed (choose application form before the early UI prototype) |
| **Date** | 2026-10-09 |
| **Related** | [Architecture § Interfaces](../architecture/overview.md#47-interfaces), [PRD § Interface](../product/prd.md#interface) |

## Context

The demo needs a polished, visual flow: paste a URL, watch progress, see a highlight timeline, preview and export clips. The pipeline is Python. Team members have experience with Python web APIs and with TypeScript/Next.js.

Web versus desktop remains open. The shared review/trim/export experience serves viewers, streamers and editors; it does not require three applications. Prototype with synthetic candidates while ML proceeds. This proposal's local demo can avoid cloud infrastructure; hosted delivery requires a separate scope decision.

## Decision (proposed)

- **Backend:** FastAPI in `src/autoclipper/api/`, wrapping library functions. Long jobs run in a background worker (in-process queue for the single-user demo). Pydantic models shared with the CLI output schema.
- **Frontend:** Next.js + TypeScript (strict) in `web/`, talking to the API through one typed client. The timeline is a custom component (canvas/SVG heat-strip with markers).
- **Video preview:** approved retained/exported media could be served from the backend; Twitch-hosted playback follows the applicable player rules. Select the permitted route under OQ-17 before implementing preview. Full VOD playback is optional.

## Options considered

| Option | Pros | Cons |
|---|---|---|
| **FastAPI + Next.js** (proposed) | Polished UI. Team experience. Clean separation. | Two toolchains |
| Streamlit / Gradio | Very fast to build. Python only. | Looks generic. A custom timeline is hard. Weaker demo polish. |
| FastAPI + server-rendered templates | One language | Interactive timeline is harder |
| Desktop application | Local media and processing can stay together | Packaging and updates add work; integration needs evaluation |

**Fallback proposal:** a simpler UI over the same functions may reduce cost, provided it still supports an appealing review, preview, trim and export flow. Choose it with the team rather than silently reducing the demo to a CLI.

## Consequences

- **Positive:** the best demo quality, and reusable API endpoints.
- **Negative:** Node tooling, plus a second set of conventions to maintain.
- **Follow-ups:** frontend conventions doc at `web/` scaffold time. CI job for `web/`.

## Revisit when

After the early interface prototype, based on user flow, integration cost and remaining time.
