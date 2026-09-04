# SENTINEL VISION — Judge Talking Points

Factual. No number claimed here has not been demonstrated.

## 1. Problem

26 Gujarat government departments run CCTV independently — different vendors, storage,
protocols, no shared intelligence layer. An officer investigating a vehicle today cannot ask
"where else has this been seen" across that infrastructure.

## 2. SENTINEL's solution

A catalogue-driven video intelligence layer that sits on top of existing cameras rather than
replacing them: ingest → detect → track → read plates → correlate across cameras → alert →
investigate → evidence → audit. Not just object detection — the value is answering "where,
when, and what happened around this vehicle."

## 3. Existing CCTV infrastructure integration

Camera onboarding is catalogue-driven: `GET {host}/api/ingest` (the official contract) is
consumed, normalized, and upserted into a Camera Registry — idempotent, never auto-connects
what it registers, never deletes a camera the catalogue stops listing (marks it stale instead,
preserving its history). RTSP-over-TCP, WHEP, and HLS URLs are all preserved from the
catalogue; RTSP is what feeds the AI pipeline, matching the official guidance.

## 4. AI detection, tracking, ANPR

Real YOLOv8 person/vehicle detection, real ByteTrack tracking (isolated per camera — verified
under concurrent load, not just in theory), real EasyOCR plate reads gated by format+confidence
before they're trusted (a noisy single-frame read never becomes a database record).

## 5. Cross-camera intelligence — the differentiator

A plate seen on one camera and later on another resolves to the same Vehicle record, with a
real, timestamp-ordered route across cameras. Labeled honestly: **ANPR-assisted cross-camera
correlation**, not visual re-identification — we don't claim to recognize a vehicle by its
image across cameras, only by its plate.

## 6. Explainable alerts

Every alert carries a `reasons[]` list built from the actual rule that fired — not a canned
string. Severity, confidence, source camera, and timestamp are all shown together.

## 7. Human-in-the-loop

A watchlist match is a **potential match** requiring officer review — Confirm / Reject / Needs
Review — never an automatic, irreversible action. Repeat detections of the same
track/zone/watchlist entry are cooldown-deduplicated so operators aren't flooded.

## 8. Evidence + audit

Snapshots and bounded video clips (5s pre-event + 10s post-event, from a small per-camera ring
buffer — never a full recording) attach to every alert/incident. All evidence access goes
through short-lived signed tokens issued only to an authenticated, authorized user — never a
raw file link — and every access is logged. Evidence packages include the real audit trail for
that incident, not a label claiming one.

## 9. Security / RBAC

Five roles (Administrator, Control Room Operator, Investigator, Supervisor, Auditor) enforced
server-side on every sensitive endpoint — verified live, not just in code, including a 403 on
a restricted action taken by the wrong role. Passwords bcrypt-hashed, JWT-based sessions,
upload validation (UUID filenames, extension allow-list, size cap, no path traversal), demo
accounts gated behind a `DEMO_MODE` flag separate from production behavior.

## 10. Scalability architecture

Single FastAPI process + SQLite is the deliberate hackathon-scale "working slice" — the
documented long-term path (the statewide 80,000-camera target) is Kafka/Kubernetes/vector-DB/
edge inference, intentionally not built for this stage. The catalogue-driven onboarding
pattern is what actually needs to scale, and it doesn't change shape between 2 cameras and
50 — same sync endpoint, same registry, same per-camera start/stop.

## 11. What is empirically verified right now

Live-tested, this build, on this hardware: 2 concurrent AI-processed camera streams (real
YOLOv8 + ByteTrack + EasyOCR on both, isolated tracker state, automatic reconnect with
exponential backoff, one camera's failure/reconnect cycle proven not to affect the other) —
the full detect → track → ANPR → cross-camera correlation → explainable alert → incident →
snapshot + video-clip evidence → audit chain, end to end, repeatably, via a documented
demo-reset/trigger mechanism. H.264 and H.265 decoding confirmed. RTSP-over-TCP client
configuration confirmed at the code level.

## 12. Update — real external camera grid now live-verified

Since the above was written, this build was connected to a **real, live external camera
grid** (the Sentinel Camera Grid, `cctv.corp8.cloud` — 30 real Ahmedabad-area traffic
cameras) and the full chain was directly verified, not assumed: real RTSP connection, real
1920×1080 frames, real YOLOv8+ByteTrack detections, real zone_entry alerts, real
auto-created incidents, and (after a hardening pass) real Evidence snapshot files
confirmed on disk. Full numbers in `FINAL_PROJECT_STATUS.md`.

**Precise wording matters here**: this is a real live camera grid, wire-level RTSP
confirmed working — but it is **not** confirmed to be the same host as the official
Gujarat Police competition sandbox unless organizers say so. Treat it as proof the
architecture and RTSP client genuinely work against a real, external, authenticated
camera source under real network conditions — not as proof of connectivity to the
specific official sandbox, which remains a separate claim.

**"SENTINEL VISION has been live-verified against a real external camera grid — real RTSP
connection, real AI detection/tracking, real alerts, incidents, and evidence, not
simulated. Two concurrent AI-processed *local* camera streams were separately verified
earlier in the build (see below). The camera integration architecture is catalogue-driven
and adapter-based, compatible with the official RTSP/TCP model, and provably works against
a real live source under real network conditions."**

Not claimed: 50+ cameras run concurrently, 80,000-camera scale tested, multiple concurrent
grid RTSP streams (only one grid camera was ever connected at a time in this pass's live
testing), connectivity confirmed specifically to the official Gujarat government sandbox
host as opposed to this real external grid.

Below (original, still accurate) — the earlier local-camera concurrency finding this was
built on:
