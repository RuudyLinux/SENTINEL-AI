# SENTINEL VISION — Manual Browser QA Checklist

Run against `http://localhost:3000` (backend on `:8000`). Check items off as PASS/FAIL; note
anything unexpected under the item rather than fixing it.

## 1. Login
- Go to `/login`, enter `admin` / `sentinel123`, submit.
- Expected: redirected to the dashboard, header shows "System Administrator" / Administrator.
- [ ] PASS  [ ] FAIL

## 2. Command Center
- Land on `/dashboard`.
- Expected: KPI tiles (cameras, alerts, incidents), system status indicator, no blank sections,
  no "undefined"/"NaN" text.
- [ ] PASS  [ ] FAIL

## 3. Live Camera Wall
- Go to `/live`.
- Expected: a tile per registered camera — code, status dot, resolution/FPS, live MJPEG image
  for any `online` camera.
- [ ] PASS  [ ] FAIL

## 4. Camera start/stop/status
- Go to `/cameras`. Select a camera → Restart (or Start/Stop if selected via checkboxes).
- Expected: status transitions `offline → online` within ~10s; FPS/resolution populate; no page
  reload needed (polling picks it up).
- [ ] PASS  [ ] FAIL

## 5. Critical alert appearance
- Trigger `POST /api/system/demo/trigger-scenario` (via `/docs`, DEMO_MODE) with both demo
  cameras started.
- Expected: a new CRITICAL alert appears on `/alerts` within a few seconds (polling), header
  bell count increments.
- [ ] PASS  [ ] FAIL

## 6. Alert detail / explainability
- Open the new alert from `/alerts`.
- Expected: severity, camera, timestamp, confidence, and a `reasons[]` explanation
  ("Watchlist signal: plate ... matches an active watchlist entry") — language reads as a
  potential match, not an identity claim. Confirm/Reject/Needs-Review controls present.
- [ ] PASS  [ ] FAIL

## 7. Cross-camera investigation
- Go to `/investigate`, search plate `GJ05AB1234`.
- Expected: sightings list shows both `C-014` and `C-019` in chronological order with real
  timestamps.
- [ ] PASS  [ ] FAIL

## 8. Map / route
- From the investigation result (or `/map`), view the vehicle's route.
- Expected: both camera points plot at their real coordinates; route order matches the
  timestamps from item 7.
- [ ] PASS  [ ] FAIL

## 9. Incident creation/detail
- Go to `/incidents`, open the auto-created incident for this plate.
- Expected: title references the plate/camera, priority CRITICAL, status `open`, linked
  camera/alert/vehicle fields populated (not blank).
- [ ] PASS  [ ] FAIL

## 10. Investigation timeline
- On the incident detail page, open the Timeline tab.
- Expected: both sightings + the alert appear, chronologically ordered, each with a source
  camera and timestamp.
- [ ] PASS  [ ] FAIL

## 11. Snapshot evidence
- Evidence tab on the incident.
- Expected: a real image renders inline (not a broken-image icon), captioned with camera/type/
  timestamp.
- [ ] PASS  [ ] FAIL

## 12. Video clip playback
- Same Evidence tab, the clip entry.
- Expected: a `<video>` player renders and actually plays a several-second clip (not just a
  static thumbnail or a download-only link).
- [ ] PASS  [ ] FAIL

## 13. Evidence authorization behavior
- Copy an evidence file's direct API URL (`/api/evidence/{id}/file`) and open it in a new tab
  with no query string.
- Expected: request fails (422/401) — not a silently-served file. Reloading the evidence page
  normally in-app still shows it (token fetched automatically).
- [ ] PASS  [ ] FAIL

## 14. Audit trail
- Go to `/admin/audit`.
- Expected: recent rows for the actions just taken (login, start_camera, trigger_demo_scenario,
  evidence access) — each shows user, action, resource, result, timestamp.
- [ ] PASS  [ ] FAIL

## 15. RBAC with Auditor
- Log out, log in as `auditor1` / `sentinel123`.
- Go to `/cameras`.
- Expected: no "Add Camera" / "Sync Camera Catalogue" / row checkboxes / Restart controls
  visible.
- Go to `/admin/audit`.
- Expected: fully accessible (this IS the Auditor's role).
- Try `POST /api/cameras` as `auditor1` directly (e.g. via `/docs`).
- Expected: `403 Forbidden`, not a silent failure or a 500.
- [ ] PASS  [ ] FAIL

## 16. Loading states
- Hard-refresh any data-heavy page (`/incidents`, `/evidence`).
- Expected: a visible loading indicator, not a blank flash or stale content held indefinitely.
- [ ] PASS  [ ] FAIL

## 17. Offline/error states
- Stop the backend process, then click around the frontend (or reload a page).
- Expected: pages show an explicit "Data unavailable" / error panel with a Retry action —
  never a blank page, an infinite spinner, or a raw stack trace. Restart the backend and Retry
  → normal data returns.
- [ ] PASS  [ ] FAIL

## 18. Keyboard/focus/accessibility basics
- Using Tab only (no mouse), navigate the login form and the Cameras page.
- Expected: a visible focus outline on each interactive element in a sensible order; Enter
  submits the login form; no keyboard trap.
- [ ] PASS  [ ] FAIL

## 19. No console errors
- Open DevTools console, click through dashboard → live → alerts → incident → evidence → audit.
- Expected: no red errors (warnings acceptable). Note any that appear.
- [ ] PASS  [ ] FAIL

## 20. Responsive layout
- Resize the browser to a narrow width (~768px) and a small laptop width (~1280px).
- Expected: no horizontal page scroll, tables/cards reflow instead of clipping, sidebar/nav
  remains usable.
- [ ] PASS  [ ] FAIL

---

## 5-minute judge demo smoke test

1. Login as `admin`.
2. `POST /api/system/demo/reset`, then start `C-014` and `C-019` from `/cameras`.
3. `POST /api/system/demo/trigger-scenario`.
4. `/alerts` → open the CRITICAL alert → confirm reasons/confidence shown.
5. `/investigate` → search `GJ05AB1234` → confirm `C-014 → C-019` route.
6. Open the incident → confirm Timeline + snapshot + clip evidence load and play.
7. `/admin/audit` → confirm the action trail for steps 2–6 is present.
8. Log out, log in as `auditor1` → confirm Cameras page hides management controls.

If all 8 pass, the system is demo-ready.
