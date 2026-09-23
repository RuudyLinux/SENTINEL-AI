import { defineConfig } from "vitest/config";

/**
 * Unit tests only. `e2e/` is Playwright's — it drives a real browser against a
 * running stack and cannot execute under vitest, so collecting it here just
 * produces a confusing failure in an unrelated test run.
 */
export default defineConfig({
  test: {
    include: ["lib/**/*.test.ts", "components/**/*.test.ts", "app/**/*.test.ts"],
    exclude: ["e2e/**", "node_modules/**", ".next/**"],
  },
});
