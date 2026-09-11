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

### Docker (reproducible full stack: PostgreSQL + backend + dashboard)

```
cp .env.example .env      # then fill in POSTGRES_PASSWORD and JWT_SECRET
docker compose up --build
```

No secret is baked into any image or compose file. Compose refuses to start
without a real `POSTGRES_PASSWORD`/`JWT_SECRET` rather than defaulting a police
datastore to a guessable credential. The backend runs `alembic upgrade head`
before serving, so the schema is always at head.

### Local development (SQLite, no Docker)

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

## V2: live plate tracking, vehicle journeys, risk and correlation

V2 turns the ANPR path from "OCR every vehicle crop, every frame" into a real
vehicle-intelligence pipeline. Everything below is live in the running system.

**Plate pipeline** (`pipeline/plate_detect.py`, `pipeline/plate_tracker.py`):

- **Plate localization.** OCR previously received the whole vehicle bounding box
  — a car, complete with bumper stickers and dealer badges — and returned
  whichever text fragment won. A plate region is now found inside the crop
  first, via an optional dedicated plate model (`PLATE_MODEL_NAME`, not bundled)
  or a classical edge/morphology localizer that needs no extra asset. A
  localization miss falls back to whole-crop OCR, i.e. exactly the old
  behavior — a miss degrades the read, it never drops it. Measured on the
  benchmark harness: **~3x faster per image** than whole-crop OCR, because
  OCR is working on a plate rather than a vehicle.

  **Correction (2026-09-11), and it matters:** this section previously also
  described localization as the single largest ACCURACY lever available,
  larger than swapping the OCR engine. That was a reasoned hypothesis that had
  never been measured. It has now been measured against 25 real labelled
  Indian plates, and **the opposite is true on that data** — localization
  roughly *tripled* the character error rate and dropped exact-match to near
  zero, while keeping the ~3x speed win. See `docs/ANPR_ACCURACY.md` for the
  numbers, the caveats (n=25, phone photos not CCTV) and what to do about it.
  The speed claim survived measurement; the accuracy claim did not.
- **Track ↔ plate association.** Every tracked vehicle now writes a real
  `Track` row (a table declared in the original schema but never written by
  anything until V2), and its `vehicle_id` is filled in once the plate
  identifies it. "Track 284 IS GJ05AB1234" is a stored fact, not an inference.
- **Confidence voting.** Reads accumulate per `(camera, track)` and vote by
  summed confidence; the reported confidence is the peak actually observed. Four
  reads at 0.72 / 0.91 / 0.94 / 0.89 resolve to `GJ05AB1234 @ 0.94`, and one bad
  frame can no longer overwrite a well-corroborated plate.
- **One sighting per vehicle per camera, not per frame.** Previously a new
  `Plate` row was inserted on every OCR frame — and since a vehicle's journey is
  reconstructed *from* those rows, a car stopped at a signal became dozens of
  duplicate route hops. The row is now created once and updated in place.
- **OCR is throttled by track state.** A settled plate is re-verified on an
  interval instead of every inference cycle — the single largest CPU saving in
  the pipeline.
- **Grammar-based character repair** (`anpr.disambiguate_plate`). Measured with
  `tools/anpr_bench.py`: the dominant real failure was not a wrong plate but a
  character-class confusion in an otherwise perfect read (`GJ05AB1234` →
  `GJO5AB1234`), which then failed the format gate and was silently discarded.
  Indian registrations have a known grammar, so the expected character class at
  each position is known; a substitution is applied only where the grammar
  demands it, only if the result then parses, and **at most twice** — without
  that budget the mapping is strong enough to manufacture `QQ00QQ0000` out of
  noise. A read that already parses is never touched, and confidence is never
  inflated by a repair.

**Vehicle intelligence**: `GET /api/vehicles/by-plate/{plate}` (normalizes input
the same way the pipeline does), `/api/vehicles/{id}/summary` (current/last
camera, journey size, linked alerts and incidents, risk), `/api/vehicles/{id}/route`
(cross-camera journey, consecutive same-camera hops collapsed with real dwell
time), `/api/vehicles/{id}/sightings` (the raw, uncollapsed evidence).

