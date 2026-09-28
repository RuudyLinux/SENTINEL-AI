import { defineConfig } from "vitest/config";

/**
 * Unit tests only. e2e/ is Playwright's and can't run under vitest.
 */
export default defineConfig({
  test: {
    include: ["lib/**/*.test.ts", "components/**/*.test.ts", "app/**/*.test.ts"],
    exclude: ["e2e/**", "node_modules/**", ".next/**"],
  },
});
