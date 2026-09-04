# SENTINEL VISION — Hybrid / Innovative Architecture

Gujarat Police Innovation Challenge 2026. This document is the architecture record for
the Hybrid/Innovative upgrade built on top of the existing SENTINEL VISION platform —
what was already real before this pass, what was added, and the exact claims that are
safe to make in the submission.

**One-line pitch for judges**: SENTINEL does not replace a department's existing
CCTV/VMS investment. It normalizes heterogeneous camera/VMS sources behind one adapter
interface, runs real AI (detection, tracking, ANPR, cross-camera correlation) on top of
them, and exposes an optional central camera-registry/VMS layer for deployments that
want it — Model 3 (VMS Federation/Middleware) and Model 4 (Central VMS+AI) capabilities
inside one coherent platform, which is why the overall architecture is Hybrid, not a
pure instance of either model.

---

## 0. Live verification update (post-submission-hardening)

Everything in this document describing the adapter interface (§3, Model 3) has since been
**live-verified against a real, external camera grid** — the Sentinel Camera Grid
(`cctv.corp8.cloud`, 30 real Ahmedabad-area traffic cameras) — not just built and
unit-tested. Real discovery, real RTSP connection (1920×1080, 25fps), real YOLOv8+ByteTrack
on real frames, real cross-camera-ready appearance signatures, real zone_entry alerts,
real auto-created incidents, and — after a hardening pass that closed a real gap
(zone_entry alerts previously got no snapshot without an ANPR/watchlist match) — real
Evidence rows with real files on disk, all confirmed via direct inspection, not assumed.
See `FINAL_PROJECT_STATUS.md` for the itemized verification table and
`HACKATHON_JUDGE_QA.md` Q5–Q6 for how to explain this to judges.

---

## 1. Architecture diagram

```
 Existing Cameras / Existing VMS / RTSP / ONVIF (future) / Generic VMS
        |
        v
 [ CameraAdapter interface ]   <-- Model 3: VMS Federation / Middleware
   WebcamAdapter | VideoFileAdapter | RTSPAdapter | MockVMSAdapter | ONVIFAdapter(stub)
        |
        v
 [ Stream / metadata normalization ]   (pipeline/timing.py — source vs. processing time)
        |
        v
 [ SENTINEL AI Intelligence Layer ]
   Detection (YOLOv8) -> Tracking (ByteTrack) -> ANPR (EasyOCR)
   -> Appearance signature (person, HSV histogram — NOT face recognition)
        |
        v
 [ Event & Risk Engine ]   (rules_engine.py)
   watchlist_plate | zone_entry | loitering — all schedule-window aware
        |
        v
 [ Cross-camera correlation ]   (correlate.py)
   vehicles: by plate text   |   persons: by appearance-similarity signature
        |
        +--------------------+--------------------+
        v                    v                     v
   Search/Investigate    Alert Bus (WebSocket)   Evidence Store
        |                    |                     |
        v                    v                     v
         Unified Police Investigation Dashboard (Next.js)

 [ Central VMS / Registry capability ]   <-- Model 4: optional, layered underneath
   Camera registry + groups | RBAC | audit log | health/diagnostics | catalogue sync
```

---

## 2. Model 2 — Unified Viewing and Selective Analytics

| Capability | Status |
|---|---|
| Camera registry | Implemented |
| Camera status (online/degraded/offline) | Implemented |
| Live stream viewing (MJPEG, token-secured) | Implemented |
| Multiple-camera grid | Implemented (`/live`) |
| Full-screen camera view | Implemented (`/live/[cameraId]`) |
| Camera search/filter | Implemented (search router + group filter, new) |
| **Camera groups** | **New this pass** — `Camera.camera_group`, filter dropdown on Cameras screen |
| Location/map view | Implemented (`/map`) |
| Enable/disable analytics per camera | Implemented (`ai_person`/`ai_vehicle`/`ai_anpr` at creation) |
| **Editable analytics config after creation** | **New this pass** — `PATCH /api/cameras/{id}` + inline edit UI |
| Detection overlays | Implemented (annotated MJPEG) |
| Person detection | Implemented (YOLOv8) |
| Vehicle detection | Implemented (YOLOv8) |
| Object tracking | Implemented (ByteTrack, per-camera isolated instance) |
| ANPR | Implemented (EasyOCR + quality gate) |
| Alert generation | Implemented, explainable (`reasons[]`) |
| Camera health/status | Implemented (`/health`, `/diagnostics`) |