**Explainable risk score** (`pipeline/risk.py`) — 0-100, deliberately *not*
machine-learned. There is no trained risk model behind this system, and an
opaque number would be an unfalsifiable claim. It is a transparent weighted sum
where every point is attributable to a named factor with its real evidence, and
the tests assert that the total always equals the sum of its stated reasons. The
rule-derived severity is a **floor**: the score can escalate an alert, never
quietly downgrade one an explicit rule classified as CRITICAL.

**Event correlation** (`models.IncidentAlert`) — a watchlisted vehicle entering a
restricted zone and then crossing three more cameras is one event that produced
five alerts. Previously that became five incidents. A qualifying alert is now
attached to the open incident it belongs to, and the incident's title,
description and priority grow to describe the whole event. Correlation is
conservative on purpose: it merges only on a hard identity (same recognized
plate) or same-camera-with-no-vehicle, within a bounded window, and never on
visual similarity or proximity. A closed incident is never reopened.

**Live control room** (`/vision`) — replaces a 5-second poll with the real
WebSocket stream. Detections are coalesced into one batch frame per 250ms
(N cameras at their inference rate would otherwise be that many React state
updates per second in every open dashboard); alerts, incidents and plate
identifications are never batched. Events carry canonical `domain.action` names
(`detection.created`, `vehicle.sighting`, `alert.created`, …) and the pre-V2
names are still emitted alongside the low-frequency ones, so no existing
consumer broke.

**Journey replay + investigation** (`/vehicles/{id}`) — vehicle summary, risk
breakdown, journey timeline with play/pause/step, a route map that highlights
the current hop, and the raw sighting evidence. A new sighting for that vehicle
arriving over the WebSocket extends the journey without a reload. The route is
labelled on-screen as a **reconstructed camera-to-camera path** — the system
knows where cameras saw the vehicle and when, and nothing in between; it is
never presented as a GPS track. `is_live` is asserted only from a genuinely
recent sighting, otherwise the UI says "last known".

**Observability** — `/api/metrics` in Prometheus format: detections, OCR
attempts vs accepted vs localized (the gap is the honest fallback rate),
sightings, alerts, incidents split by opened/correlated, inference/OCR/DB-write
latency histograms, lock retries, camera states, WebSocket clients, CPU/memory.
Authenticated by scrape token or Administrator JWT — there is no unauthenticated
mode. GPU memory is *absent* rather than zero on a CPU-only host, so a missing
GPU is never reported as an idle one.

**PostgreSQL** — `DATABASE_URL` selects the datastore; unset keeps the exact
SQLite behavior, so an existing checkout and the whole test suite are unchanged.
Alembic owns the schema on any non-SQLite backend (the additive helpers in
`db.py` emit SQLite DDL and are skipped there). CI verifies that migrations
apply *and* roll back, and that the models have not drifted from them.

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
  (`app/self_heal/`), Prometheus metrics (`app/metrics.py`), Alembic migrations
  (`alembic/`), SQLite (dev) or PostgreSQL (production) datastore.
- `backend/tools/anpr_bench.py` — ANPR benchmark harness. Compares whole-crop vs localized
  OCR, and EasyOCR vs a candidate engine, on a directory of labelled real plate images.
  It reports numbers and deliberately draws no conclusion: a 3% accuracy gain that costs 4x
  the CPU is a different decision on a 30-camera box than on a workstation. No sample
  corpus is bundled — real Gujarat plate footage is not something this repository can ship,
  and a synthetic set would produce a figure that looks like evidence while measuring
  nothing.
- `frontend/` — Next.js 16 (App Router) + TypeScript + Tailwind dashboard, all ~26 screens
  from the doc's site map plus the Camera Control Center and Self-Heal section, wired to the
  live backend API + WebSocket.

## Database architecture & scaling decision

