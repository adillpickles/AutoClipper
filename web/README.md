# AutoClipper web app

The Next.js frontend for AutoClipper ([app stack](../docs/decisions/0007-web-application-stack.md)). It has the start screen (`/`) and the progress screen (`/jobs/<job_id>`), running in **mock mode**: the typed API client answers from the [API contract](../docs/architecture/api-contract.md)'s examples and § 11 scenarios inside the browser, so no backend is needed yet (#33 connects the real one).

Stack: Next.js 16 (App Router, Turbopack), React 19, TypeScript (strict), Tailwind CSS 4, ESLint 9 and Prettier 3. npm is the package manager.

## Run it

You need Node.js 24 (LTS). The version is pinned in [`.nvmrc`](.nvmrc) and in `engines` in `package.json`; `nvm use` or `fnm use` in this folder picks it up.

```bash
cd web
npm ci          # install the exact versions in package-lock.json
npm run dev     # dev server at http://localhost:3000
```

The same commands work in PowerShell. Full setup steps are in [docs/development/setup.md](../docs/development/setup.md#frontend-web).

## Scripts

| Script                 | What it does                                                            |
| ---------------------- | ----------------------------------------------------------------------- |
| `npm run dev`          | Dev server with hot reload (Turbopack)                                  |
| `npm run build`        | Production build                                                        |
| `npm run start`        | Serve the production build (run `build` first)                          |
| `npm run lint`         | ESLint (Next.js core-web-vitals + TypeScript rules). Any warning fails. |
| `npm run typecheck`    | `next typegen` (route types, `next-env.d.ts`), then `tsc --noEmit`      |
| `npm run format`       | Prettier: format every file, sorting Tailwind classes                   |
| `npm run format:check` | Prettier: report unformatted files without changing them (what CI runs) |
| `npm test`             | Vitest unit tests (`src/**/*.test.ts`)                                  |

Before you push, run what CI runs ([`.github/workflows/web.yml`](../.github/workflows/web.yml)):

```bash
npm run lint && npm run typecheck && npm run format:check && npm test && npm run build
```

## Layout

```text
web/
├── src/
│   ├── app/                 # App Router: routes, layouts, global styles
│   │   ├── layout.tsx       # root layout and page metadata
│   │   ├── page.tsx         # start screen: VOD link and optional local file
│   │   ├── jobs/[jobId]/    # progress screen
│   │   ├── icon.svg         # favicon
│   │   └── globals.css      # Tailwind entry point and color tokens
│   ├── components/          # start form, job progress, header
│   └── lib/api/             # the typed API client: types, mock mode, fixtures
├── AGENTS.md           # maintained by Next.js; see below
├── eslint.config.mjs
├── next.config.ts
├── .prettierrc.json
└── tsconfig.json
```

New code follows the frontend conventions in [docs/development/conventions.md § 10](../docs/development/conventions.md#10-frontend). That's where components and the typed API client will live.

## Mock mode

`getApiClient()` in `src/lib/api/client.ts` is the only way components reach the API. In mock mode it simulates a server.

- **Timing:** a job is queued for one step (`MOCK_STEP_MS`, 2 s) and then runs the six stages. The audio download takes four steps and reports how far along it is; every other stage takes one. Jobs run one at a time, first in first out, as on the server.
- **Scenarios:** the VOD ID picks the outcome, as in the contract's § 11. For example, `9000000003` finishes with a chat warning, `9000000004` fails, `9000000006` finds nothing, and any other ID gets the example result.
- **Storage:** jobs are kept in `localStorage`, so a reload or a new tab finds them.
- **Fixtures:** the results are in `src/lib/api/fixtures/`.

## Notes

- **`AGENTS.md`** is written by `next dev` when it detects a coding agent, and points agents at the docs bundled in `node_modules/next/dist/docs/` for the installed version. Commit it if `next dev` updates it, and don't edit the marked block. The repo-wide agent rules are in the root [`AGENTS.md`](../AGENTS.md).
- **Telemetry:** Next.js collects anonymous usage data. CI turns it off with `NEXT_TELEMETRY_DISABLED=1`. To opt out locally, run `npx next telemetry disable`.
- **Generated files** (`.next/`, `next-env.d.ts`, `*.tsbuildinfo`) are git-ignored and recreated by `dev`, `build` and `typecheck`.
