# AutoClipper web app

The Next.js frontend for AutoClipper ([ADR-0007](../docs/decisions/0007-web-application-stack.md)). Right now it's a scaffold with one placeholder home page. It doesn't call the API and shows no data.

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

Before you push, run what CI runs ([`.github/workflows/web.yml`](../.github/workflows/web.yml)):

```bash
npm run lint && npm run typecheck && npm run format:check && npm run build
```

## Layout

```text
web/
├── src/
│   └── app/            # App Router: routes, layouts, global styles
│       ├── layout.tsx  # root layout and page metadata
│       ├── page.tsx    # placeholder home page
│       ├── icon.svg    # favicon
│       └── globals.css # Tailwind entry point and color tokens
├── AGENTS.md           # maintained by Next.js; see below
├── eslint.config.mjs
├── next.config.ts
├── .prettierrc.json
└── tsconfig.json
```

New code follows the frontend conventions in [docs/development/conventions.md § 10](../docs/development/conventions.md#10-frontend). That's where components and the typed API client will live.

## Notes

- **`AGENTS.md`** is written by `next dev` when it detects a coding agent, and points agents at the docs bundled in `node_modules/next/dist/docs/` for the installed version. Commit it if `next dev` updates it, and don't edit the marked block. The repo-wide agent rules are in the root [`AGENTS.md`](../AGENTS.md).
- **Telemetry:** Next.js collects anonymous usage data. CI turns it off with `NEXT_TELEMETRY_DISABLED=1`. To opt out locally, run `npx next telemetry disable`.
- **Generated files** (`.next/`, `next-env.d.ts`, `*.tsbuildinfo`) are git-ignored and recreated by `dev`, `build` and `typecheck`.