SQLite (`backend/sentinel.db`), WAL journal mode, `busy_timeout=30000` (`app/db.py`) — every
write-heavy call site (camera workers, API routes) goes through the same connection pool, so a
transient lock is absorbed by SQLite's own busy-wait before ever reaching Python, and any lock
that does surface is retried with bounded backoff (see Self-Heal below) rather than crashing.

**V2 update**: the datastore is now selected by `DATABASE_URL`. Leaving it unset
keeps everything below exactly as it was (SQLite, WAL, the same busy-timeout and
retry behavior). Setting a PostgreSQL URL takes the path this section described
as the eventual requirement — the ORM ported without a rewrite, as predicted;
what changed is the connection string, dialect-guarding the SQLite PRAGMAs, and
handing schema ownership to Alembic (`backend/alembic/`), since the additive
`ensure_columns`/`ensure_indexes` helpers emit SQLite DDL and are now skipped on
any other backend. The paragraphs below still describe the SQLite deployment.

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

## 10/10 roadmap gap-closure (2026-09-10)

A competition-readiness pass focused on measurable engineering quality over
architecture changes — see `docs/THREAT_MODEL.md` and
`docs/PRIVACY_GOVERNANCE.md` for the two new standalone docs this produced.

- **Confidence-aware watchlist alerts**: a watchlist match on a plate read
  below `WATCHLIST_HIGH_CONFIDENCE_FLOOR` (default 0.60) is capped at HIGH
  instead of auto-CRITICAL and says so in the alert reason — it still fires
  (never silenced), it just no longer claims the same certainty as a
  confidently-read match (`pipeline/rules_engine.py`, `config.py`).
- **Human-in-the-loop ANPR review**: a Plate sighting below
  `PLATE_REVIEW_CONFIDENCE_FLOOR` is flagged `pending_review`; operators
  accept/correct/reject it via `GET/POST /api/review/...`
  (`pipeline/anpr.py::review_status_for`, `routers/review.py`). Raw OCR,
  grammar-normalized text, and a human correction are kept as three separate,
  always-preserved fields — never overwritten into each other.
- **Alert feedback + precision metrics**: `POST /api/alerts/{id}/feedback`
  (confirmed/false_positive/needs_review) and
  `GET /api/analytics/alert-precision`, which reports `insufficient_sample`
  below a configurable minimum (20) rather than a misleading rate from a
  handful of reviews (`routers/alerts.py`, `routers/analytics.py`).
- **Tamper-evident audit chain**: every `AuditLog` row now carries
  `chain_seq`/`prev_hash`/`entry_hash` — a real (non-blockchain) hash chain,
  verified end-to-end via `GET /api/audit/verify-chain`
  (`app/audit.py`). Detects both a modified row and a deleted row.
- **Evidence provenance completion**: `Evidence.model_version`/`rule_version`
  stamped at capture time; the evidence/incident UI now shows an exact
  `VERIFIED`/`TAMPERED`/`UNVERIFIABLE`/`NO CAPTURE-TIME BASELINE`/`NOT YET
  VERIFIED` badge (`components/EvidenceIntegrityBadge.tsx`) instead of a raw
  status string.
- **Incident Investigator Summary**: `GET /api/incidents/{id}/summary`
  answers what/why/where/evidence/confidence in one call — risk factor
  breakdown, plate + confidence + observation count, watchlist match detail,
  cross-camera route, evidence integrity, related alerts — surfaced as a new
  panel on the incident Overview tab (`routers/incidents.py`,
  `incidents/[incidentId]/page.tsx`).
