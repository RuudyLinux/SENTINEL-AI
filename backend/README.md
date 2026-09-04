# SENTINEL VISION — Backend

FastAPI service running the real detection pipeline: YOLOv8 (ultralytics) person/vehicle
detection + ByteTrack tracking, EasyOCR-based ANPR, cross-camera correlation, a zone/watchlist
rules engine producing explainable alerts and incidents, evidence packaging, RBAC, and audit
logging. SQLite is the datastore; WebSocket pushes live alerts/detections to the dashboard.

## Run

```
uv venv --python 3.11 .venv         # already created
uv pip install --python .venv -r requirements.txt --extra-index-url https://download.pytorch.org/whl/cpu
.venv/Scripts/python.exe -m uvicorn app.main:app --reload --port 8000
```

First boot downloads `yolov8n.pt` (~6MB) and EasyOCR's detection/recognition models
(~100MB) — this only happens once per machine.

API docs: http://localhost:8000/docs
Health: http://localhost:8000/api/health

Seeded accounts (see `app/seed.py`), all password `sentinel123`:
`admin` (Administrator), `operator1` (Control Room Operator), `investigator1` (Investigator),
`auditor1` (Auditor).

## Adding a camera source

Three source types are supported, all producing genuine frames for the real detection
pipeline (`app/pipeline/source.py`):

- **webcam** — device index (e.g. `0`)
- **video_file** — path to an uploaded video, looped continuously like a live feed
- **rtsp** — any `rtsp://` URL, via OpenCV's FFmpeg backend (best-effort: no real
  CCTV/VMS was available to test against in this environment, no ONVIF discovery, no
  vendor-specific auth — open/read timeouts are set so a dead stream fails fast). Dropped
  streams (RTSP or otherwise) trigger a real reconnect with exponential backoff
  (`worker.py`), not an infinite "degraded" loop; after `reconnect_max_attempts` the
  camera is marked `offline` and the worker stops (Restart brings it back).

## Scope & honesty notes (see also doc's own "AI honesty rule")

- **No Kafka / Kubernetes / vector-DB / edge-Jetson deployment.** Single FastAPI process +
  SQLite is the "working slice" the source master document itself recommends building for a
  hackathon; the scale-out path is documented, not built.
- **No face recognition / person re-identification.** Privacy-sensitive and marked ADVANCED
  in the source doc — the Person module is detection/tracking only.
- **ANPR accuracy is whatever EasyOCR actually reads** off the vehicle crop in real time — no
  accuracy number is hard-coded or floor-clamped. See `/api/analytics/ai-performance` for real
  measured aggregates (not precision/recall — that needs a labeled ground-truth set, which
  isn't available here).
- **Restricted zones are full-camera-frame or axis-aligned rectangles**, not free-form polygon
  drawing.
- **Natural-language search is a regex/keyword parser** to structured filters, not an LLM.
- **JWT secret is a dev default in `config.py`** — replace via `.env` for anything beyond a
  local demo.
- **Evidence file/package and MJPEG/snapshot endpoints require a short-lived signed resource
  token** (`?token=`, scoped to that exact resource id, fetched via an authenticated
  `.../*-token` request first) instead of dropping auth for those `<img>`/`<a>`-driven
  endpoints — see `create_resource_token`/`get_user_from_resource_token` in `app/security.py`.
  Every access is logged to the audit trail under the real requesting user.
- **Demo accounts are gated behind `DEMO_MODE`** (`app/config.py`, default `true`). The four
  seeded accounts (`admin`, `operator1`, `investigator1`, `auditor1`, all `sentinel123`) and
  the demo watchlist entry only get created when `DEMO_MODE=true`; set it to `false` in
  `.env` for a real deployment (roles still seed either way — create an admin out-of-band).

## Official camera catalogue (Phase 3)

`POST /api/cameras/catalog/sync` (Administrator / Control Room Operator) calls
`GET {CAMERA_CATALOG_BASE_URL}/api/ingest` per the official Gujarat Police sandbox contract
and upserts the result into the Camera Registry — **register only**, it never starts AI
processing on anything. Start a registered camera explicitly with
`POST /api/cameras/{id}/start` (or select cameras and bulk-start from the Cameras screen).

- `CAMERA_CATALOG_BASE_URL` is empty by default and **never hardcoded anywhere** — set it in
  `.env` (see `.env.example`). Sync returns a clear `502` with an explanatory message while
  it's unset, or if the host times out / is unreachable / returns something unparseable —
  never fabricated camera data.
- Idempotent: re-syncing updates existing cameras (matched by the catalogue's own id, stored
  in `external_catalog_id`) instead of duplicating them. A camera the catalogue stops listing
  is marked `catalog_stale=True`, never deleted (its detections/evidence/incidents survive).
- The response schema's exact field names aren't fully pinned down in the official material
  available at build time, so `pipeline/catalog.py`'s normalizer accepts a few plausible key
  spellings (`id`/`camera_id`, `rtsp`/`rtsp_url`/`urls.rtsp`, `lat`/`latitude`, ...) and
  records what it couldn't find per-record rather than guessing.
- The raw RTSP URL (which may carry embedded credentials) is written to `Camera.source_uri`
  and **never** returned by any API response (`CameraOut` deliberately omits it — same
  principle as the evidence-token work below) or logged.
- **All three catalogue stream URLs are preserved** (Phase 6): `rtsp_url` feeds AI ingestion
  (`source_uri`, never exposed); `whep_url`/`hls_url` are stored on `Camera` and *are* returned
  by `CameraOut` — they're meant for direct client use (WHEP: browser preview player, HLS:
  dashboard/mobile/restricted-network fallback), unlike the RTSP URL. Neither is required to
  exist; a catalogue record that omits one leaves it `null`, and a later re-sync that happens
  not to repeat a previously-seen value doesn't erase it (see `tests/test_catalog.py`).

