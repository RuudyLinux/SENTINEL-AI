import { expect, test, type Page } from "@playwright/test";

/**
 * No screen logs a browser error, and none overflows horizontally on a phone.
 *
 * Both happened and got dismissed as cosmetic:
 * - every page requested /branding/smart-shield-logo.png, which isn't in the
 *   repo, and logged a 404. BrandLogo fell back fine, but a console with a
 *   permanent error in it is a console nobody checks.
 * - /admin/users logged a DOM warning on its password field, and the
 *   browser's suggested fix (current-password) would have been a real bug:
 *   that field creates SOMEONE ELSE'S account.
 *
 * Strict on purpose: any console error, uncaught error or failed request on
 * these routes fails the run.
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

/** Errors that belong to the harness, not the app. Just these two, matched
 *  narrowly so a real failure can't hide behind them. */
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

/** Widest element sticking out past the viewport, ignoring things inside a
 *  scrollable container (a wide table in overflow-x-auto is on purpose). */
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
      // one test walks 22 routes against a real backend; the 60s default is
      // for one screen and a loaded CI runner would time out on speed alone
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
        // Next prefetches every visible <Link> as ?_rsc=, and moving on aborts
        // the in-flight ones. That's the browser dropping unneeded work, and
        // it only shows up once cameras exist (empty CI DB never hit it).
        // Other failures, or an RSC request failing for real, still count.
        const aborted = req.failure()?.errorText === "net::ERR_ABORTED";
        if (aborted && req.url().includes("_rsc=")) return;
        if (!isIgnorable(req.url())) problems.push(`[${page.url()}] request failed: ${req.url()}`);
      });
      page.on("response", (res) => {
        // a 404 on a static asset is what the logo bug looked like. API 4xx
        // are the app's business (unknown vehicle -> 404 is right), so only
        // same-origin non-API routes count
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
