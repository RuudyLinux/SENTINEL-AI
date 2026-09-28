// Real screenshots of the running system for the submission package.
const { chromium } = require("D:/Aproject/SENTINEL AI/frontend/node_modules/@playwright/test");
const OUT = "D:/Aproject/SENTINEL AI/Documentation/screenshots/";
const API = "http://localhost:8000";
(async () => {
  const tok = (await (await fetch(API + "/api/auth/login", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ username: "admin", password: "sentinel123" }) })).json()).access_token;
  const get = async (p) => (await fetch(API + p, { headers: { Authorization: `Bearer ${tok}` } })).json();
  const first = (d) => (Array.isArray(d) ? d : d.items || Object.values(d).find(Array.isArray) || [])[0];
  const alert = first(await get("/api/alerts?limit=1"));
  const incident = first(await get("/api/incidents?limit=1"));
  const evidence = first(await get("/api/evidence?limit=1"));
  const vehicle = "veh_47780f293f";
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1600, height: 900 }, deviceScaleFactor: 1.5 });
  await page.goto("http://localhost:3000/login");
  await page.screenshot({ path: OUT + "00_login.png" });
  await page.getByLabel(/police id \/ username/i).fill("admin");
  await page.getByLabel(/^password$/i).fill("sentinel123");
  await page.getByRole("button", { name: /^login$/i }).click();
  await page.waitForURL(/\/dashboard/, { timeout: 60000 });
  const shots = [
    ["01_dashboard", "/dashboard"], ["02_camera_map", "/live/map"], ["03_cameras", "/cameras"],
    ["04_camera_control", "/cameras/control"], ["05_live_ai_vision", "/vision"], ["06_alerts", "/alerts"],
    ["07_alert_detail", alert ? `/alerts/${alert.id}` : null], ["08_incidents", "/incidents"],
    ["09_incident_detail", incident ? `/incidents/${incident.id}` : null], ["10_evidence", "/evidence"],
    ["11_evidence_detail", evidence ? `/evidence/${evidence.id}` : null], ["12_vehicle_journey", `/vehicles/${vehicle}`],
    ["13_anpr", "/vehicles/anpr"], ["14_watchlists", "/watchlists"], ["15_rules", "/admin/rules"],
    ["16_self_heal_health", "/self-heal/health"], ["17_camera_health", "/self-heal/camera-health"],
    ["18_audit", "/admin/audit"], ["19_users_rbac", "/admin/users"], ["20_map_intelligence", "/map"],
  ];
  for (const [name, path] of shots) {
    if (!path) continue;
    await page.goto("http://localhost:3000" + path);
    await page.waitForTimeout(3500);
    await page.screenshot({ path: OUT + name + ".png" });
    console.log("saved", name, path);
  }
  await browser.close();
})();
