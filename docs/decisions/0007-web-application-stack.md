# ADR-0007: FastAPI backend + Next.js frontend

| | |
|---|---|
| **Status** | Proposed (needed by M5; no rush to accept) |
| **Date** | 2026-10-09 |
| **Related** | [Architecture § Interfaces](../architecture/overview.md#47-interfaces), [PRD § Interface](../product/prd.md#interface) |

## Context

The demo needs a polished, visual flow: paste a URL, watch progress, see a highlight timeline, preview and export clips. The pipeline is Python. Team members have experience with Python web APIs and with TypeScript/Next.js.

## Decision (proposed)

- **Backend:** FastAPI in `src/autoclipper/api/`, wrapping library functions. Long jobs run in a background worker (in-process queue for the single-user demo). Pydantic models shared with the CLI output schema.
- **Frontend:** Next.js + TypeScript (strict) in `web/`, talking to the API through one typed client. The timeline is a custom component (canvas/SVG heat-strip with markers).
- **Video preview:** serve exported MP4s and short preview segments from the backend. No full VOD playback is needed.

## Options considered

| Option | Pros | Cons |
|---|---|---|
| **FastAPI + Next.js** (proposed) | Polished UI. Team experience. Clean separation. | Two toolchains |
| Streamlit / Gradio | Very fast to build. Python only. | Looks generic. A custom timeline is hard. Weaker demo polish. |
| FastAPI + server-rendered templates | One language | Interactive timeline is harder |

**Fallback:** if time is short at M5, a Streamlit or Gradio UI over the same library functions is acceptable for the demo.

## Consequences

- **Positive:** the best demo quality, and reusable API endpoints.
- **Negative:** Node tooling, plus a second set of conventions to maintain.
- **Follow-ups:** frontend conventions doc at `web/` scaffold time. CI job for `web/`.

## Revisit when

At M4 completion, based on the remaining time.