- **Camera capacity benchmark** (`tools/camera_bench.py`): drives real
  camera workers (real YOLO+ByteTrack+EasyOCR, not a stub) against the
  bundled demo video at configurable concurrency and reports real FPS/
  inference-latency/CPU/RSS. Measured on the development machine this pass
  ran on: 1/3/5 concurrent `video_file` cameras sustained ~7.5-8.4 FPS
  each with inference cost dropping as OS/model caches warmed; CPU plateaued
  near saturation by 3 concurrent cameras on that host. **This is a
  single-host, single-clip measurement — not a claim about any other machine,
  real RTSP streams, or any specific camera count in production.** Re-run it
  on target hardware before sizing a real deployment.

  **Correction (2026-09-11): those numbers are weaker evidence than the above
  implies.** The bundled clip it drives was measured to be **320x240, 10 fps,
  40 frames (4 seconds), 15 KB — and it contains no vehicles at all** (raw
  YOLOv8n at conf>=0.05 finds only a "tv"). Decoding that is nothing like
  decoding a 1080p RTSP stream, and no ANPR work is triggered because nothing
  is ever detected, so the measured cost is close to a floor rather than a
  realistic load. Treat the figures as "the pipeline runs N workers without
  falling over", NOT as a capacity envelope. A real envelope needs real
  1080p footage with actual vehicles in it — see `docs/ANPR_ACCURACY.md`
  ("What is still needed") for what to supply.
- **Disaster recovery test** (`tests/test_disaster_recovery.py`): seeds a
  real incident/evidence/audit-chain, backs up the SQLite file via SQLite's
  own online-backup API, destroys the working copy, restores, and proves
  incidents, evidence hashes, and the audit chain all survive intact.
  PostgreSQL restore uses the same alembic-managed schema but was not
  exercised (no PostgreSQL instance available here) — stated explicitly, not
  implied.
- **Privacy/governance controls**: configurable `EVIDENCE_RETENTION_DAYS`
  (`None` by default — no automatic expiry until explicitly set) and an
  audited, dry-run-by-default, double-confirmed purge workflow
  (`POST /api/governance/purge-expired`) — see `docs/PRIVACY_GOVERNANCE.md`
  for what this is (mechanism) and explicitly is not (a legal-compliance
  claim).
- **ANPR benchmark framework and real Indian-plate temporal fusion/parsing**
  (`tools/anpr_bench.py`, `pipeline/plate_tracker.py`,
  `pipeline/anpr.py::disambiguate_plate`) already existed from an earlier
  pass and were audited, not rebuilt — see those files' own docstrings.
  **ANPR accuracy remains UNVALIDATED**: no labelled real-plate corpus exists
  in this repository (`tools/anpr_corpus/README.md` explains why one isn't
  shipped) — the benchmark is ready the moment real, labelled images are
  added there.

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
- **Backend CI never ran the test suite**: `python-package.yml` ran `pytest` from the
  repository ROOT across Python 3.9/3.10/3.11. The suite lives in `backend/tests` and
  imports `app.*`, so from the root pytest collected nothing and the job passed green
  without executing a single test; the 3.9/3.10 legs could never have worked either, since
  the code requires 3.11. Fixed (pinned 3.11, correct working directory, real system
  libraries) and joined by a frontend workflow — typecheck, production build, and a
  Playwright smoke test against a real backend — where previously there was none at all.
- **Global search could never return an alert**: `routers/search.py` filtered
  `Alert.camera_id.ilike(query)` — matching an opaque internal id column against the
  operator's free text — so that section of a global search was permanently empty. Alerts
  are now found the way an operator looks for them: by the camera they fired on and by the
  vehicle plate involved.
- **Login fields had no accessible labels**: the labels were visually adjacent but not
  associated via `htmlFor`/`id`, so a screen reader announced two unlabelled text boxes.
  Fixed, with `autoComplete` so a password manager can fill an account an operator uses at
  the start of every shift.
- **Batched live events could be lost at shutdown**: the WebSocket batcher's final flush
  lived in the flush task's own `except CancelledError`. A task cancelled before the event
  loop ever scheduled it never enters its body, so its cancellation handler never ran and
  events buffered immediately before shutdown were dropped. The final flush is now done by
  the caller, which covers that case and every other one.
- **The root `.env` was not gitignored**: only `backend/.env` was. Since `.env.example`
  instructs you to create a root `.env` holding `POSTGRES_PASSWORD` and `JWT_SECRET`, that
  was a direct path to committing production secrets.
