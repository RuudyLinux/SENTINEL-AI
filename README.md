# SENTINEL VISION

Unified CCTV intelligence and real-time smart policing platform, built for the Gujarat Police
Innovation Challenge 2026.

SENTINEL VISION connects cameras from different sources into one registry, runs person and
vehicle detection, tracking and number-plate recognition on their video, turns detections into
rule-based alerts and correlated incidents, captures tamper-evident evidence, and gives
control-room operators one web dashboard for live monitoring, mapping and investigation.

## Capabilities

- **Camera integration**: one registry for webcams, uploaded video files, RTSP streams, the
  Sentinel Camera Grid catalogue and a mock VMS adapter. ONVIF is an interface stub.
- **AI analytics**: Ultralytics YOLO11s person and vehicle detection on one shared GPU model,
  a ByteTrack tracker per camera, selectable per camera. AI slots rotate when the hardware is
  short.
- **ANPR**: YOLO plate detector, EasyOCR, Indian plate-format validation and multi-frame voting.
  Uncorroborated reads go to a review queue.
- **Rules and alerts**: restricted zones (with schedules), watchlists and loitering, with
  cooldowns and an explainable risk score. CRITICAL alerts open or join incidents.
- **Evidence**: a snapshot per alert, event clips, operator REC recordings, SHA-256 at capture,
  and incident evidence packages (JSON or PDF, optional plate redaction).
- **GIS**: Leaflet/OpenStreetMap camera map and vehicle journey maps; nearby-camera search.
- **Self-Heal**: camera health states, reconnect with backoff, a grid-wide circuit breaker,
  database lock retry, and recovery events shown to operators.
- **Security**: JWT login with rate limiting, five roles with per-route RBAC, short-lived
  stream and evidence tokens, WebSocket authentication, and a hash-chained audit log.

## Architecture

```
Camera sources ──> adapters ──> per-camera worker ──> YOLO11s + ByteTrack ──> ANPR
                                     │                                          │
                                     ▼                                          ▼
                        clip buffer, preview (MJPEG)              correlation ──> rules engine
                                                                                   │
                                          alerts ──> incidents ──> evidence <──────┘
                                                        │
             FastAPI REST + WebSocket <─────────────────┘ ──> Next.js dashboard
             SQLite (dev) / PostgreSQL + PostGIS (Compose), evidence store, optional Valkey
```

The backend is one FastAPI process. Each connected camera runs as an asyncio task with its own
frame-reader thread. Blocking work (decoding, inference, OCR, file I/O) runs in a thread pool
sized to the camera count, and database operations run on a dedicated executor. Detections,
alerts and camera state reach dashboards over a WebSocket. The detailed design is in
`Documentation/02_High_Level_Design_Architecture.pdf`.

| Path | Contents |
|---|---|
| `backend/app/pipeline/` | Adapters, frame reader, camera worker, detector, ANPR, correlation, rules, clips, recorder, grid supervisor |
| `backend/app/routers/` | REST API (about 98 endpoints) |
| `backend/app/self_heal/` | Recovery event log and open problems |
| `backend/alembic/` | Database migrations |
| `backend/tests/` | Backend test suite |
| `frontend/` | Next.js 16 dashboard (App Router, TypeScript, Tailwind) |
| `docs/` | Accuracy benchmarks, threat model, privacy and governance, development notes |
| `Documentation/` | Challenge submission package (presentation, architecture, diagrams, demo) |

## Technology stack

| Layer | Technology | Licence |
|---|---|---|
| Dashboard | Next.js 16, React 18, TypeScript, Tailwind | MIT |
| Maps | Leaflet 1.9, OpenStreetMap tiles | BSD-2-Clause (data ODbL) |
| API | Python 3.11, FastAPI, Uvicorn, SQLAlchemy 2, Alembic | MIT / BSD |
| Video | OpenCV with FFmpeg (RTSP, decoding), libx264 for clips | Apache-2.0 / LGPL |
| Detection and tracking | PyTorch, Ultralytics YOLO11s, ByteTrack | BSD / AGPL-3.0 |
| Plate detection | YOLOv11 licence-plate model (morsetechlab) | AGPL-3.0 |
| OCR | EasyOCR | Apache-2.0 |
| Database | SQLite (WAL); PostgreSQL 16 + PostGIS 3.5 | Public domain / PostgreSQL, GPL-2.0 |
| Shared runtime state | Valkey 8 (optional) | BSD-3-Clause |
| Metrics | prometheus-client | Apache-2.0 |
| Evidence PDF | ReportLab | BSD |

Ultralytics YOLO and the plate model are AGPL-3.0. Distributing the system or offering it as a
network service carries AGPL obligations. The repository does not yet declare its own licence.

## Getting started

### Docker Compose (PostgreSQL + PostGIS, Valkey, backend, dashboard)

```
cp .env.example .env          # set POSTGRES_PASSWORD and JWT_SECRET
docker compose up --build
```

Compose refuses to start without those secrets, and the backend runs `alembic upgrade head`
before serving. Compose defaults to `DEMO_MODE=false`, so no demo accounts are seeded; create
the first administrator separately. The backend image uses CPU-only PyTorch.

### Local development (SQLite)

Backend (Python 3.11):

```
cd backend
uv venv --python 3.11 .venv
uv pip install --python .venv -r requirements.txt --extra-index-url https://download.pytorch.org/whl/cpu
.venv/Scripts/python.exe -m uvicorn app.main:app --reload --port 8000
```

Frontend:

```
cd frontend
npm install
npm run dev
```

Open <http://localhost:3000> and sign in as `admin` / `sentinel123` (demo accounts are seeded
while `DEMO_MODE=true`). Add a camera under **Cameras → Add Camera** (a video file of real
traffic, a webcam or an RTSP URL), then watch **Live Cameras** and **AI Vision**. Create a
restricted zone under **Map → Restricted Zones** or a watchlist entry to see alerts and incidents.

On Windows, `start.bat` starts both services: the backend on `backend/.venv-gpu` when CUDA works
there (CPU otherwise) and the production frontend build. `stop.bat` stops them.

Model weights: YOLO11s downloads on first use. The plate detector weights are not bundled and
never downloaded automatically; without them, ANPR falls back to classical plate localization
(see `backend/app/config.py`, `plate_model_name`).

## Configuration

Settings are read from the environment or `backend/.env` (template: `backend/.env.example`;
Compose uses the root `.env.example`). The main ones:

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | PostgreSQL URL; unset means SQLite at `DB_PATH` (default `backend/sentinel.db`) |
| `JWT_SECRET` | Required when `DEMO_MODE=false`; a random per-process secret is used otherwise |
| `DEMO_MODE` | Seeds demo accounts and the demo watchlist entry |
| `CORS_ALLOWED_ORIGINS` | Dashboard origin(s), default `http://localhost:3000` |
| `REDIS_URL` | Optional; shares cooldowns and rate limits across processes |
| `SENTINEL_GRID_EMAIL`, `SENTINEL_GRID_PASSWORD` | Sentinel Camera Grid credentials (`.env` only) |
| `SENTINEL_GRID_AUTOCONNECT` | Connect every grid camera at startup and keep it connected |
| `METRICS_TOKEN` | Bearer token for a Prometheus scraper; otherwise `/api/metrics` needs an Administrator |
| `NEXT_PUBLIC_API_BASE`, `NEXT_PUBLIC_WS_BASE` | Backend URLs, compiled into the frontend build |

Every setting, with its reasoning, is in `backend/app/config.py`. `NEXT_PUBLIC_*` values are
read at build time, so change them and rebuild. If port 8000 is taken, run the backend on
another port and point `NEXT_PUBLIC_API_BASE`/`NEXT_PUBLIC_WS_BASE` at it; the login page shows
which backend it reached.

## Testing

```
cd backend && .venv/Scripts/python.exe -m pytest -q        # backend suite
cd frontend && npm test                                   # Vitest unit tests
cd frontend && npm run typecheck                          # TypeScript
cd frontend && npx playwright test                        # browser tests, against a running stack
```

The Redis-backed tests are skipped unless a Redis-compatible server is available. CI (GitHub
Actions) runs the backend suite, the frontend tests and build, a Docker Compose deployment with
a migration check, and CodeQL.

## Known limitations

- **Sentinel Grid streaming is partial.** Grid authentication has been unreliable; in recent
  runs most grid cameras stayed degraded while a few connected. The platform does not claim all
  30 grid cameras streaming at once.
- **SQLite is single-writer.** It is the development and demo database. Write transactions are
  kept short and lock errors are retried, but PostgreSQL is the database for any larger
  deployment. The fix for login stalls under camera load has not yet been re-verified with all
  30 grid cameras streaming.
- **Map coverage.** The grid catalogue has no coordinates; 19 of 30 grid cameras were placed from
  OpenStreetMap place names (`backend/app/pipeline/grid_locations.json`) and 11 are
  intentionally unmapped.
- **One process, one machine.** AI throughput is bounded by that machine's GPU.
- **Accuracy figures are internal benchmarks** (`docs/AI_ACCURACY.md`, `docs/ANPR_ACCURACY.md`),
  not certified evaluations. ANPR on real CCTV depends heavily on plate size and sharpness.
- **VMS coverage.** ONVIF and vendor VMS adapters are not implemented beyond the interface.

## Production and scaling notes

- Run on PostgreSQL + PostGIS (the Compose stack) with `DEMO_MODE=false` and real secrets.
- Put the API behind a reverse proxy with TLS and address-level rate limiting.
- Set `REDIS_URL` before running more than one backend process.
- Set evidence and detection retention (`evidence_retention_days`, `detection_retention_days`)
  to match local policy; see `docs/PRIVACY_GOVERNANCE.md`.
- Planned, not implemented: separate AI worker processes per camera group, event streaming
  between ingestion and alerting, edge inference, and more VMS adapters.

## Further reading

- `CHANGELOG.md`: release history
- `docs/DEVELOPMENT_NOTES.md`: engineering log with design decisions and measurements
- `docs/THREAT_MODEL.md`, `docs/PRIVACY_GOVERNANCE.md`: security and governance
- `docs/AI_ACCURACY.md`, `docs/ANPR_ACCURACY.md`: detection and ANPR benchmarks
