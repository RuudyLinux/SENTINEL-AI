import { defineConfig, devices } from "@playwright/test";

/**
 * Smoke tests against an already running stack (the e2e job in
 * .github/workflows/frontend.yml, or both services started locally). No
 * webServer block: the backend needs its own env (throwaway DB, no grid
 * credentials) and hiding that in a spawn command would bury it.
 */
export default defineConfig({
  testDir: "./e2e",
  // a real backend loading YOLO/EasyOCR weights is slow
  timeout: 60_000,
  expect: { timeout: 15_000 },
  fullyParallel: false,
  // specs share one backend and account, a retry would fight the half-done
  // previous attempt
  retries: 0,
  workers: 1,
  reporter: process.env.CI ? [["html", { open: "never" }], ["list"]] : [["list"]],
  use: {
    // localhost, not 127.0.0.1: CORS_ALLOWED_ORIGINS defaults to
    // http://localhost:3000 and the browser treats them as different origins.
    // From 127.0.0.1 every API call fails CORS and it looks like a broken login.
    baseURL: process.env.PLAYWRIGHT_BASE_URL || "http://localhost:3000",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "off",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
});
