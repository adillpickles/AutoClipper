import { fileURLToPath } from "node:url";

import { defineConfig } from "vitest/config";

export default defineConfig({
  resolve: {
    alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) },
  },
  test: {
    // Component tests (.test.tsx) pick jsdom per file via a `@vitest-environment` docblock;
    // logic tests keep running in plain node.
    include: ["src/**/*.test.ts", "src/**/*.test.tsx"],
    poolOptions: {
      forks: {
        // Node 26 defines a global `localStorage` (undefined without --localstorage-file), and
        // Vitest's jsdom environment won't replace a global that already exists, so tests saw
        // `window.localStorage === undefined`. Turning Node's web storage off in the test workers
        // (already the default on Node 24) lets jsdom's storage through again. See #179.
        execArgv: ["--no-experimental-webstorage"],
      },
    },
  },
});
