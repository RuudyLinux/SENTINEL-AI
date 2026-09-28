# SENTINEL VISION

Unified CCTV Intelligence & Real-Time Smart Policing Platform — built from
`SENTINEL_VISION_Master_Project_Documentation_Gujarat_Police_Innovation_Challenge_2026.docx`
for the Gujarat Police Innovation Challenge 2026.

Real, running system: a FastAPI backend runs actual YOLO11 detection + ByteTrack tracking +
EasyOCR ANPR against live RTSP cameras, a webcam or an uploaded video file, persists
everything to PostgreSQL + PostGIS (docker compose) or SQLite (single-machine development),
and evaluates a real rules engine that produces explainable alerts and auto-created
incidents. A Next.js dashboard covers the full site map from the doc against that live
backend — no mocked data. Every component is open source (next section).

## Built on open source

Every runtime component is under an OSI-approved open-source licence. Verified on
2026-09-28 by reading the installed package metadata: every Python distribution, and
every npm package the frontend ships at runtime (`npm ls --omit=dev`).

| Layer | Technology | Licence | Where |
|---|---|---|---|
| Dashboard | React 18, Next.js 16, Tailwind | MIT | `frontend/` |
| Maps, GIS, route display | Leaflet 1.9 + OpenStreetMap tiles | BSD-2-Clause (data ODbL) | `frontend/components/CameraMap.tsx` |
| Live video, low latency | WebRTC via WHEP (browser `RTCPeerConnection`) | W3C/IETF standard | `frontend/lib/whep.ts`, `components/WhepVideo.tsx` |
| Live video with AI overlay | MJPEG over HTTP | standard | `backend/app/routers/streams.py` |
| API and AI backend | Python 3.11, FastAPI, Uvicorn, SQLAlchemy, Alembic | MIT / BSD | `backend/app/` |
| Camera input | RTSP over TCP, decoded by FFmpeg (through OpenCV) | LGPL / Apache-2.0 | `backend/app/pipeline/adapters.py` |
| Evidence clips | FFmpeg (libx264) | LGPL / GPL | `backend/app/pipeline/clips.py` |
| Detection and tracking | PyTorch, Ultralytics YOLO11s, ByteTrack | BSD / AGPL-3.0 | `backend/app/pipeline/detector.py` |
| Plate detection | YOLOv11 licence-plate model (morsetechlab) | AGPL-3.0 | `backend/app/pipeline/plate_detector.py` |
| OCR | EasyOCR | Apache-2.0 | `backend/app/pipeline/anpr.py` |
| Database | PostgreSQL 16 + PostGIS 3.5 | PostgreSQL / GPL-2.0 | `docker-compose.yml`, `backend/app/geo.py` |
| Shared runtime state | Valkey 8 (Redis protocol) | BSD-3-Clause | `docker-compose.yml`, `backend/app/runtime_state.py` |
| Metrics | Prometheus client | Apache-2.0 | `backend/app/metrics.py` |
| Evidence PDF | ReportLab | BSD | `backend/app/routers/evidence.py` |

Two things were not open source until 2026-09-28 and were replaced:

- **react-leaflet** (the React wrapper around Leaflet) is under the Hippocratic 2.1
  licence, which restricts use and is not OSI-approved. The map now uses Leaflet
  directly; behaviour is unchanged.
- **`redis:7-alpine`** in compose had become Redis 7.4, licensed RSALv2/SSPL, neither
  OSI-approved. Compose now runs Valkey, the Linux Foundation's BSD-licensed fork. The
  app speaks the same protocol through the MIT `redis` Python client and needed no
  change. The Redis-backed test suite (18 tests, always skipped before for want of a
  server) passes against Valkey 8.1.

Ultralytics YOLO and the plate model are AGPL-3.0, a copyleft licence: distributing
this system, or offering it as a network service, carries AGPL obligations. The
repository does not yet declare its own licence; that is the owners' decision.

**What each listed technology does here, and what was deliberately not added.**

- **PostgreSQL + PostGIS** is the deployment database. PostGIS answers
  `GET /api/cameras/nearby` (cameras within a radius, nearest first) with one
  GiST-indexed `ST_DWithin` query over a generated `cameras.geog` column (migration
  `20260928_0700`); the incident page uses it to list the cameras within 1 km of the
  incident. `backend/tools/postgis_verify.py` proves this against a real PostGIS 3.5
  server: the index is used, and the results match the haversine fallback that SQLite
  and PostGIS-less PostgreSQL use. SQLite remains only for zero-install development
  and the test suite.
- **RTSP** is how every real camera is read (the government camera grid included).
- **FFmpeg** decodes every RTSP stream and encodes every evidence clip. **GStreamer**
  would duplicate it and was not added.
- **WebRTC**: the official camera catalogue gives each camera a WHEP URL next to its
  RTSP one. The live camera page plays it directly in the browser (low latency, no
  backend load) with **LOW LATENCY (WEBRTC)**, and falls back to MJPEG if it fails.
  MJPEG stays the default because only it carries the AI boxes. Tested in Chromium
  with real grid footage re-streamed by MediaMTX (MIT): 1080p, first frame in 840 ms.
  The Sentinel Grid gives no WHEP URLs, so its cameras show MJPEG only.
- **Kafka / RabbitMQ were not added.** Detections go from the camera loop to the
  rules engine in-process, and to dashboards over one WebSocket with batching. At
  this deployment's scale (one backend, at most 2 AI cameras per GPU), a broker would
  add a service to run and a network hop per detection and solve no measured
  problem. It belongs in the multi-site design, between edge inference nodes and the
  central platform, where producers and consumers are different machines.
- **TensorFlow** is not used; the models are PyTorch. **OpenLayers** would duplicate
  Leaflet. **Node.js** runs the Next.js server.

## Run order

### Docker (reproducible full stack: PostgreSQL + PostGIS, Valkey, backend, dashboard)

```
cp .env.example .env      # then fill in POSTGRES_PASSWORD and JWT_SECRET
docker compose up --build
```

No secret is baked into any image or compose file. Compose refuses to start
without a real `POSTGRES_PASSWORD`/`JWT_SECRET` rather than defaulting a police
datastore to a guessable credential. The backend runs `alembic upgrade head`
before serving, so the schema is always at head.

**Verification status: `docker compose up --build` has now actually been run,
on 2026-09-23, and it works.** Both images build, all three containers report
`healthy`, the backend serves `/api/health` on port 8000, the dashboard serves
on port 3000, `alembic upgrade head` ran against real PostgreSQL and produced
all 17 expected tables, and the API is reachable end to end through it.

