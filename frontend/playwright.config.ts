import { defineConfig, devices } from "@playwright/test";

/**
 * Smoke-test configuration.
 *
 * The tests run against an already-running real stack (see the `e2e` job in
 * .github/workflows/frontend.yml, or start both services locally). There is no
 * `webServer` block on purpose: the backend needs its own environment — a
 * throwaway database, no real camera-grid credentials — and burying that in a
 * Playwright spawn command would hide it from anyone reading the workflow.
 */
export default defineConfig({
  testDir: "./e2e",
  // A real backend loading YOLO/EasyOCR weights is not fast; these are
  // wall-clock realities, not slow tests.
  timeout: 60_000,
  expect: { timeout: 15_000 },
  fullyParallel: false,
  // The specs share one backend and one seeded account, so a retry of a
  // half-finished test would fight the previous attempt's state.
  retries: 0,
  workers: 1,
  reporter: process.env.CI ? [["html", { open: "never" }], ["list"]] : [["list"]],
  use: {
    // `localhost`, not `127.0.0.1`: the backend's CORS_ALLOWED_ORIGINS default
    // is http://localhost:3000, and a browser treats those two as different
    // origins. Serving the page from 127.0.0.1 makes every API call fail CORS
    // — correct enforcement, but it would look like a broken login rather than
    // a misconfigured harness. Matching the documented origin keeps the test
    // exercising the same configuration a real deployment uses.
    baseURL: process.env.PLAYWRIGHT_BASE_URL || "http://localhost:3000",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "off",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
});
