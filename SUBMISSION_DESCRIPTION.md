# SENTINEL VISION — Submission Description

Gujarat Police Innovation Challenge 2026.

## Problem

26+ Gujarat government departments run CCTV independently — different vendors, storage,
protocols, no shared intelligence layer. An officer investigating a vehicle or a person of
interest today cannot ask "where else has this been seen" across that fragmented
infrastructure, and raw video feeds alone don't produce actionable, explainable,
investigation-ready alerts.

## Solution

SENTINEL VISION is a video-intelligence layer that sits **on top of** existing cameras and
VMS platforms rather than replacing them: ingest → detect → track → read plates →
correlate across cameras → alert (explainably) → investigate → preserve evidence → audit.
Not an object-detection demo — the value is answering "where, when, and what happened"
around a vehicle or a restricted-zone event, with a defensible evidence trail.

## Architecture

**Hybrid / Innovative Architecture** — deliberately combines two of the three reference
models rather than picking one:

- **Model 3 (VMS Federation / Middleware)**: a formal `CameraAdapter` interface normalizes
  heterogeneous sources — real RTSP, webcam, uploaded video, a demonstrably-pluggable
  generic-VMS adapter, an honest ONVIF interface stub, and a real live external grid
  integration (see below). A new vendor plugs in as one new adapter class; nothing else
  in the pipeline changes.
- **Model 4 (Central VMS + AI Platform)**: a central camera registry, camera groups,
  per-camera and system health diagnostics, 5-role RBAC, audit logging, centralized
  alert/incident/evidence management.
- **Model 2 (Unified Viewing + Selective Analytics)** is the operator-facing surface of
  both: one camera grid, per-camera AI toggles editable in place, live view, detection
  overlays.

SENTINEL does not require replacing existing CCTV/VMS infrastructure. It consumes
heterogeneous camera/VMS sources through the adapter boundary and provides centralized AI
analytics, cross-camera event correlation, explainable alerting, investigation, and
evidence management on top of them.

## Key innovation

**Cross-camera intelligence with honest boundaries.** Vehicles correlate across cameras by
plate text (a real, timestamp-ordered route, not a guess). Persons correlate across
cameras by a lightweight, non-biometric appearance-similarity signature — explicitly
labeled as a ranked candidate list for manual review, never an identity claim. Every
alert carries a real `reasons[]` explaining exactly why it fired.

## AI capabilities

Real YOLOv8 person/vehicle detection, real ByteTrack tracking (one isolated tracker
instance per camera — verified under concurrent load, not just in theory), real EasyOCR
ANPR gated by format + confidence before a read is ever trusted, PTS-aware timestamping
(distrusts unreliable backends like webcam by design, never assumes constant frame rate),
and a rule engine (watchlist match, restricted-zone entry, loitering/dwell-time, all
schedule-window aware) — configurable via database rows, not hardcoded per-deployment.

## Investigation workflow

Search by plate, camera, time, or free-text (regex/keyword-parsed, not an LLM — stated
honestly). Vehicle cross-camera route with map visualization. Person appearance-similarity
search. Incident timeline aggregating every linked sighting/alert. Every workflow traces
back to a real camera, timestamp, confidence, and rule.

## Evidence workflow

Snapshot + bounded video clip (5s pre-event ring buffer + 10s post-event) attached to
every alert automatically — including bare restricted-zone alerts with no plate match, a
gap closed and live-verified this pass. Access requires a short-lived, resource-scoped
signed token issued only to an authenticated user; every access is audit-logged. SHA-256
verification on demand. Package export (JSON/PDF) includes the real audit trail for that
incident, labeled as prototype output, never a forensic-certainty claim.

## Real Sentinel Camera Grid validation

Beyond architecture and unit tests, this build was connected to a real, live, external
camera grid (30 real Ahmedabad-area traffic cameras) and the full chain was directly
verified: real login-gated discovery, real RTSP connection (1920×1080 @ 25fps), real
YOLOv8+ByteTrack detections (100 in one run across 4 object classes), real appearance
signatures (48/48 person detections), real zone_entry alerts (9), real auto-created
incidents (9), and real Evidence snapshot files confirmed on disk. Full numbers and method
in `FINAL_PROJECT_STATUS.md`.

## Scalability approach

Single FastAPI process + SQLite is the deliberate "working slice" for this stage — not
disguised as more than it is. What's architected to scale without a redesign: the adapter
interface (any number of sources), the catalogue-sync pattern (identical whether
registering 2 cameras or 30), and per-camera-isolated runtime state (no shared mutable
state between cameras). The documented statewide path — message queues, distributed GPU
inference workers, an event bus, object storage, a real search index, edge inference — is
intentionally not built here; the adapter boundary is where it attaches later. Full
target-architecture diagram and the mapping from today's real components to each future
layer: `HYBRID_ARCHITECTURE.md` §17.

## Security

Bcrypt password hashing, JWT sessions, 5-role RBAC enforced server-side (not just hidden
UI), short-lived resource-scoped tokens for browser-navigated file/stream endpoints
(never a raw unauthenticated link), upload hardening (generated filenames, extension
allow-list, streamed size cap), audit logging on every mutating action, login rate
limiting. External grid credentials live in a gitignored `.env` only — never in source,
docs, logs, or any API response; never reach the frontend.

## Limitations (stated plainly)

Not tested at statewide scale. ONVIF is an interface stub, not a working vendor
integration. No facial recognition anywhere, by design. No forensic/legal evidentiary
certification claimed. Appearance-similarity search will conflate similarly-dressed
people and is presented as such. Only one external grid camera connected concurrently in
live testing. No frontend automated test suite yet.

## Future roadmap

ONVIF discovery/auth against a real device. Additional rule types (direction-of-travel,
multi-event correlation). Configurable recording-retention policy. Distributed rate
limiting. Message-queue-based ingestion and distributed GPU inference workers for
statewide scale, behind the existing adapter boundary.
