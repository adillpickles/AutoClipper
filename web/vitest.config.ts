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
  },
});