## Real Sentinel Camera Grid integration (final integration task)

A second, separate real camera source beyond the official Gujarat catalogue above: the
**Sentinel Camera Grid** (`https://cctv.corp8.cloud`) — 30 real, live traffic cameras
(Ahmedabad-area locations), **live-verified end-to-end this build**: discovery, real RTSP
connection, real 1920×1080 frames, real YOLOv8+ByteTrack, real alerts/incidents/evidence. See
`HYBRID_ARCHITECTURE.md` and `FINAL_PROJECT_STATUS.md` for the full verification record.

**Setup** — `backend/.env` (never committed, see `.env.example` for the exact keys):

```
SENTINEL_GRID_EMAIL=<your email>
SENTINEL_GRID_PASSWORD=<your password>
```

Base URL and RTSP host/port already default correctly in `app/config.py` (not secret — given
for this integration). Only the credentials are secret and must go in `.env`.

**Discovery**: `POST /api/cameras/sentinel-grid/sync` (Administrator / Control Room Operator,
also a button on the Cameras screen). Not a bare public JSON endpoint despite appearances —
`GET /cameras.json` unauthenticated 302-redirects to a session-cookie web login
(`POST /auth/login` with `email`/`password` form fields). `pipeline/sentinel_grid.py` logs in
once per sync (one `httpx.AsyncClient`, cookie carried automatically), then fetches
`/cameras.json` with that session, normalizes tolerantly (reuses `catalog.py`'s `_first()`
key-spelling lookup), and upserts — register-only, exactly like the official-catalogue sync:
never starts AI processing, never auto-connects anything.

**RTSP + credential handling**: `pipeline/adapters.SentinelGridAdapter` builds
`rtsp://<%40-encoded-email>:<password>@103.250.160.189:8554/stream/<id>` **in memory, at
connect time only**, from `.env` — never stored in the database (`Camera.source_uri` holds
just the bare grid camera id, e.g. `"cam04"`, never a URL), never logged, never returned by any
API response, never reaches the frontend. Delegates the actual capture to the existing
`RTSPAdapter` (same TCP-forcing, same timeouts) — one real RTSP implementation, not two.

**PTS timing**: reuses `pipeline/timing.py` unchanged — `sentinel_grid` is just added to
`TRUSTED_SOURCE_TYPES` (real RTSP underneath, same trust tier as `rtsp`).

