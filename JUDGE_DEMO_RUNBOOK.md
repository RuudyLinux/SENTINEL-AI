# SENTINEL VISION — Judge Demo Runbook

Short and practical. ~5 minutes end to end.

## 0. Start system

```
start.bat
```

Wait for both windows: backend `http://localhost:8000/docs`, frontend `http://localhost:3000`.

## 1. Login

`http://localhost:3000` → `admin` / `sentinel123`.

## 2. Reset to a clean demo state

Admin → System (or `POST /api/system/demo/reset` from `/docs`). Wipes old alerts/incidents/
evidence, ensures cameras `C-014`/`C-019` and the `GJ05AB1234` watchlist entry exist.

## 3. Start cameras

Cameras screen → select `C-014` and `C-019` → **Start selected**. Wait ~5–10s until both show
**online** with a live FPS number — this is real YOLOv8 + ByteTrack + EasyOCR running, not a
recording.

## 4. Trigger the scenario

`POST /api/system/demo/trigger-scenario` (from `/docs`, or a "Trigger Demo Scenario" action if
wired into the UI). Fires a real watchlist-plate sighting on C-014, then C-019 four minutes
later — through the real correlation/alert code, not scripted output.

## 5. Show the alert

Alerts screen → open the new **CRITICAL** alert. Point out: what (watchlist plate match), where
(camera), when (timestamp), why (explainable `reasons[]`), confidence, and the
Confirm/Reject/Needs-Review controls — it's presented as a **potential match**, not an
identity claim.

## 6. Show the cross-camera route

Investigate screen → search the plate `GJ05AB1234` → route shows `C-014 → C-019` with real
timestamps and the map plotting both real camera coordinates.

## 7. Show the incident

Incidents screen → open the auto-created incident → Timeline tab shows both sightings and the
alert, chronologically, with source cameras and timestamps.

## 8. Show the evidence

Evidence tab on the incident → a real snapshot (the camera's actual current frame) and a real
~15s video clip (5s pre-event + 10s post-event, from that camera's own live buffer) — both
served through the signed-token flow, not a direct file link.

## 9. Show the audit trail

Admin → Audit Log → filter to the last few minutes: `start_camera` ×2, `trigger_demo_scenario`,
`generate_evidence_clip` ×2, `request_evidence_file_token`, `download_evidence` — who, what,
when, resource, result, all real rows.

## 10. Show RBAC

Log out → log in as `auditor1` / `sentinel123` → open Cameras: no "Add Camera"/"Sync
Catalogue"/"Restart" controls (Auditor can't manage cameras) → Audit Log is still fully
visible (Auditor's actual job). Optionally show a raw `403` from `POST /api/cameras` as
`auditor1` in `/docs` to prove the backend enforces this, not just the UI.

---

**If something looks stuck**: camera "degraded" for a few seconds is normal (it's really
reading frames); check `GET /api/cameras/{id}/diagnostics` for live counters. Two cameras is
the tested, reliable configuration on this hardware — don't add a third mid-demo.

## Bonus (optional, if credentials + network are confirmed working beforehand): real camera grid

The steps above are the **deterministic, always-works core demo** — run them first, every
time. If time and a pre-flight check permit, this optional add-on shows a genuinely real
external camera, not a local video file:

0. **Pre-flight, before judges arrive**: confirm `backend/.env` has `SENTINEL_GRID_EMAIL`/
   `SENTINEL_GRID_PASSWORD` set, and run one test connection (Cameras → Add Camera →
   `sentinel_grid` source type → Test Connection) to confirm the grid is reachable *today* —
   real internet-relay latency (RTSP handshake measured 10–14s live) makes this worth
   checking once beforehand rather than discovering it live in front of judges.
1. Cameras screen → **SYNC SENTINEL GRID** → show the real discovered catalogue (30
   real cameras, real Ahmedabad locations) appear in the registry.
2. Select one camera → **Start**. Point out: this is a real RTSP connection to a real
   external, authenticated camera grid — not a bundled video file.
3. Live view → real detections streaming from real frames.
4. If a restricted zone is configured on it, show a real alert/incident firing from this
   real feed (same explainable `reasons[]`, same evidence-snapshot pipeline as the core
   demo — nothing special-cased for this source).
5. **If the stream stalls or disconnects mid-demo** (real network — it can happen): say so
   plainly ("that's a real external network hiccup"), open `/{id}/diagnostics` to show the
   real `grid_state` and reconnect counters live, and fall back to the core demo above —
   don't stall on it. This is the honest, designed-for reality behavior, not a bug to hide.
