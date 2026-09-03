# SENTINEL VISION

Unified CCTV Intelligence & Real-Time Smart Policing Platform — built from
`SENTINEL_VISION_Master_Project_Documentation_Gujarat_Police_Innovation_Challenge_2026.docx`
for the Gujarat Police Innovation Challenge 2026.

Real, running system: a FastAPI backend runs actual YOLOv8 detection + ByteTrack tracking +
EasyOCR ANPR against a webcam or an uploaded video file, persists everything to SQLite, and
evaluates a real rules engine that produces explainable alerts and auto-created incidents.
A Next.js dashboard covers the full site map from the doc against that live backend — no
mocked data.

## Run order

1. **Backend** (see `backend/README.md`):
   ```
   cd backend
   .venv/Scripts/python.exe -m uvicorn app.main:app --reload --port 8000
   ```
2. **Frontend**:
   ```
   cd frontend
   npm run dev
   ```
3. Open http://localhost:3000 → log in (`admin` / `sentinel123`) → **Cameras → Add Camera**
   → upload a short video (or use a webcam) → **Live Cameras** to watch real detections stream
   in, or add a **Restricted Zone** / **Watchlist** entry under Map / Watchlists to see the
   real rules engine fire an alert and auto-create an incident, then **Investigate** →
   **Generate Evidence Package**.

## What this is (and isn't)

This is the "working slice" the source document itself recommends for a hackathon build —
real AI on real frames, running end-to-end from ingestion through alerting, investigation and
evidence export — documented against, but not attempting to stand up, the statewide-scale
architecture (80,000+ cameras, Kafka, Kubernetes, vector search, edge Jetson boxes) the same
document describes as the long-term target. See `backend/README.md` → "Scope & honesty notes"
and the in-app **System → Scope & Honesty** panel for the full list of what's real vs.
explicitly out of scope.

## Layout

- `backend/` — FastAPI app, detection pipeline (`app/pipeline/`), SQLite datastore.
- `frontend/` — Next.js 16 (App Router) + TypeScript + Tailwind dashboard, all ~26 screens
  from the doc's site map, wired to the live backend API + WebSocket.

## Known-fixed issues (kept here so they don't get re-introduced)

- **Login crash (bcrypt/passlib)**: `passlib`'s bcrypt backend detection breaks on
  `bcrypt>=4.1`. Fixed by hashing/verifying directly with `bcrypt` in `app/security.py`
  instead of going through `passlib`.
- **Camera creation crash**: `POST /api/cameras` called `asyncio.create_task` from a sync
  route handler running in FastAPI's worker thread (no running event loop there). Fixed by
  making `create_camera`/`restart_camera` and the startup handler `async def`.
- **Evidence download 401**: browsers can't attach a bearer header to a plain `<a href>` /
  `<img>` / new-tab navigation, so `/api/evidence/{id}/file` and the evidence-package endpoint
  401'd when opened directly. Fixed by dropping the auth dependency on those two binary/file
  endpoints only (same accepted trade-off as the MJPEG stream endpoints) — everything else
  stays authenticated.
- **Alert flood**: a tracked object sitting in a zone re-fired a new alert every inference
  cycle. Fixed with a per-(camera, zone/watchlist, track) cooldown in `rules_engine.py`.
- **Map tiles watermarked**: the CARTO dark basemap now requires an API key. Switched to
  key-free standard OpenStreetMap tiles with a CSS `invert()` filter for the dark look
  (`components/CameraMap.tsx`).
- **`next audit` critical CVEs (Next 14.2.16)**: upgraded to Next 16.3.4 + recharts 3 (`npm
  audit` now reports 0 vulnerabilities). React stayed on 18.3.1 — Next 16's peer range still
  accepts React 18, so no React 19 migration was needed.
- **Leaflet map crash after the Next 16 upgrade** (`Map container is already initialized`):
  react-leaflet v4's `MapContainer` doesn't clean up Leaflet's internal `_leaflet_id` on its
  DOM node before React 18 Strict Mode's dev-only double-invoke remounts it — a known
  upstream react-leaflet/Leaflet incompatibility with no clean fix short of a React 19 bump
  (react-leaflet v5). Fixed by setting `reactStrictMode: false` in `next.config.js` (a
  dev-only diagnostic feature; production builds don't double-invoke regardless).
- **Failed fetches looked identical to "no real data" (strict real-data requirement)**: ~26
  places used `.catch(() => {})` or had no error handling at all, so a backend outage, a 403,
  or a network failure rendered the same empty table / stuck spinner / hardcoded-`0` KPI as a
  genuine empty result — silently misrepresenting a failure as real data. Fixed project-wide
  with `lib/useApiData.ts` (tracks `data`/`loading`/`error` honestly) and
  `components/ErrorState.tsx` (a real "Data unavailable" panel with Retry), applied to every
  page and to the header's system-status indicator (which was previously a hardcoded green
  dot, always claiming "System" was healthy regardless of actual connectivity). Verified live
  by killing the backend mid-session and confirming every page shows the real error state
  instead of fake/empty content, then restoring it and confirming normal operation resumes.