That run also found and fixed a real defect: the backend image's
`pip install` pulled the DEFAULT PyPI build of torch, which bundles the full
CUDA runtime as separate wheels — one of them alone is 214MB — for an image
that has no GPU runtime (`python:3.11-slim` base, no CUDA toolkit installed,
no GPU requested anywhere in compose). On a real network that download
occasionally timed out outright (`pip`'s `ReadTimeoutError` mid-download),
which is how this was found rather than merely inferred. Fixed by pointing
pip at PyTorch's CPU-only wheel index for the build — `torch-2.14.0+cpu` at
196MB versus a multi-gigabyte CUDA install for a container that could never
have used it. See the Dockerfile's own comment at that line for the reasoning
in full.

Login with the documented `admin`/`sentinel123` correctly FAILS against this
compose deployment — that is `DEMO_MODE=false` (the compose default, meaning
production mode) doing exactly what its own comment says: no seeded demo
accounts, ever, on a real deployment. Zero rows in `users` after a fresh
`docker compose up` is the correct starting state, not a bug; provisioning
the first real administrator is a separate, deliberate step this README does
not yet document — flagged here rather than worked around.

`backend/tests/test_deployment_contract.py` (16 tests in the normal suite)
covers the contract between these files and the application without needing
a real build: every file the images COPY exists, every build context exists,
the evidence and uploads volumes are mounted where
`settings.evidence_dir`/`uploads_dir` actually write (a mismatch would
silently discard captured evidence on the next rebuild), every environment
key compose sets is one the settings object really reads, both healthchecks
poll routes that exist, the schema is migrated before uvicorn starts, and the
secrets have no guessable defaults. That drift protection is what keeps this
claim from going stale between real runs — it is not a substitute for one.

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
   For live code editing only. `start.bat` serves the production build
   (`npm run build && npm run start`), which is what a demo should use.

3. Open http://localhost:3000 → log in (`admin` / `sentinel123`) → **Cameras → Add Camera**
   → upload a short clip of **real traffic footage** (or use a webcam; the bundled demo clip is
   a synthetic rectangle and produces no detections) → **Live Cameras** to watch real detections stream
   in, or add a **Restricted Zone** / **Watchlist** entry under Map / Watchlists to see the
   real rules engine fire an alert and auto-create an incident, then **Investigate** →
   **Generate Evidence Package**.

**Demo procedure (real grid cameras).**

1. With `SENTINEL_GRID_AUTOCONNECT=true` (the default) every grid camera
   connects at startup and reconnects on its own; AI rotates across them (see
   *AI capacity guard*). Set it to `false` to connect cameras by hand.
2. Run `start.bat`. It starts the backend on the GPU environment
   (`backend/.venv-gpu`) when CUDA works there and on the CPU environment
   (`backend/.venv`) otherwise, and prints which. It builds and serves the
   production frontend. The AI rate and AI-camera limit follow the hardware:
   2 AI cameras at every frame with CUDA, 1 at every 3rd frame on CPU.
3. **Camera Control** → connect one camera (GRID-cam02 was the stable test camera)
   and check the live view is moving and the card shows **AI RUNNING**.
4. Draw a **small** restricted zone over one lane. A zone covering a busy junction
   raises an alert for every vehicle: 320 CRITICAL alerts in 10 minutes on cam02
   in the 2026-09-28 run, each with its own snapshot and clip.
5. Wait for the alert. CRITICAL alerts open an incident. Open it and check the
   snapshot and clip, press **Verify** (SHA-256), then check **Admin → Audit**.
6. ANPR only reads plates that are large and sharp in the frame. On this grid, most
   plates are not readable at source resolution (see `docs/AI_ACCURACY.md`), so a
   plate result is not guaranteed. Low-confidence or uncorroborated reads show as
   *pending review*. GRID-cam06 (daytime, riders close to the lens) is the only
   grid camera where a plate was read correctly in the 2026-09-28 check.
7. Never run **Demo reset** on a database with real footage.

**If port 8000 is taken.** It is a popular default, and an unrelated local
service holding it is not a hypothetical — it happened here, and the dashboard
reported only "Login failed" while talking to a stranger's API. Run both
processes on a free port instead:

```
cd backend && .venv/Scripts/python.exe -m uvicorn app.main:app --reload --port 8008
cd frontend && NEXT_PUBLIC_API_BASE=http://localhost:8008 NEXT_PUBLIC_WS_BASE=ws://localhost:8008 npm run dev
```

On PowerShell set them first (`$env:NEXT_PUBLIC_API_BASE="http://localhost:8008"`),
and note these are read at BUILD time — for `next build` output, change them
and rebuild; a restart alone will not pick them up.

The login screen now refuses to be silent about this: it calls `/api/health`
before you type anything and checks the returned service name, so a wrong or
missing backend is named on the form, and the API base the build was compiled
against is printed underneath it.


## V2: live plate tracking, vehicle journeys, risk and correlation

V2 turns the ANPR path from "OCR every vehicle crop, every frame" into a real
vehicle-intelligence pipeline. Everything below is live in the running system.

**Plate pipeline** (`pipeline/plate_detector.py`, `pipeline/plate_preprocess.py`,
`pipeline/plate_tracker.py`; `pipeline/plate_detect.py` remains as the
compatibility facade older callers import):

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
- **State/UT code validation.** The format regex alone accepts any two letters,
  so `QQ00QQ0000` and `XX12AB1234` were "valid plates" — OCR noise in the right
  shape cleared the quality gate and became a real `Vehicle` row. The prefix must
  now be a code an Indian state or union territory actually issues (legacy codes
  like `OR`/`TS`/`UA` included, because those vehicles are still on the road),
  and the Bharat series (`23BH1234AA`) is matched by its own grammar. Measured
  effect on the labelled corpus: plate-shaped-but-wrong reads fell from 0.64 to
  0.52 of images, with exact match unchanged at 0.24 — fewer false identities at
  no cost to correct reads.
- **Preprocessing variants** (`pipeline/plate_preprocess.py`) — perspective
  correction for off-axis plates, plus grayscale/CLAHE/denoise/sharpen/threshold
  renderings of the same crop. **Off by default**: each variant is a full extra
  OCR pass, and the measured exact-match difference is one sample out of 25.
  `PLATE_PREPROCESS_VARIANTS` opts in.
- **Selection by agreement, never by maximum confidence.** When several variants
  are read, the winner is the text most of them produced, not the one with the
  highest score. Max-of-N is a biased estimator — it is systematically larger
  than any single read — so reporting it would inflate every recorded confidence
  and silently loosen the gates. Reported confidence stays the mean over the
  agreeing reads; corroboration is reported separately as `variants_agreeing`.
  Measured: agreement separates correct from wrong reads far more cleanly than
  confidence does (≤2 of 7 variants agreeing: 0 of 13 correct; ≥5 of 7: 4 of 4).
- **Multi-variant reading can only TIGHTEN the gate, never loosen it.** A read
  with high confidence but only one variant agreeing is not corroborated and is
  refused.
- **Temporal consensus gates persistence.** A plate becomes trusted intelligence
  only once `PLATE_MIN_OBSERVATIONS` frames agree on it. Previously the first
  gate-passing read created a `Vehicle` outright. An uncorroborated read is still
  recorded — a vehicle crossing frame in one inference cycle is real — but is
  flagged `pending_review` regardless of its confidence rather than presented as
  settled.

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
snapshots. Setup: put `SENTINEL_GRID_EMAIL`/`SENTINEL_GRID_PASSWORD` in `backend/.env` (see
`backend/.env.example`), then **Cameras → Sync Sentinel Grid** to register. The 24/7 auto-connect
supervisor (`app/pipeline/supervisor.py`) then keeps every registered camera connected on its own
— up to `SENTINEL_GRID_MAX_AUTOCONNECT` (default 100, covering the whole catalog), one connection
at a time with a real delay between each (`SENTINEL_GRID_STAGGER_SECONDS`) so a restart or a fresh
sync never opens dozens of simultaneous RTSP handshakes against the external grid at once — that
burst, not local CPU/RAM, is what the external grid's own connection tolerance actually limits.

**Default operating posture: always connected, AI always on.** A freshly discovered grid camera
now starts with `ai_person`/`ai_vehicle`/`ai_anpr` all `True` (matching every other camera-creation
path in this app), so the supervisor connecting it also means it is under real detection from the
moment it comes up — no separate "Start AI" step. "Connected" and "AI processing" remain two
independent fields under the hood (the supervisor itself never writes the AI flags, only the
*default value* a new camera gets them changed), so an operator can still turn AI off for any one
camera via **Cameras → Edit** or `PATCH /api/cameras/{id}` without affecting the rest of the fleet.

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
  (`alembic/`), PostgreSQL + PostGIS (docker compose) or SQLite (development) datastore,
  GIS queries (`app/geo.py`).
- `backend/tools/postgis_verify.py` — proves the PostGIS path on a real server.
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

## Final submission audit fixes (2026-09-27)

- **Resource tokens were full session tokens.** The short-lived tokens put in evidence/package/
  stream URLs share the JWT secret and `sub` claim with login tokens, and nothing checked their
  `scope` claim, so any leaked `?token=` (one-hour stream tokens included) worked as a bearer
  token on every API route and on `/ws`. `get_user_from_token` now rejects scoped tokens.
- **The Auditor role could change what it audits.** Alert acknowledge/escalate/dismiss/feedback,
  incident create/note/assign/close, and plate-review accept/correct/reject accepted any
  logged-in user. They now require an operational role (Administrator, Control Room Operator,
  Investigator, Supervisor). The Auditor keeps read access.
- **Disabling a rule did nothing.** Watchlist and zone-entry alerts ignored `AlertRule.active`.
  They still fire when no rule row exists (the default), but once rules exist and every one of
  that type (for zone entry: that zone) is disabled, the alert stops.
- `/ws` now drops a client from the broadcast list on any exit, not only a clean disconnect.
- **Camera workers held SQLite's write lock through OCR.** A vehicle's Detection row was
  flushed, then ANPR ran (seconds on a loaded CPU, plus a one-off ~8s EasyOCR model load),
  then the row was committed. Every other writer queued behind it. Measured with three
  cameras running: camera creation failed with "database is locked" after the 30s busy
  timeout, evidence clips for alerts were lost, and audit writes surfaced as 500s. OCR now
  runs before the flush (`worker._anpr_ocr`). After the fix: 20 API writes under the same
  load took 10-40ms each, and there were zero lock errors in the log.
- Evidence clip rows are written from a worker thread rather than on the event loop, and
  `audit.log_action` retries a locked database instead of failing the audited request.
- Investigate → **Export Report** was a plain link without the resource token the package
  endpoint requires, so it always failed. Evidence **Download** reused a token minted at page
  load, which expired 5 minutes later. Both now mint a fresh token per click.
- Clickable table rows (alerts, incidents, evidence, cameras) are keyboard-operable, and the
  Watchlist, Rules and Person-tracking form labels are linked to their fields.
- **`/ws` outlived its token.** The session token was checked once at the handshake, so a
  socket kept receiving live events after the token expired. The socket now closes with 4401
  at the token's `exp`, and the dashboard reconnects (with backoff) using its current token.
- **A deleted camera's failure stayed on Self-Heal → Problems.** Deleting a camera removed its
  recovery-log rows but not the in-memory open-problem index, so the problem lingered until
  a restart. The index is now cleared on delete.

**Judge-demo footage.** The bundled `app/demo_assets/car-detection.mp4` that the two demo
cameras (C-014, C-019) play is a synthetic moving rectangle. It proves decoding and
streaming work anywhere, but YOLO correctly detects nothing in it. On those cameras the
only vehicle identity is the one **Trigger scenario** injects (see
`pipeline/demo_scenario.py`). To show live detection and tracking, add a camera with real
traffic footage (**Cameras → Add Camera → upload**).

**Capacity.** Each AI camera runs its own YOLO instance with 8 torch threads. On the 16-core
test machine, three AI cameras (one of them 1080p real footage with ANPR) held the CPU at
~100%, and camera test-connection probes then hit their 20s timeout. Stopping the heavy
camera brought the probe back to 0.27s. For a live demo, keep to two or three AI cameras.

**ANPR validation.** The ANPR pipeline (plate localization, preprocessing variants, EasyOCR,
Indian-format validation, temporal voting) is covered by unit and integration tests. It has
not been validated end to end on real Indian number-plate footage in this build. The
measured accuracy caveats in `docs/ANPR_ACCURACY.md` apply, and no real-world accuracy
figure is claimed.

**Sessions.** Login tokens (8h) are held in browser `localStorage`. Logout clears them on
the client only: there is no server-side revocation, so a copied token stays valid until it
expires. Disabling a user does take effect immediately, because every request and every
WebSocket handshake re-checks that the account is active. Revocation lists and cookie-based
sessions are deliberately out of scope for this build.

**Deployment.** SQLite (the default) suits a single-machine demo with a handful of cameras,
and PostgreSQL is the supported production datastore. Statewide scale is a design target,
not something this build has been tested at. The master project document's scenario script
(C-014 → C-019 → C-027 with a live plate read) is the design plan: the implemented **Trigger
scenario** covers C-014 → C-019 with an injected plate read, as described above.

Regression tests: `backend/tests/test_submission_audit_fixes.py`.

### Real-camera fixes (2026-09-27, measured on the government grid)

- **Live view fell behind real time.** The camera loop read a frame, processed it, then read
  the next, so a 25-30fps stream consumed at ~5fps queued up: the picture ran at ~0.2x real
  time (the camera's clock advanced 11s per 60s). A per-session reader thread
  (`pipeline/frame_reader.py`) now decodes continuously and keeps only the newest frame. The
  loop always takes the newest one and older frames are dropped. After the fix, in a healthy
  grid window, the camera clock advanced 61s in 60s with two AI cameras running; frame age at
  processing is typically under 100ms. The grid itself sometimes stalls and then flushes
  bursts of frames; the app shows the newest frame it has received and cannot show more.
- **Retiring a camera.** `POST /api/cameras/{id}/retire` (Administrator, button on Cameras)
  takes a camera out of service without deleting its history, which a camera with evidence
  cannot be (delete returns 409). A retired camera never connects, starts, auto-starts at boot,
  or counts in statistics. Its alerts, incidents and evidence stay intact.
  `POST /{id}/reinstate` undoes it. C-014 and C-019 (the synthetic demo cameras) are retired.
- **Stale "online" after a restart.** At boot every camera is set offline until its worker
  reports in; a hard kill previously left the dashboard reading "4/34 online" with 2 running.
- **AI rate vs CPU.** Inference runs on every Nth frame the loop takes (`DETECT_EVERY_N_FRAMES`,
  default 3). Measured with 2 AI cameras: N=3 gives ~0.9 AI fps per camera at ~52% CPU; N=1
  gives ~2.2 AI fps per camera at ~98% CPU. Tracking is better at higher AI fps.
- **Demo reset wipes data.** `POST /api/system/demo/reset` deletes ALL detections, plates,
  vehicles, alerts, incidents and evidence, including data from real cameras. Do not run it
  on a database holding real footage.
- **Clip encoding ran the machine out of memory — fixed.** The encoder decoded every
  buffered frame before writing any: ~750 MB for one 15s 1080p clip at GPU frame rates, and a
  busy zone raises several clips at once. The live test process died with an OpenCV
  "Insufficient memory" error. Frames are now decoded one at a time as they are written, and at
  most 2 clips encode at once. The same busy zone then produced 48 alerts and 39 clips in under
  2 minutes at 1.6 GB RSS.
- **Tokens in access logs — fixed.** uvicorn logged full URLs, including the `?token=` of the
  WebSocket handshake and stream/evidence links. `app/log_redaction.py` now redacts token values
  on uvicorn's own loggers however it is launched; verified on a live log (43 WebSocket
  handshakes and every stream request: 0 tokens written).

### Measured accuracy

Superseded by **`docs/AI_ACCURACY.md`** (2026-09-28): a reproducible benchmark on
24 audited real grid frames. Summary:

- **Detector:** yolo11s at 960px, conf 0.30. Vehicle P 0.860 / R 0.508 and person
  P 0.959 / R 0.538, against 0.842 / 0.354 and 1.000 / 0.288 for the previous
  yolov8s at 640px. Night preprocessing was tested and made every metric worse,
  so none is applied.
- **Tracker:** tuned ByteTrack (`app/pipeline/bytetrack_sentinel.yaml`). At the
  live AI rate, day track fragments fell 30% to 0% and night 40% to 35%.
- **ANPR:** a trained plate detector located plates on 9 of 17 real vehicles
  (classical: 2). On the 3 plates readable at source resolution, character
  accuracy rose from 0 to 0.58, with no wrong plate published. **No plate was
  read fully correctly.** A plate-specific OCR model was tested, and rejected
  because it published confident wrong plates.
- **Capacity:** the selected configuration uses one AI camera's worth of this
  CPU. For two cameras use `DETECTOR_IMGSZ=640`, or the GPU runtime below.
- **Tracking ID merges:** ByteTrack matches by position only, and on night
  footage a lost car's ID was picked up by a motorbike rider. An ID whose object
  changes kind (car/bus/truck vs person/motorbike) now gets a new ID
  (`pipeline/detector.py`). Rider and bike trading an ID stay one object.
  Same-kind merges (car to car) are not detected.

### AI capacity guard

`MAX_AI_CAMERAS` caps how many cameras run AI at once. Unset, it is 1 on CPU
and 2 when CUDA is available; `DETECT_EVERY_N_FRAMES` likewise defaults to 3 on
CPU and 1 with CUDA. An explicit value in the environment always wins. Connecting
more cameras is allowed. They stream live video without AI while they wait,
and the camera card shows **AI WAITING**.

The slots rotate: a camera that has had one for `AI_ROTATION_SECONDS` (default
60) hands it to the camera that has waited longest, so with every camera
connected each gets AI in turn (30 cameras on 2 slots: 60s of AI about every
15 minutes). A camera that loses its turn frees its model, since a 4 GB GPU
can't hold one per camera. `AI_ROTATION_SECONDS=0` gives fixed slots, where
starting AI on a full machine is refused with *"AI capacity limit reached"*.
The worker enforces the slot check, so no path (bulk action, PATCH, restart,
supervisor) can exceed it. A worker that stops or stalls frees its slot.
`GET /api/cameras/diagnostics/system` reports `ai_device`, `ai_cameras`,
`ai_waiting` and `max_ai_cameras`.

### Recording (REC)

The **REC** button on a camera's live page records the view with AI boxes to
an H.264 MP4 at `RECORDING_FPS` (10). It stops when pressed again, after
`RECORDING_MAX_SECONDS` (30 min), when the camera stops, or when frames stop
arriving for 15s, and is then saved as SHA-256-hashed evidence
(`evidence_type="recording"`). At most `RECORDING_MAX_CONCURRENT` (4) run at
once. Administrator and Control Room Operator only; starts, stops and saves are
audited.

### Zone alert thresholds

Zone alerts need a detection confidence of `ZONE_ALERT_MIN_CONFIDENCE` (0.40)
and, for a tracked object, `ZONE_ENTRY_MIN_FRAMES` (2) inference frames inside
the zone, so a one-frame ghost box doesn't raise an alert. Lower-confidence
boxes are still tracked, stored and drawn. `ZONE_ENTRY_MIN_FRAMES=1` restores
the old behaviour. Not yet measured on labelled footage.

### GPU runtime (optional, measured 2026-09-28)

The default `backend/.venv` is CPU-only and stays that way. A separate CUDA
environment runs the same code on an NVIDIA GPU (measured on an RTX 3050 Ti
Laptop, 4 GB):

```bash
cd backend
uv venv .venv-gpu --python 3.11
uv pip install --python .venv-gpu/Scripts/python.exe torch torchvision --index-url https://download.pytorch.org/whl/cu128
uv pip install --python .venv-gpu/Scripts/python.exe -r requirements.txt
```

`start.bat` uses this environment automatically when CUDA works in it, and falls
back to `.venv` otherwise. YOLO and EasyOCR select CUDA automatically, and the
AI rate and camera limit follow (see *AI capacity guard*).

Re-measured live on GRID-cam02, 2026-09-28, after the event-loop fix below (no
alert rule active):

| GRID-cam02, live | AI fps | YOLO inference | System CPU | Errors |
|---|---|---|---|---|
| CPU (`.venv`), every 3rd frame, 3 min | 1.6 | 121 ms | ~58% median, peaks 99% | 0 |
| GPU (`.venv-gpu`), every 3rd frame, 3 min | 3.5 | 36 ms | ~7% | 0 |
| GPU, every frame, 2.5 min | 5.8 | 25 ms | ~11% | 0 |
| GPU, every frame, cam02 + cam06, 3 min | 6.6 (cam02) / 3-9 (cam06) | 23 ms | ~18% | 0 |

With a full-frame CRITICAL zone on cam02 (an alert about every 2 seconds, each
encoding a clip) the GPU run fell to ~2 AI fps over 10 minutes. See
*Performance and runtime pass* below.

Earlier measurement (before that fix):

| Real grid camera(s), `DETECT_EVERY_N_FRAMES=1` | AI fps / camera | Inference | App CPU | GPU util | Frame age |
|---|---|---|---|---|---|
| CPU, 1 camera | 2.0 | 126 ms | ~92% of machine | — | 219 ms |
| GPU, 1 camera | 8.2 | 16 ms | 22% of machine | 25-48% | 78 ms |
| GPU, 2 cameras | 6.2 / 6.9 | 26 / 38 ms | 32% of machine | 27-64% | <100 ms |

Detection results on the benchmark are identical on CPU and GPU. GPU memory
used by the process was ~1.2 GB with one camera and ~2.7 GB with two, so a
third camera is not recommended on a 4 GB card. With another heavy process
competing for the CPU, one GPU camera fell to 4.4 AI fps (10.5-minute run).

**Model weights** are not in git. `yolo11s.pt` downloads on first use. The plate
detector (AGPL-3.0) must be fetched once into `backend/`:

```bash
cd backend
.venv/Scripts/python.exe -c "from huggingface_hub import hf_hub_download; hf_hub_download('morsetechlab/yolov11-license-plate-detection','license-plate-finetune-v1n.pt',local_dir='.')"
```

Without it the app logs a warning and uses the classical plate localizer.

**Detection storage:** ~694 bytes per detection row. One AI camera writes ~300 MB/day at
`DETECT_EVERY_N_FRAMES=3` and ~780 MB/day at 1. `DETECTION_RETENTION_DAYS` plus
`POST /api/governance/purge-detections` (Administrator, dry-run by default, audited) removes old
detections that no alert, plate read or evidence item references. It is off by default.

## Performance and runtime pass (2026-09-28)

Every number here was measured on this machine against a copy of the real
database and live grid cameras; `backend/sentinel.db` itself was not used.

- **The website was slow because the event loop was busy with JPEG work.** For
  every source frame of every connected camera, the camera loop copied the
  frame, drew boxes and JPEG-encoded it twice (MJPEG preview and clip buffer),
  on the event loop. At 1080p that is ~30 ms per frame; grid cameras send 23-30
  fps, so one connected camera used ~68% of the event loop, and every API call,
  WebSocket push and live stream waited behind it. Both encodes now run in a
  worker thread, at 10 frames/s, the rate the MJPEG stream and clips consume
  (`pipeline/worker.py`). Measured with three 1080p/25 fps cameras connected:

  | | Before | After |
  |---|---|---|
  | `GET /api/cameras` p50 (3 cameras, AI off) | 575 ms | 3.2 ms |
  | `GET /api/alerts` p50 (3 cameras, AI off) | 675 ms | 3.6 ms |
  | `GET /api/cameras` p50 (1 AI + 2 connected) | 747 ms | 6.2 ms |
  | Frames the readers had to drop, 2 connected cameras, ~40 s | 527-534 each | 13-15 each |
  | AI frames processed in the same window (1 AI camera) | 121 | 182 |

- **Clip encoding is limited to 2 threads per encode** (`pipeline/clips.py`).
  libx264 otherwise takes every core. In the 10-minute alert-flood run
  above, system CPU sat at 90-100% most of the time; with the cap, the same
  scenario ran at 21-61% until the grid stream itself stalled.
- **Seven indexes** from the queries the app actually runs
  (`alembic/versions/20260928_0600_list_and_lookup_indexes.py`). The live camera
  page's newest detections for one camera went from 27 ms (sorting all 22k rows)
  to 0.12 ms. The rest keep the list pages' sort from growing with the table.
  `zones.camera_id` and `watchlist_entries.identifier` were measured and left
  out: both tables hold a handful of rows.
- **`rules_engine.evaluate`** runs 1 query per detection plus 1 per zone the box
  is inside: 0.75 ms and 4 queries per detection with 3 active zones, against
  ~25-120 ms of inference. Left as is.
- **Frontend at rest was already fast** (production pages 14-31 ms to load,
  APIs 3-29 ms). `start.bat` now serves the production build instead of
  `next dev`. With the GPU runtime processing cam02, 15 screens × 6 widths
  (320-1440 px) loaded with 0 console errors, 0 failed API calls, 0 horizontal
  overflow and nothing stuck loading.
- **JWT forgery in demo mode fixed.** With no `JWT_SECRET` set, demo mode signed
  tokens with the secret committed in this repository, so anyone could mint an
  Administrator token. It now signs with a random per-process secret
  (restarting the backend logs everyone out). Set `JWT_SECRET` in
  `backend/.env` to keep sessions across restarts.
- **Map positions.** The grid catalogue gives no coordinates (its records carry
  only `id` and `name`), so every grid camera is stored at 0,0. The map now
  leaves 0,0 cameras and route hops off and says how many it left off, instead
  of drawing them in the Gulf of Guinea. The Add Camera form no longer
  pre-fills an Ahmedabad position that was saved for any camera whose operator
  did not change it; the Edit form on the Cameras page can now set coordinates.
- **Real end-to-end, 10 minutes on GRID-cam02 (GPU):** live frame → YOLO11s →
  ByteTrack → CRITICAL zone rule → 320 alerts → 1 correlated incident → 90
  snapshots + 110 clips → SHA-256 verified on both → alert acknowledged → audit
  chain intact (699 rows). 1 worker throughout, 0 reconnects, 0 database locks,
  0 HTTP 5xx. The one traceback in the log was Windows asyncio reporting a
  browser closing an MJPEG socket; that exact case is now silenced
  (`main.py`).
- **Restart path:** camera stop → 0 workers; start → PROCESSING; restart and a
  second start → still 1 worker; backend hard-killed → every camera boots
  offline → cam02 reconnects on start → audit chain intact.

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
- **Camera capacity benchmark** (`tools/camera_bench.py`): drives real camera
  workers through the full pipeline (YOLOv8 + ByteTrack + plate localization +
  EasyOCR) at several camera counts and reports measured FPS, inference
  latency, CPU and RSS — never a number read off a config value.

  **Which clip you drive it with changes the answer completely**, so `--video`
  is now explicit. The bundled demo clip is 320x240, 4 seconds, and contains no
  vehicles at all (raw YOLO sees only a "tv" in it), so it measures
  decode-and-inference overhead on an empty frame — a floor, not a workload.
  Measured on one machine (16 cores / 16.9 GB, 25s per stage) against that clip
  and against a 1080p clip built from real vehicle photographs:

  | cameras | bundled 320x240, no vehicles | 1080p, real vehicles + plates |
  |---|---|---|
  | 1 | 8.27 fps, 148 ms inference, 416 MB | **5.64 fps**, 436 ms (p95 1250 ms), 1197 MB |
  | 3 | 8.34 fps, 40 ms, 590 MB | **2.15 fps** (p95-low 0.59), 1534 MB |
  | 5 | 7.85 fps, 65 ms, 771 MB | **2.28 fps** (p95-low 0.44), 2012 MB |

  On realistic content this machine sustains roughly **2 fps per camera at
  three to five cameras**, with the slowest 5% of intervals exceeding two
  seconds between processed frames, ~870-890% CPU (about 9 of 16 cores) and
  2 GB RSS at five cameras. No camera went silent at any stage.

  Read as "what this hardware does on this content", NOT as a capacity
  envelope. A real envelope needs real
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
  **ANPR accuracy is MEASURED but only on a small public still-image corpus**
  (n=25 labelled plates; see `docs/ANPR_ACCURACY.md` for the numbers and the
  caveats). The current figure is **exact match 0.24, character error rate
  0.39** — reproducible, and unchanged by the 2026-09-12 architecture pass,
  which improved the false-positive rate (0.28 → 0.24) rather than accuracy.
  It is **not** validated on this deployment's cameras: no labelled corpus is
  shipped in this repository (`tools/anpr_corpus/README.md` explains why), and
  the still-image corpus cannot exercise temporal fusion at all. The benchmark
  is ready the moment real, labelled footage is added there.

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

### Coverage-driven pass (2026-09-12)

Measuring per-module coverage pointed at the least-tested routers; every one of
them was hiding a real defect. Each was reproduced before it was fixed, and the
measurement is quoted with the fix.

- **Search displayed filters it never applied**: `after_hour`, `before_hour` and `entity`
  were parsed, returned in `parsed_filters`, and rendered to the operator ("Parsed filters:
  ..."), while every query ignored them — a time-scoped search that is not scoped is worse
  than one that is absent, because the screen asserts the filter was understood. `after 12am`
  also parsed to hour 12 (noon), and the raw query was used verbatim as the LIKE pattern, so
  text and a filter could never match together.
- **Taking a plate off the watchlist did not take it off**: the alert rule gated on
  `Vehicle.watchlist_flag`, and nothing recomputed that flag when an entry was deactivated. A
  cleared plate kept producing CRITICAL alerts — whose own reason text claimed a match to "an
  active watchlist entry" that no longer existed — and kept having snapshot evidence captured
  of it. Separately, `valid_until` was stored by the API and never compared to the clock
  anywhere, so an entry with an end date matched forever.
- **A live stream outlived the token that authorized it**: `/api/streams/{id}/mjpeg` validated
  its token once, at connect, then held the response open indefinitely, so
  `stream_token_ttl_seconds` bounded nothing and disabling an account did not cut its feed.
  The first version of the fix failed OPEN — `jwt.decode` verifies `exp`, so an expired token
  cannot be decoded at all and the expiry helper returned `None`, which the generator treated
  as "no deadline". An unreadable token now falls back to the configured TTL.
- **Every stream viewer pinned a database connection**: FastAPI holds a `Depends(get_db)`
  dependency until the response completes, and an MJPEG response is designed not to complete,
  so each open tile consumed one connection from a pool of 15 while doing nothing but reading
  JPEG bytes from a dict.
- **List limits had no ceiling**: measured on a 120-row database, `GET /api/detections`
  returned 100 by default, all 120 for `limit=-1` (SQLite reads `LIMIT -1` as no limit), and
  all 120 for `limit=100000000` — the whole detections table in one response for any
  authenticated user. Incident notes were unbounded too: a 2,000,000-character note returned
  200.
- **An administrator could lock out every administrator in one click**: self-disable returned
  200 and the caller's next request returned 401, and no enable route existed anywhere in the
  API, so recovery meant editing the database by hand. Correct credentials against a disabled
  account were also refused silently — the one login event most worth auditing.
- **Zones and rules accepted configurations that could never fire**: an unknown camera or zone
  id raised an unhandled `IntegrityError` (a 500 with a raw "FOREIGN KEY constraint failed"),
  an inverted zone box was stored and listed while matching nothing, `rule_type` was a free
  string, and deleted zones stayed in the rules page's zone picker — where a rule attached to
  one silently never fires.
- **Incident creation, assignment and timeline 500'd on ordinary input**: unknown
  camera/alert/vehicle ids and unknown assignees hit the same unhandled `IntegrityError`, a
  disabled account could be assigned work, and `', '.join(alert.reasons)` on a nullable column
  raised `TypeError: can only join an iterable` — while the summary endpoint in the same file
  already guarded that exact column.
- **One malformed row blanked the whole Alert Center**: a single alert with NULL `reasons`
  made `GET /api/alerts` fail response validation entirely, so the page went empty for every
  camera rather than for that one line. The per-camera view was also filtered in the browser
  over rows the server had already truncated to 200, so a camera whose alerts were older than
  the newest 200 system-wide looked like a camera with no alerts.
- **The dashboard's hourly chart was SQLite-only**: `/api/analytics/events-by-hour` grouped by
  `func.strftime(...)`, which SQLAlchemy passes through verbatim. Reproduced against a real
  PostgreSQL server: `function strftime(unknown, timestamp without time zone) does not exist`
  — a 500 on the production datastore that every SQLite test passed. Now grouped with
  `extract`, verified live by `tools/postgres_verify.py` (13/13) and guarded by a test that
  fails if any app module calls a SQLite-only SQL function again.

Backend coverage moved 81% → 85% across this pass and the CI floor was raised 80 → 84. The
percentage is the least interesting part: it was useful only as a map of which code had never
been executed by a test.