**Timeouts — measured, not guessed**: this real grid's RTSP handshake measured 9.8s–13.8s
across repeated live attempts (real internet round-trip through the grid's relay), and its
HTTP login occasionally exceeded 8s too. `source_open_timeout_seconds` (20s) and
`sentinel_grid_timeout_seconds` (20s) were raised on that real evidence, not defensively
guessed — see `config.py`'s comments for the exact numbers observed.

**Reconnect**: reuses the existing `_reopen_with_backoff` (1×/2×/4×/8×/16×/30s-capped
exponential backoff) unchanged.

**Selective analytics**: reuses existing per-camera `ai_person`/`ai_vehicle`/`ai_anpr` toggles
and `/start`/`/stop` — sync registers all 30 offline, operator starts one (or several) at a
time. No "start all" exists anywhere in this codebase.

**Connection-lifecycle diagnostics**: `GET /api/cameras/{id}/diagnostics` includes a
`grid_state` field (`CONNECTING`/`CONNECTED`/`PROCESSING`/`DEGRADED`/`RECONNECTING`/
`DISCONNECTED`/`AUTH_ERROR`/`ERROR`) — kept separate from the DB `Camera.status` column
(only ever `online`/`offline`/`degraded`, many other call sites depend on that 3-value
contract) rather than migrating it.

**Troubleshooting**:

- `AUTH_ERROR` in diagnostics, or a 502 from `/sentinel-grid/sync` mentioning
  `SENTINEL_GRID_EMAIL/PASSWORD` → credentials missing or rejected — check `.env`.
- `Source did not respond within 20s` on Test Connection → real relay latency spike; retry
  once before assuming the camera is actually down.
- Frontend never needs and never receives RTSP credentials — playback goes through the
  existing MJPEG/resource-token pipeline (`/api/streams/{id}/mjpeg`), same as every other
  camera source type; no HLS/WHEP frontend work was needed for this.

## RTSP over TCP (Phase 3)

The official sandbox requires TCP transport. OpenCV's FFmpeg backend has no per-capture API
for this — the documented mechanism is the process-level `OPENCV_FFMPEG_CAPTURE_OPTIONS`
environment variable, which `CameraSource.open()` sets to `rtsp_transport;tcp`
**immediately before** constructing the `cv2.VideoCapture` for every RTSP open (see
`pipeline/source.py`), not once at import — so it's actually in effect, not just documented.
Toggle via `settings.rtsp_force_tcp` (default `true`) if a source genuinely needs UDP.

**Verifying it's really TCP** (OpenCV doesn't expose the negotiated transport back to the
caller, so this has to be checked outside Python):
- Packet capture / `netstat` on the camera's RTP port while a stream is open — TCP transport
  means no UDP flow to that port at all, RTP/RTCP travel interleaved on the existing TCP
  connection instead.
- `ffprobe -v trace -rtsp_transport tcp <url>` against the same URL as a manual cross-check.
- `tests/test_source_rtsp.py` asserts the environment variable is actually set before
  `VideoCapture` is constructed — the automatable part of this; the wire-level confirmation
  above is manual since it needs a real reachable RTSP source.

## Source vs. processing timestamps (Phase 3)

Every `Detection`/`Plate`/`Alert`/`Evidence` row now carries both:
- `timestamp` / `created_at` — **PROCESSING time**: when SENTINEL wrote the row.
- `source_timestamp` — **SOURCE time**: when the frame was actually captured, if reliably
  known. Reconstructed from `CAP_PROP_POS_MSEC` (see `pipeline/timing.py`) anchored to the
  wall-clock time the capture session was opened — POS_MSEC is stream-relative, not absolute.

**This is deliberately not trusted uniformly.** `CAP_PROP_POS_MSEC` reliability is
backend-dependent and this project does not pretend otherwise:
- `video_file`: generally reliable (FFmpeg demuxing a local file).
- `rtsp`: often reliable when the camera/server sends proper RTP timestamps, but not
  guaranteed across vendors — OpenCV exposes no way to tell a trustworthy reading from a
  synthesized one.
- `webcam`: well-documented as unreliable/stuck-at-zero on DirectShow/MSMF/V4L2 — **never**
  trusted here.

A reading is also rejected if it hasn't advanced since the last accepted one (guards a known
stuck/backwards-jump failure mode on some FFmpeg/RTSP builds). When source time isn't
trustworthy, `source_timestamp` stays `null` and callers (UI, evidence package, incident
timeline) fall back to processing time — the two are never conflated. See
`tests/test_timing.py`.

Variable frame rate: none of the temporal logic elsewhere assumes a constant interval between
frames. The alert cooldown (`rules_engine._on_cooldown`) already keyed off real wall-clock
deltas (`time.monotonic()`), not a frame count — unaffected by Phase 3. The AI-inference
throttle (`detect_every_n_frames`) counts frames, not time, by design (a CPU-budget control,
not a timing source), and a slow-but-successful read is never treated as camera failure — only
consecutive *failed* reads trigger the reconnect path.

## Event video clips (Phase 3)

Every alert now gets a bounded MP4 clip alongside the existing snapshot: `clip_pre_event_seconds`
(default 5s) from a small per-camera ring buffer of already-encoded JPEG frames, plus
`clip_post_event_seconds` (default 10s) collected live as a background task
(`pipeline/clips.py`) — the camera's own read/inference loop is never blocked waiting for the
post-event window, and decode/encode work runs off the event loop via `asyncio.to_thread`.
Nothing here ever buffers a whole stream — both windows are bounded and the ring buffer is
released with the camera on stop.

