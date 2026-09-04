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

1. **Backend**:
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

## Real Sentinel Camera Grid (live-verified)

Beyond the official Gujarat catalogue, this build also integrates a second real, live camera
source — 30 real traffic cameras — with genuine end-to-end verification: discovery, RTSP
connection, real frames, real YOLOv8+ByteTrack detections, real alerts, incidents, and evidence
snapshots. Setup/troubleshooting: put `SENTINEL_GRID_EMAIL`/`SENTINEL_GRID_PASSWORD` in
`backend/.env` (see `backend/.env.example`), then **Cameras → Sync Sentinel Grid** to register
(never auto-starts AI), then **Start**/**Connect** per camera or from the **Camera Control
Center** (see below) — the 24/7 auto-connect supervisor (`app/pipeline/supervisor.py`) keeps
eligible ones reconnected afterward, up to `SENTINEL_GRID_MAX_AUTOCONNECT`.

Credentials go in `backend/.env` only (gitignored — see `backend/.env.example`), **never** in
source, docs, or committed anywhere. They never reach the frontend.

## What this is (and isn't)

This is the "working slice" the source document itself recommends for a hackathon build —
real AI on real frames, running end-to-end from ingestion through alerting, investigation and
evidence export — documented against, but not attempting to stand up, the statewide-scale
architecture (80,000+ cameras, Kafka, Kubernetes, vector search, edge Jetson boxes) the same
document describes as the long-term target. See the in-app **System → Scope & Honesty** panel
for the full list of what's real vs. explicitly out of scope.

## Layout

- `backend/` — FastAPI app, detection pipeline (`app/pipeline/`), Self-Heal recovery engine
  (`app/self_heal/`), SQLite datastore.
- `frontend/` — Next.js 16 (App Router) + TypeScript + Tailwind dashboard, all ~26 screens
  from the doc's site map plus the Camera Control Center and Self-Heal section, wired to the
  live backend API + WebSocket.

## Database architecture & scaling decision

SQLite (`backend/sentinel.db`), WAL journal mode, `busy_timeout=30000` (`app/db.py`) — every
write-heavy call site (camera workers, API routes) goes through the same connection pool, so a
transient lock is absorbed by SQLite's own busy-wait before ever reaching Python, and any lock
that does surface is retried with bounded backoff (see Self-Heal below) rather than crashing.

**Verified acceptable for this deployment shape** — a single backend process, a bounded number
of concurrent camera workers (real-camera testing: staged up to the documented safe concurrency
figure; synthetic stress test: 12 concurrent workers, real detection/heartbeat/alert writes,
zero crashed workers — see `backend/tests/test_stress_concurrency.py`). **Not** appropriate
once the deployment needs multiple backend *processes/instances* sharing one datastore (SQLite
has no real concept of a remote/networked writer) or the statewide-scale (80,000+ camera)
architecture the source document describes as the long-term target. That path is
**PostgreSQL** — a separate, dedicated migration task (schema is already a plain SQLAlchemy
ORM, so the model layer ports without a rewrite; what changes is the connection string, the
SQLite-specific PRAGMAs in `app/db.py`, and the additive-migration helpers in `db.py` which
assume SQLite's `ALTER TABLE`/`inspect()` behavior) — never mixed into a stability/hardening
pass, and not attempted here.

## Self-Heal (`backend/app/self_heal/`)

Observes and logs the platform's real recovery paths — it does not re-implement or override
them: SQLite lock retry (`pipeline/db_retry.py`), camera reconnect/backoff
(`pipeline/worker.py`), outbound HTTP retry for this app's own camera-catalogue fetch
(`self_heal/http_retry.py` — deliberately NOT applied to the Sentinel Grid client, whose
timeouts are already real-measured/tuned). Every recovery attempt is a `SelfHealEvent` row
(`GET /api/self-heal/health|problems|events|events/{id}`), broadcast live over the existing
WebSocket. A repeat "recovered" event for the identical ongoing condition within a short window
is deduplicated so the Error Log doesn't drown in identical rows; a genuine failure is never
deduplicated. UI: sidebar **Self-Heal** section (Health Dashboard, Problems, Recovery Activity,
Camera Health, Error Logs, Problem Details).

**Known, honest limitation**: OpenCV/FFmpeg exposes no structured H264/decode-error signal to
this codebase — a corrupted frame just makes `cv2.VideoCapture.read()` return `False`. Self-Heal
therefore labels a stream disruption `STREAM_READ_FAILURE` (or `CAMERA_CONNECT_FAILURE` for an
initial-connect failure), never a fabricated "H264 decoder" diagnosis — real ffmpeg stderr lines
(`error while decoding MB...`, `mmco: unref short failure`, etc.) still appear in the server log
as FFmpeg's own diagnostic output, just not parsed/re-classified by Self-Heal.

## Camera Control Center (`/cameras/control`, `POST /api/cameras/bulk`)

Bulk connect/start/start AI/stop/restart/disconnect — reuses the existing per-camera
start_worker/stop_worker/supervisor connect/disconnect, bounded concurrency (max 5 at once),
per-camera failure isolation (one bad camera never aborts the batch), a duplicate-in-progress
guard, one audit-log entry per bulk call, and live per-camera progress over the WebSocket.
`stop` disables AI while keeping the stream connected; `disconnect` fully stops the worker;
`connect`/`start` are honest aliases — this codebase has no real distinction between them.
RBAC is enforced server-side (`require_roles("Administrator", "Control Room Operator")`) —
a disabled frontend button is a convenience, not the security boundary.

## Known-fixed issues (kept here so they don't get re-introduced)

- **Login crash (bcrypt/passlib)**: `passlib`'s bcrypt backend detection breaks on
  `bcrypt>=4.1`. Fixed by hashing/verifying directly with `bcrypt` in `app/security.py`
  instead of going through `passlib`.
- **Camera creation crash**: `POST /api/cameras` called `asyncio.create_task` from a sync
  route handler running in FastAPI's worker thread (no running event loop there). Fixed by
  making `create_camera`/`restart_camera` and the startup handler `async def`.
- **Evidence download 401**: browsers can't attach a bearer header to a plain `<a href>` /
  `<img>` / new-tab navigation, so `/api/evidence/{id}/file` and the evidence-package endpoint
  401'd when opened directly. Originally "fixed" by dropping auth on those endpoints entirely —
  that made police evidence fetchable by anyone with an ID. Replaced with short-lived signed
  resource tokens instead (`security.create_resource_token` / `get_user_from_resource_token`):
  the frontend fetches a token via an authenticated `.../file-token` (or `.../stream-token`,
  `.../package-token`) request first, then appends it as `?token=` on the actual file/stream
  URL. Same pattern now covers the MJPEG/snapshot endpoints too — nothing evidence- or
  camera-feed-related is unauthenticated anymore, and every access is attributed to the real
  user in the audit log.
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
- **Shared tracker state across cameras**: `detector.py` cached a single YOLO model instance
  (`lru_cache(maxsize=1)`) reused by every camera's worker; since `model.track(persist=True)`
  keeps ByteTrack state on that shared object and each camera's inference runs on its own
  thread (`asyncio.to_thread`), concurrent cameras could race and corrupt each other's track
  IDs. Fixed with one YOLO/tracker instance per camera (`_MODELS_BY_CAMERA` dict, released on
  `stop_worker`).
- **No real reconnect on a dropped stream**: `worker.py`'s camera loop set `status="degraded"`
  on a bad read and just kept looping forever with a 1s sleep — it never actually reopened the
  source. Fixed with a real release+reopen retry using exponential backoff
  (`reconnect_max_attempts`/`reconnect_backoff_base`/`_max`); after the retry budget is
  exhausted the camera is marked `offline` and the worker stops (an operator's Restart brings
  it back) instead of spinning "degraded" indefinitely.
- **RTSP was a defined-but-unimplemented source type**: now routed through the same
  `cv2.VideoCapture` path as `video_file` via OpenCV's FFmpeg backend, with open/read timeouts
  so a dead stream fails fast. Best-effort (no ONVIF discovery, no real CCTV/VMS available to
  test against here) but real, not a stub.
- **ANPR correlated on any non-empty OCR read**: `looks_like_plate()` (Indian plate-format
  regex) existed in `anpr.py` but was never called, so a 2-character garbage OCR read became a
  real `Vehicle`/`Plate` row. Fixed by gating the correlation write on `looks_like_plate()` AND
  a minimum confidence (`plate_min_confidence`, default 0.35) — noisy reads are simply not
  persisted as a vehicle sighting, rather than trusted.
- **Upload endpoint trusted the client filename**: `POST /api/cameras/upload-video` wrote
  straight to `uploads_dir / file.filename` (path-traversal/overwrite risk, no extension
  check, no size cap, whole file read into RAM first). Fixed with a server-generated UUID
  filename, an extension allow-list, a streamed chunked write with a size cap
  (`max_upload_mb`), and cleanup of any partial file on failure.
- **Evidence package claimed an audit trail it didn't include**: the docstring said "audit
  trail" but the returned JSON never actually queried `AuditLog`. Fixed — it now includes the
  real `AuditLog` rows touching that incident or any of its evidence items.
- **SQLite lock during `db.flush()`, not just `commit()`**: `worker.py`'s `db.add(det_row);
  db.flush()` (assigns a detection's identity before the rest of the frame's processing) was
  completely unguarded — a lock there escaped to the outer per-iteration `except`, silently
  dropping that detection instead of retrying like every other write. Fixed with
  `db_retry.safe_flush` (same rollback→reapply→bounded-backoff→retry as `safe_commit`) — see
  `backend/tests/test_db_concurrency.py`.
- **WebSocket reconnect had no backoff**: `useLiveSocket.ts` retried a dropped connection every
  1s forever. Fixed with bounded exponential backoff (1s→30s cap, resets on a real reconnect);
  also guarded `onopen`/`onmessage` against updating state from a socket that's already closing
  during unmount (a real, if rare, stale-update race).
- **Frontend API calls had no retry for transient failures**: `lib/api.ts` now retries GET
  requests (never POST/PATCH/DELETE — those may have already taken effect server-side) on
  408/429/500/502/503/504 or a network failure, bounded to 3 attempts.
- **Camera Control's per-row action menu stayed open until another item was clicked**: fixed
  with a real click-outside listener (`RowActionsMenu` in `cameras/control/page.tsx`).
