import { expect, test, type Page } from "@playwright/test";

/**
 * No screen may log a browser error, and no screen may overflow horizontally
 * on a phone.
 *
 * Both of these were real and both had been dismissed as cosmetic:
 *
 * - Every single page requested `/branding/smart-shield-logo.png`, a file the
 *   repository does not contain, and logged `404 (Not Found)` for it. The
 *   visible result was correct — `BrandLogo` falls back to the shipped shield
 *   — so it was left alone as "one console line". The cost is not the line:
 *   it is that a console with a permanent error in it stops being somewhere
 *   anyone looks for a new one.
 * - `/admin/users` logged a DOM warning on its password field, and the
 *   browser's suggested fix (`current-password`) would have been a real bug:
 *   the field creates SOMEONE ELSE'S account.
 *
 * This spec is deliberately strict: any console error, any uncaught page
 * error, and any failed request on any of these routes fails the run.
 */

const ADMIN = { username: "admin", password: "sentinel123" };

// 375px is the narrowest phone this dashboard is expected on; 1366 is the
// control-room laptop. A layout bug usually shows up at one or the other.
const VIEWPORTS = [
  { name: "phone", width: 375, height: 812 },
  { name: "laptop", width: 1366, height: 768 },
] as const;

const ROUTES = [
  "/dashboard", "/cameras", "/cameras/control", "/live", "/map", "/alerts",
  "/incidents", "/investigate", "/search", "/analytics", "/evidence",
  "/vision", "/watchlists", "/admin/audit", "/admin/rules", "/admin/system",
  "/admin/users", "/self-heal/health", "/self-heal/problems",
  "/self-heal/activity", "/self-heal/errors", "/self-heal/camera-health",
] as const;

/** Errors that are the HARNESS's, not the application's. Kept to the two that
 *  are genuinely not the app's to fix, and matched narrowly so a real failure
 *  cannot hide behind them. */
const IGNORED = [
  // Next's dev-mode HMR socket, closed when a navigation interrupts it.
  /_next\/static\/chunks\/.*hot-reloader/i,
  /websocket connection to 'ws:\/\/localhost:3000\/_next/i,
];

function isIgnorable(text: string): boolean {
  return IGNORED.some((re) => re.test(text));
}

async function login(page: Page) {
  await page.goto("/login");
  await page.getByLabel(/police id \/ username/i).fill(ADMIN.username);
  await page.getByLabel(/^password$/i).fill(ADMIN.password);
  await page.getByRole("button", { name: /^login$/i }).click();
  await page.waitForURL(/\/dashboard/, { timeout: 30_000 });
}

/** Widest element that sticks out past the viewport, ignoring anything inside
 *  a deliberately scrollable container — a wide table in `overflow-x-auto` is
 *  a design decision, not a bug. */
async function horizontalOverflow(page: Page) {
  return page.evaluate(() => {
    const vw = document.documentElement.clientWidth;
    const offenders: string[] = [];
    document.querySelectorAll<HTMLElement>("*").forEach((el) => {
      const r = el.getBoundingClientRect();
      if (r.width === 0) return;
      let scrollable = false;
      let n = el.parentElement;
      while (n && n !== document.body) {
        const ox = getComputedStyle(n).overflowX;
        if (ox === "auto" || ox === "scroll" || ox === "hidden") { scrollable = true; break; }
        n = n.parentElement;
      }
      if (scrollable) return;
      const over = Math.round((r.right - vw) * 10) / 10;
      if (over > 0.5) {
        const cls = typeof el.className === "string" ? el.className.split(/\s+/).slice(0, 3).join(".") : "";
        offenders.push(`${el.tagName.toLowerCase()}.${cls} +${over}px`);
      }
    });
    return { page: document.documentElement.scrollWidth - vw, offenders };
  });
}

for (const viewport of VIEWPORTS) {
  test.describe(`console and layout hygiene (${viewport.name})`, () => {
    test(`every operator screen loads clean at ${viewport.width}px`, async ({ page }) => {
      // One test walks 22 routes against a real backend. The suite-wide 60s
      // budget is sized for a single screen, not for a sweep; on a loaded CI
      // runner this would time out on speed alone and read as a failure.
      test.setTimeout(240_000);
      await page.setViewportSize({ width: viewport.width, height: viewport.height });

      const problems: string[] = [];
      page.on("console", (msg) => {
        if (msg.type() !== "error" && msg.type() !== "warning") return;
        const text = msg.text();
        if (!isIgnorable(text)) problems.push(`[${page.url()}] console.${msg.type()}: ${text}`);
      });
      page.on("pageerror", (err) => problems.push(`[${page.url()}] uncaught: ${err.message}`));
      page.on("requestfailed", (req) => {
        if (!isIgnorable(req.url())) problems.push(`[${page.url()}] request failed: ${req.url()}`);
      });
      page.on("response", (res) => {
        // A 404 on a static asset is the exact shape of the logo bug. API
        // 4xx are the app's own business (a 404 for an unknown vehicle is
        // correct behaviour), so only same-origin non-API routes count.
        if (res.status() < 400) return;
        const url = new URL(res.url());
        if (url.port !== "3000" || url.pathname.startsWith("/api/")) return;
        if (isIgnorable(res.url())) return;
        problems.push(`[${page.url()}] ${res.status()} for ${url.pathname}`);
      });

      await login(page);

      const overflows: string[] = [];
      for (const route of ROUTES) {
        await page.goto(route);
        await page.waitForLoadState("networkidle").catch(() => {});
        const { page: pageOverflow, offenders } = await horizontalOverflow(page);
        if (pageOverflow > 0) overflows.push(`${route}: page scrolls ${pageOverflow}px`);
        if (offenders.length) overflows.push(`${route}: ${offenders.slice(0, 3).join(", ")}`);
      }

      expect(problems, `browser reported problems:\n${problems.join("\n")}`).toEqual([]);
      expect(overflows, `horizontal overflow:\n${overflows.join("\n")}`).toEqual([]);
    });
  });
}