The resulting `Evidence(evidence_type="clip")` row links `alert_id`, `detection_id`,
`incident_id` (when one exists), `camera_id`, `event_type`, and `source_timestamp`, and is
served through the exact same secured `/api/evidence/{id}/file` endpoint (short-lived
resource token, RBAC-checked issuance, audited access) as snapshot evidence — no separate,
weaker path for video.

## Regression tests (Phase 3)

```
.venv/Scripts/python.exe -m pytest tests/ -v
```

Runs against a throwaway temp SQLite DB (never the real `sentinel.db`), with YOLO/OpenCV
mocked out where a test doesn't need the real model (tracker isolation, RTSP transport
config) — focused unit/integration coverage for catalogue normalization + idempotency, RTSP
TCP config, the ANPR quality gate, evidence-token authorization + scope, upload validation,
alert-cooldown dedup, source-timestamp propagation, and per-camera tracker isolation.

## Multi-camera concurrency (Phase 4)

Live-tested with 2 concurrent `video_file` cameras (different files) using a diagnostics
surface built for this: `GET /api/cameras/{id}/diagnostics` (per-camera frame/inference
timing, drop/reconnect/error counters, worker task state) and
`GET /api/cameras/diagnostics/system` (process CPU/RAM, torch/cv2 thread config, running
worker count) — both Administrator/Control Room Operator only.

**Real bug found and fixed**: a camera's worker task could die silently under sustained
2-camera SQLite write contention. Root cause: SQLAlchemy's default `expire_on_commit=True`
(and unconditional object expiry on `rollback()`) meant a bare attribute read like
`camera.fps`, at a call site with no `db.commit()` nearby, could trigger an implicit,
unguarded `SELECT` — and that `SELECT` could itself hit "database is locked". Fixed at the
root (`app/db.py`: `expire_on_commit=False`, WAL journal mode, 30s busy_timeout) and defensively
(the entire per-iteration loop body in `worker.py` is now one guarded region; static
per-camera values are read once and cached instead of re-read from the ORM object every
iteration; a top-level `_camera_loop_supervised` wrapper guarantees a camera is marked
`offline` and logged with a full traceback instead of disappearing, even for a failure mode
nobody anticipated). Also throttled the `last_frame_at`/`status` heartbeat commit to at most
once/second (was every frame — the dominant source of write volume) — real detection/alert
writes are unaffected, they still commit immediately.

**Result after the fix**: 2 concurrent cameras (different video files) sustained ~90+ seconds
of continuous operation in testing, incrementing frame counts throughout, with one transient
error caught and recovered from (logged, `recovered_errors` incremented, task stayed alive) —
not a crash. **Not claimed**: sustained real-time throughput for 3+ concurrent cameras, or any
specific count against real RTSP streams (only tested against 2 local video files) — this
hardware's actual concurrent-AI-processing ceiling has not been measured beyond 2.

## RTSP — local test harness attempted

A real local RTSP source was stood up for this: `pip install imageio-ffmpeg` (bundles a real
ffmpeg binary) run as `ffmpeg -re -stream_loop -1 -i uploads/car-detection.mp4 -c copy -f rtsp
-rtsp_flags listen rtsp://127.0.0.1:8554/test`, with `CameraSource` pointed at that URL. The
attempt is real and reproducible — but in this build environment the connection could not
complete: opening a listening socket for a new process requires a Windows Firewall prompt
this non-interactive session has no way to approve (confirmed: adding an explicit `netsh
advfirewall` allow rule fails with "requires elevation"). Marked **UNVERIFIED at the wire
level**, not passed — rerun the same two commands on a machine where that prompt can be
accepted (or with `netsh advfirewall firewall add rule ... program=<ffmpeg path>` run as
Administrator first) to actually complete it.

This attempt did surface one real, useful finding en route: **`CAP_PROP_OPEN_TIMEOUT_MSEC` is
not reliably honored** by this build's OpenCV/FFmpeg — measured at ~30s before failing despite
being set to 5000ms. Fixed by enforcing the timeout independently at the asyncio level
(`worker._open_with_timeout`, `settings.source_open_timeout_seconds`) instead of trusting that
property alone, covering both the worker's connect/reconnect path and the `test-connection`
endpoint.

## Codec/resolution decode capability (Phase 5)