Per-camera selective analytics already worked as designed at creation time (Camera A:
person+vehicle, Camera B: vehicle+ANPR, etc., per the example in the brief) — the gap
closed this pass was the inability to *change* that after the fact without deleting and
re-adding the camera.

---

## 3. Model 3 — VMS Federation and Middleware Layer

| Capability | Status |
|---|---|
| Common internal representation (adapter interface) | **New this pass** — `CameraAdapter` ABC (`pipeline/adapters.py`) |
| RTSP adapter | Implemented, real (OpenCV/FFmpeg, TCP-forced per Gujarat sandbox spec) |
| Webcam / video-file adapters | Implemented, real |
| Generic VMS adapter | **New this pass** — `MockVMSAdapter`, real synthetic frames end-to-end through the actual AI pipeline, proves the boundary is pluggable |
| ONVIF adapter | **New this pass** — honest interface **stub**: registered, dispatches correctly, `open()` raises a clear `NotImplementedError` rather than pretending to connect (no ONVIF device was available to test against) |
| Official Gujarat camera catalogue federation | Implemented, real (`pipeline/catalog.py`) — register-only sync, idempotent, never fabricates fields it can't find |
| Future vendor-specific adapters | Interface ready — drop in a new `CameraAdapter` subclass, register it in `get_adapter()`, nothing else in the pipeline changes |
| Normalized event types (Detection/Alert/Evidence as the common representation) | Implemented (existing ORM models already serve this role) |

`CameraSource` (used by `worker.py`, `routers/cameras.py`) is now a thin backward-compat
wrapper over `get_adapter()` — every existing camera keeps working exactly as before;
the adapter boundary was extracted underneath it, not bolted on beside it.

---

## 4. Model 4 — Central VMS and AI Platform

| Capability | Status |
|---|---|
| Central camera registry | Implemented |
| Camera provisioning (manual + catalogue sync) | Implemented |
| **Camera groups** | **New this pass** |
| Camera health monitoring | Implemented (per-camera + system-wide diagnostics) |
| Stream status | Implemented |
| Recording configuration | Prototype — event clips are a bounded ring buffer with fixed pre/post-event windows (`clips.py`), not yet operator-configurable retention |
| Retention policy configuration | Planned/future — not built |
| Event storage | Implemented (SQLite, indexed on the hot query columns as of this pass) |
| AI analytics management | Implemented (per-camera, now editable in place) |
| User/RBAC | Implemented (5 roles: Administrator, Control Room Operator, Investigator, Supervisor, Auditor) |
| Audit logs | Implemented — every mutating action, resource-token issuance, and access is logged |
| Centralized alert management | Implemented |
| Evidence management | Implemented — snapshot/clip/report, sha256 verify, JSON/PDF package export, chain-of-custody from real audit rows |
| Camera-to-location mapping | Implemented (lat/lng + map view) |
| System health dashboard | Implemented (`/admin/system`, `/api/system/status` — real subsystem checks, not hardcoded strings) |

---

## 5. Cross-camera intelligence (the main differentiator)

- **Vehicles**: already real before this pass — same normalized plate text seen on any
  camera is the same `Vehicle` row (`correlate.upsert_vehicle_for_plate`), with a full
  cross-camera route (`GET /api/vehicles/{id}/route`) and a dedicated tracking screen.
- **Persons — new this pass**: `GET /api/persons/{detection_id}/similar` ranks other
  person detections by a compact HSV color-histogram similarity of their crop
  (`pipeline/appearance.py`), weighted toward Hue (the primary color signal) over
  Saturation/Value (lighting-sensitive). **This is explicitly not face recognition and
  not an identity claim** — it is a visual-similarity candidate list for an
  investigator to review manually, surfaced with a prominent disclaimer on the new
  `/persons/tracking` screen and in the API's own docstring. No new dependency (built
  on `cv2`/`numpy`, already in `requirements.txt`).
- Supported search patterns: plate search, vehicle route across cameras, person
  appearance-similarity search, restricted-zone violations, watchlist matches, and
  keyword/time-hint natural-language search (`search.py`) mapping to structured
  filters — not an LLM, documented as such.

---

## 6. Event & Risk Engine (Phase 6)

Rule types, all schedule-window aware as of this pass:

- `watchlist_plate` — unconditional, cooldown per (camera, vehicle).
- `zone_entry` — unconditional per active zone, cooldown per (camera, zone, track).
  **`Zone.schedule_start`/`schedule_end` existed on the model/schema before this pass
  but were never read by the evaluator — a real, previously-dead capability, now
  enforced** (`rules_engine._within_schedule`, wraps past midnight correctly).
- `loitering` — **new this pass**, dwell-time based. Unlike `zone_entry`, a zone's
  loitering check only activates when an **active `AlertRule(rule_type="loitering")`**
  row actually targets it — configurable, not hardcoded, per the brief's requirement,
  without changing `zone_entry`'s existing unconditional behavior. Presence is tracked
  in-memory per (camera, zone, track), pruned of stale entries every call.

Not built (documented, not attempted this pass, for honesty and time reasons):
direction-of-travel rules, multi-event correlation rules across separate cameras/rules.

---

## 7. Evidence system

Unchanged by this pass, already solid: `Event ID`/`Camera ID`/`Location`/`Timestamp`/
`Detection type`/`Confidence`/`Track ID`/`ANPR result`/`Snapshot`/`Clip reference`/
`Chain-of-custody (real AuditLog rows)`/`Export metadata` — all present. Package export
labeled `"generated_by"` a real username, never a forensic-certainty claim.

---

## 8. Database changes (this pass)

| Table | Change |
|---|---|
| `cameras` | + `camera_group VARCHAR` (default `''`) |
| `detections` | + `appearance_signature JSON` (nullable), + index on `timestamp`, `camera_id` |
| `zones` | + `loitering_seconds FLOAT` (nullable) |
| `alerts` | + index on `severity`, `status`, `camera_id` |
| `incidents` | + index on `status` |

All additive, via the existing `ensure_columns` migration helper plus a new parallel
`ensure_indexes` helper (`db.py`) — safe to run against an already-populated database,
idempotent on every startup. **Note**: the group field is named `camera_group`, not
`group` — `GROUP` is a reserved SQL keyword; using it broke the raw-SQL migration
helpers (`ALTER TABLE ... ADD COLUMN group ...` is a syntax error in SQLite), caught by
running the actual migration against a real `TestClient` startup, not just unit tests.

---

## 9. API changes (this pass)

- `PATCH /api/cameras/{id}` — in-place edit of name/location/camera_group/lat/lng/
  analytics toggles. Does not touch `source_type`/`source_uri` (a reconnect operation).
- `GET /api/persons/{detection_id}/similar?min_similarity=&exclude_camera_id=&after=&before=`
  — ranked appearance-similarity candidates.
- `source_type` now also accepts `"mock_vms"` and `"onvif"` on camera create /
  test-connection.
- `POST /api/cameras/test-connection` and the worker's own reconnect path now catch and
  report an adapter's `NotImplementedError` (e.g. ONVIF) as a clean, honest failure
  instead of an unhandled 500/crash.

---

## 10. Frontend changes (this pass)

- `cameras/add`: Group field; Mock VMS / ONVIF source-type options with honest inline
  captions.
- `cameras`: Group column, group filter, inline Edit action (PATCH).
- `admin/rules`: Loitering rule type, zone selector shared with `zone_entry`.
- `map` (Restricted Zones tab): optional loitering-threshold input, shown on each zone.
- `vision`: "Find similar" action on person detection rows.
- **New** `persons/tracking`: appearance-similarity search screen, prominent
  not-identity-verification banner, mirrors the existing `vehicles/tracking` UX.
- `lib/api.ts`: added `api.patch`.

---

## 11. AI / pipeline changes (this pass)

- `pipeline/adapters.py` (new): `CameraAdapter` interface + 5 concrete adapters.
- `pipeline/appearance.py` (new): person appearance-similarity signature + weighted
  per-channel HSV histogram comparison.
- `worker.py`: computes and persists a person detection's appearance signature
  (best-effort, never breaks detection persistence on failure); `_open_with_timeout`
  now also catches a general adapter exception, not just a timeout.
- `correlate.py`: `find_similar_person_detections`.
- `rules_engine.py`: schedule-window gate (activates existing dead fields) + loitering.

---

## 12. Security improvements (this pass)

- **Login rate limiting** (`routers/auth.py`): in-memory sliding window, 5 failed
  attempts / 60s per username → `429`, cleared on success. Documented as a
  single-process first layer, not a distributed limiter.
- Hot-path indexes reduce the query surface an attacker could use to degrade
  availability via expensive unindexed scans at scale (secondary benefit of #8).
- Everything already in place before this pass (bcrypt direct, JWT, RBAC, short-lived
  scoped resource tokens for browser-nav endpoints, upload hardening, audit logging) is
  unchanged.

**Deliberately not done**: a repo-wide `datetime.utcnow()` → timezone-aware sweep.
SQLite `DateTime` columns store naive values throughout and cooldown/schedule/timeline
logic compares them directly; mixing naive and aware datetimes across dozens of call
sites this close to submission is a real regression risk for no functional gain.
Documented as future cleanup.

---

## 13. Tests added (this pass)

`backend/tests/`: `test_adapters.py` (factory dispatch, mock VMS produces real frames,
ONVIF fails loudly), `test_appearance.py` (signature/similarity correctness incl. the
channel-weighting fix), `test_rules_loitering.py` (dwell-under-threshold, fires-once,
schedule-window suppression), `test_camera_update.py` (PATCH partial-update semantics,
auth, 404). **70/70 backend tests pass** (was 63 before this pass).

Not added: a frontend test runner (none existed; not introduced under deadline
pressure — accepted gap, not silently dropped).

---

## 14. How to run

```bash
# Backend
cd backend
.venv/Scripts/python.exe -m uvicorn app.main:app --reload --port 8000
# Frontend
cd frontend
npm run dev
```

Login: `admin` / `sentinel123`. See `backend/README.md` and `README.md` for full detail
(unchanged by this pass).

---

## 15. Demo script (adapted to what's real)

1. **Cameras screen** — show camera groups, per-camera status, catalogue-synced vs.
   manual cameras side by side.
2. **Add a Mock VMS camera** — live in seconds, real synthetic frames flowing through
   the real detector — proves the adapter boundary without a real vendor.
3. **Attempt an ONVIF camera** — Test Connection reports the honest interface-stub
   error instantly, not a hang or fake success.
4. **Live grid** — real person/vehicle detection + tracking overlays on a real/uploaded
   video feed.
5. **ANPR** — real plate reads on a vehicle detection.
6. **Trigger a zone_entry or loitering alert** (loitering: walk/loop within a
   loitering-configured zone past its threshold) — explainable `reasons[]` on the
   alert.
7. **Investigate the incident** — auto-created on CRITICAL, evidence attached.
8. **Vehicle cross-camera route** — same plate seen on two cameras, route + map.
9. **Person appearance-similarity search** — pick a person detection on Vision, "Find
   similar," show the ranked candidates with the not-identity-verification banner.
10. **Generate an evidence package** — JSON/PDF, chain-of-custody from real audit rows.
11. **Camera Management → Sync Camera Catalogue** — show the official Gujarat catalogue
    federation registering cameras without connecting any of them (Model 3 in action).
12. **System → Scope & Honesty / System Health** — show the architecture-vs-scale
    honesty panel and real subsystem status checks.

---

## 16. Known limitations

- ONVIF is an interface stub, not a working integration (no device was available).
- Person appearance-similarity is a lightweight color-histogram signal, not a learned
  re-identification embedding — it will conflate people wearing similarly-colored
  clothing and is explicitly presented as such, never as identity.
- No recording retention-policy configuration UI yet (clips use fixed windows).
- No direction-of-travel or multi-event-correlation rule types yet.
- No distributed rate limiting (single-process only).
- No frontend automated test suite.
- Everything already documented as out of scope in `backend/README.md` (no Kafka/K8s/
  vector-DB/edge-Jetson deployment, no face recognition, ANPR accuracy unclamped,
  natural-language search is a regex/keyword parser) remains out of scope.

## 17. Future scalability plan

**ARCHITECTURE TARGET — statewide scale, NOT built, NOT running, NOT tested at this size.**
This diagram is the documented future path only. It is unrelated in scale to the REAL
VERIFIED result in §0/§11: a 30-camera authorized Sentinel Camera Grid, with one real
camera connected and processed at a time. The diagram's generic "Sentinel" box is an
illustrative example source type at target scale, not a claim that our real 30-camera
grid is already operating at 80,000-camera scale — it isn't, and no sentence in this
document should be read to say otherwise.

```
80,000 CAMERA SOURCES
                         │
          ┌──────────────┼──────────────┐
          │              │              │
       VMS A          VMS B          Sentinel
          │              │              │
          └──────────────┼──────────────┘
                         ↓
                CAMERA GATEWAY LAYER
                         ↓
                MESSAGE / EVENT BUS
                         ↓
              ┌──────────┴──────────┐
              │                     │
        Stream Workers         Metadata Workers
              │                     │
              ↓                     ↓
        GPU AI Workers       Event Processing
              │                     │
              └──────────┬──────────┘
                         ↓
                CENTRAL PLATFORM
                         ↓
        Search / Alerts / Investigation
                         ↓
                      Evidence
```

Maps onto this build's existing components, not a clean-slate design:

- **VMS A / VMS B / Sentinel** → illustrative source types at target scale. Today's real,
  tested equivalent is 3 adapters (`RTSPAdapter`, plus catalogue clients for the official
  Gujarat contract and the real 30-camera Sentinel Camera Grid — one camera connected and
  AI-processed at a time, not 30 concurrently, and nowhere near 80,000). The claim here is
  narrow: the *interface* doesn't change shape as source count grows — not that source
  count has been grown.
- **Camera Gateway Layer** → today's `CameraAdapter` boundary (`pipeline/adapters.py`)
  is exactly this layer at 1-camera scale; a gateway process fronting many adapters is
  the same interface, more instances.
- **Message/Event Bus** → not built. Today, `worker.py` calls the pipeline in-process
  per camera; at scale this is where frames/detections would be published instead of
  called directly.
- **Stream Workers / GPU AI Workers** → today's per-camera worker task + per-camera
  YOLO/ByteTrack instance (`_MODELS_BY_CAMERA`) is the single-process analogue —
  already isolated per camera (verified: no shared mutable state), which is what makes
  "one worker per camera, distributed across GPU nodes" a scale-out of the *same*
  isolation property, not a redesign of it.
- **Metadata Workers / Event Processing** → today's `rules_engine.evaluate()` running
  synchronously after each detection is the same responsibility; at scale it would
  consume from the bus instead of being called in-process.
- **Central Platform → Search/Alerts/Investigation/Evidence** → already real today
  (SQLite-backed); the swap at scale is the datastore/search index underneath, not the
  API/UI contract above it.

None of the bus/gateway/distributed-worker layer is built — would be premature for this
stage's real camera count — but nothing in this build's design (adapter interface,
per-camera-isolated state, stateless rule evaluation keyed by camera+zone+track) blocks
inserting it later without touching detector/ANPR/rules-engine/dashboard code.

## 18. Exact safe claims for the Gujarat Police submission

**Safe to claim:**
- A working, end-to-end video-intelligence platform: real YOLOv8 detection, ByteTrack
  tracking, EasyOCR ANPR, explainable rule-based alerting, cross-camera vehicle
  correlation, cross-camera person appearance-similarity search, evidence packaging,
  RBAC, and audit logging — all running against real frames, not scripted/mocked data.
- A formal adapter interface (Model 3) supporting RTSP, webcam, video file, and a
  demonstrably-pluggable generic VMS today, with ONVIF and future vendor-specific
  adapters ready to be implemented against the same interface.
- Real, tested integration with the official Gujarat Police camera catalogue endpoint
  contract (register-only, never auto-connects).
- An optional central camera-registry/VMS layer (Model 4) alongside the federation
  layer (Model 3) — the Hybrid architecture is not aspirational, both halves are real
  in this codebase today.
- **Real, live external camera grid integration (Sentinel Camera Grid,
  `cctv.corp8.cloud`)**: 30 real cameras discovered live, real RTSP connection to a
  real camera, real 1920×1080 frames, real YOLOv8+ByteTrack detections, real
  zone_entry alerts, real auto-created incidents, real Evidence snapshot files on
  disk — all directly inspected this session, not assumed from passing unit tests.
  See `FINAL_PROJECT_STATUS.md` for the exact numbers.

**Not safe to claim:**
- Statewide 80,000+ camera integration or throughput at that scale.
- Multiple concurrent Sentinel-Camera-Grid RTSP streams tested together (only ever
  one real grid camera connected at a time in live testing this pass — the
  pre-existing "2 concurrent local video_file cameras" finding is a separate,
  earlier result and does not extend to this external grid).
- Working ONVIF or any specific vendor VMS integration (interface only).
- Face recognition or identity resolution of any kind (appearance-similarity ranking
  only, always labeled as such).
- Forensic-grade evidence guarantees (packages are labeled prototype output with a
  real chain-of-custody trail, not a legal certification).
- Kafka/Kubernetes/vector-DB/edge-Jetson production deployment (documented as the
  long-term target architecture, not built).
