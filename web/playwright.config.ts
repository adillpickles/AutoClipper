import { defineConfig, devices } from "@playwright/test";

// Browser smoke tests of the demo path (e2e/). See web/README.md § End-to-end tests.
//
// By default the app runs in in-browser mock mode, so no backend is needed. Set E2E_API_ORIGIN
// (e.g. http://127.0.0.1:8000) to build the app against `autoclipper serve --jobs mock` instead;
// Playwright then starts that backend from the repo root as well.

const PORT = Number(process.env.E2E_PORT ?? 3197);
const BASE_URL = `http://127.0.0.1:${PORT}`;
const API_ORIGIN = process.env.E2E_API_ORIGIN?.trim() || null;
const CI = Boolean(process.env.CI);

// NEXT_PUBLIC_* values are inlined at build time, so the web server builds for the chosen API
// mode before it starts. Outside CI an already running server on PORT is reused as is.
const webServer = {
  command: `npm run build && npm run start -- --hostname 127.0.0.1 --port ${PORT}`,
  url: BASE_URL,
  reuseExistingServer: !CI,
  timeout: 240_000,
  env: {
    NEXT_TELEMETRY_DISABLED: "1",
    ...(API_ORIGIN
      ? { NEXT_PUBLIC_API_ORIGIN: API_ORIGIN }
      : { NEXT_PUBLIC_API_MODE: "mock" }),
  },
};

function backendServer(origin: string) {
  const url = new URL(origin);
  const port = url.port || "8000";
  return {
    command: `uv run autoclipper serve --jobs mock --step-s 0.2 --host ${url.hostname} --port ${port}`,
    cwd: "..",
    url: `${url.origin}/api/v1/health`,
    reuseExistingServer: !CI,
    timeout: 180_000,
    env: {
      AUTOCLIPPER_CORS_ORIGINS: `${BASE_URL},http://localhost:${PORT}`,
    },
  };
}

export default defineConfig({
  testDir: "./e2e",
  // A browser-mock job takes about 20 seconds (MOCK_STEP_MS in src/lib/api/mock.ts).
  timeout: 120_000,
  expect: { timeout: 10_000 },
  fullyParallel: true,
  forbidOnly: CI,
  retries: 0,
  workers: CI ? 2 : undefined,
  reporter: CI
    ? [["github"], ["html", { open: "never" }]]
    : [["list"], ["html", { open: "never" }]],
  use: {
    baseURL: BASE_URL,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: API_ORIGIN ? [backendServer(API_ORIGIN), webServer] : webServer,
});