Live-tested (not assumed) via `pipeline/source.CameraSource`, the same code the worker uses:
transcoded local copies of the demo clip to H.265/HEVC @ 960×540 and H.264 @ 1280×720
(`imageio-ffmpeg`, `libx265`/`libx264`), then opened and read 10 frames from each plus the
original H.264 768×432. All three opened, reported the correct resolution, and decoded every
frame. This is a codec-decode test, not a network/RTSP test — codec support is independent of
transport, so it holds for RTSP sources using the same codecs.

## Reconnect/backoff — real timing evidence (Phase 5)

Live-tested: a camera pointed at a nonexistent webcam device (guaranteed open failure) while a
second, real camera kept running. Observed real backoff (`error_count` climbing 4→5→6 across
~5 attempts before giving up, `status` `degraded`→`offline`, no further retries afterward — not
a tight loop) while the healthy camera's `last_frame_at` advanced continuously and its `status`
never left `online` — confirms one camera's failure/reconnect cycle does not affect another's.

## Scene discontinuity / loop behavior (Phase 5)

The demo clip already loops continuously in every run (`CameraSource.read()` rewinds on EOF),
so this is exercised on every test, not a special case. Verified from real per-frame data:
`source_timestamp` visibly resets (jumps backward) at each loop boundary — the actual scene
cut — and the very next assigned ByteTrack `track_id` after a cut is a fresh, low id (e.g. `11`
appearing right after a cut with prior ids in the 200s), not a corrupted or runaway value. The
alert cooldown is keyed on real wall-clock elapsed time (`time.monotonic()`, unit-tested in
`tests/test_alert_dedup.py`), not frame count or track continuity, so it is structurally
unaffected by a scene cut — not independently re-verified with an *active* zone/watchlist
firing across a cut in this pass.

## Reproducible workflow once CAMERA_CATALOG_BASE_URL is supplied

```bash
# 1. Configure (never hardcoded — .env only)
echo "CAMERA_CATALOG_BASE_URL=https://<official-sandbox-host>" >> backend/.env

# 2. Sync the catalogue (registers only — connects nothing)
curl -X POST -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/cameras/catalog/sync

# 3. Show imported cameras
curl -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/cameras

# 4. Select ONE camera (by id from step 3) and start it — do not start all of them
curl -X POST -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/cameras/{camera_id}/start

# 5. Verify RTSP/TCP + health
curl -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/cameras/{camera_id}/health
curl -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/cameras/{camera_id}/diagnostics

# 6. Detection/tracking/ANPR/alerts happen automatically once online — watch:
curl -H "Authorization: Bearer $TOKEN" "http://localhost:8000/api/detections?camera_id={camera_id}"
curl -H "Authorization: Bearer $TOKEN" "http://localhost:8000/api/alerts"

# 7. Evidence + audit are produced automatically on any alert (see Phase 3/4 — already
#    live-verified with real footage); investigate via the dashboard or:
curl -H "Authorization: Bearer $TOKEN" "http://localhost:8000/api/evidence"
curl -H "Authorization: Bearer $TOKEN" "http://localhost:8000/api/audit"

# 8. Repeat step 4 for a SECOND camera id to confirm 2-camera isolation on the real grid —
#    same diagnostics endpoints, same per-camera stats, no shared state between them.
```

No step here starts more than the operator explicitly selects — catalogue sync never calls
`start_worker` (see `pipeline/catalog.upsert_from_catalog`), and there is no "start all" endpoint.

## Judge demo (Phase 6, DEMO_MODE only)

Two endpoints (Administrator/Control Room Operator, `403` outside `DEMO_MODE`) make the
primary demo scenario repeatable without manual DB editing:

- `POST /api/system/demo/reset` — stops any running camera workers, wipes transactional data
  (detections/plates/vehicles/alerts/incidents/evidence — **not** users, roles, or the audit
  trail, which keeps recording), and ensures `C-014`/`C-019` + the `GJ05AB1234` watchlist entry
  exist. Idempotent — safe to call repeatedly.
- `POST /api/system/demo/trigger-scenario` — fires one deterministic sighting of the watchlist
  plate on `C-014` then `C-019` (4 minutes apart) through the **real** code path
  (`upsert_vehicle_for_plate`, `rules_engine.evaluate`, `get_route`) — see
  `pipeline/demo_scenario.py` for exactly what is and isn't real about it (short version: only
  the OCR-image-decode step is stood in for, because the one real test clip available doesn't
  contain this plate; detection/tracking/correlation/alerting/evidence are all genuine). If the
  target camera is actually running, its **real current frame and real live event-clip buffer**
  are used for the resulting snapshot/clip evidence — nothing is fabricated there either.

Cameras must be started (`POST /{id}/start`) *after* reset and *before* triggering — reset
stops them, so there's nothing to snapshot/clip until they're running again.
