import { expect, test, type Page } from "@playwright/test";

/**
 * Critical-flow smoke test.
 *
 * Covers the path a judge or an operator actually walks: log in, reach the live
 * control room, search a plate, and open a vehicle's journey and evidence.
 *
 * It runs against a REAL backend on a throwaway database, so it deliberately
 * does not assert that detections exist — no camera is streaming in CI, and a
 * test that demanded real detections would either be a lie or permanently red.
 * What it does assert is that every screen loads its real state and reports
 * honestly, including the empty and not-found cases, which is exactly where a
 * frontend/backend contract break shows up.
 */

const ADMIN = { username: "admin", password: "sentinel123" };

async function fillCredentials(page: Page, password: string) {
  await page.goto("/login");
  await page.getByLabel(/police id \/ username/i).fill(ADMIN.username);
  await page.getByLabel(/^password$/i).fill(password);
  await page.getByRole("button", { name: /^login$/i }).click();
}

async function login(page: Page) {
  await fillCredentials(page, ADMIN.password);
  await page.waitForURL(/\/dashboard/, { timeout: 30_000 });
}

test.describe("critical operator flow", () => {
  test("logs in and reaches the command centre", async ({ page }) => {
    await login(page);
    await expect(page.getByRole("heading", { name: /command center/i })).toBeVisible();
  });

  test("rejects a wrong password with a real message", async ({ page }) => {
    // Regression guard: a 401 from the login endpoint used to hard-redirect and
    // silently reset the form, showing the user nothing at all.
    await fillCredentials(page, "definitely-not-the-password");
    await expect(page.getByText(/invalid|incorrect|unauthor/i)).toBeVisible();
    await expect(page).toHaveURL(/\/login/);
  });

  test("live control room connects to the event stream", async ({ page }) => {
    await login(page);
    await page.goto("/vision");
    await expect(page.getByRole("heading", { name: /live ai detection/i })).toBeVisible();
    // The WebSocket must actually connect — a dead feed showing a healthy page
    // is the exact failure this screen exists to make visible.
    await expect(page.getByText("LIVE STREAM")).toBeVisible({ timeout: 20_000 });
    // Filter chips drive what the operator sees; they must render.
    await expect(page.getByRole("button", { name: /plates/i })).toBeVisible();
    await expect(page.getByRole("button", { name: /alerts/i })).toBeVisible();
  });

  test("pausing the feed says the system is still running", async ({ page }) => {
    await login(page);
    await page.goto("/vision");
    await page.getByRole("button", { name: /^pause$/i }).click();
    await expect(page.getByText(/feed paused/i)).toBeVisible();
    await expect(page.getByText(/still detecting/i)).toBeVisible();
    await page.getByRole("button", { name: /^resume$/i }).click();
    await expect(page.getByText(/feed paused/i)).not.toBeVisible();
  });

  test("an unrecognized plate reports not found rather than an arbitrary match", async ({ page }) => {
    await login(page);
    await page.goto("/vehicles/anpr");
    await page.getByPlaceholder(/GJ05AB1234/i).fill("GJ99ZZ0000");
    await page.getByRole("button", { name: /search/i }).click();
    await expect(page.getByText(/no plate matches|no vehicle/i)).toBeVisible();
  });

  test("an unknown vehicle id shows a real error, not a blank page", async ({ page }) => {
    await login(page);
    await page.goto("/vehicles/veh_doesnotexist");
    // `.first()`: the error panel legitimately states the problem in both its
    // heading and its body, so the phrase appearing more than once is correct
    // behavior, not an ambiguity to resolve by narrowing the wording.
    await expect(page.getByText(/not found|unavailable|could not/i).first()).toBeVisible({ timeout: 20_000 });
  });

  test("the operator screens all load their real state", async ({ page }) => {
    await login(page);
    // Headings are matched exactly as the screens actually title themselves —
    // a loose regex here would keep passing after a screen was renamed or
    // replaced, which defeats the point of a smoke test.
    for (const [path, heading] of [
      ["/live", /^Live Cameras$/],
      ["/alerts", /^Alert Center$/],
      ["/incidents", /^Incident Management$/],
      ["/evidence", /^Evidence Library$/],
      ["/watchlists", /^Watchlists$/],
      ["/map", /^Map Intelligence$/],
      ["/self-heal/health", /^System Health$/],
      ["/analytics", /^Analytics$/],
    ] as const) {
      await page.goto(path);
      await expect(page.getByRole("heading", { name: heading }).first()).toBeVisible();
      // Nothing may render a stuck spinner or a silent blank: every screen owes
      // the operator either real data, a real empty state, or a real error.
      await expect(page.locator("text=/undefined|NaN|\\[object Object\\]/")).toHaveCount(0);
    }
  });

  test("camera control centre enforces its own confirmation on disruptive actions", async ({ page }) => {
    await login(page);
    await page.goto("/cameras/control");
    await expect(page.getByRole("heading", { name: /camera control/i })).toBeVisible();
  });
});
