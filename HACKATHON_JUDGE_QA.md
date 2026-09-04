# SENTINEL VISION — Judge Q&A

Concise, honest answers grounded in what's actually in this codebase and what was actually
run this session. Cross-references: `HYBRID_ARCHITECTURE.md` (architecture record),
`FINAL_PROJECT_STATUS.md` (verification table), `JUDGE_TALKING_POINTS.md`,
`JUDGE_DEMO_RUNBOOK.md`.

**1. Why Hybrid architecture?**
Because both halves are real, not aspirational. We built a formal adapter interface
(`pipeline/adapters.py`) that normalizes any camera/VMS source — that's Model 3 (VMS
Federation/Middleware). We also built a central camera registry, RBAC, audit log, health
dashboard, and evidence management — that's Model 4 (Central VMS+AI). Neither replaces
the other; a deployment can use either or both, which is what "Hybrid" actually means here.

**2. How does Model 2 work?**
Unified camera grid with per-camera selective analytics: each camera independently toggles
`ai_person`/`ai_vehicle`/`ai_anpr` (editable in place via `PATCH /api/cameras/{id}`, not
just at creation), camera groups for filtering, live MJPEG view, detection overlays, camera
health. Camera A can run person+vehicle while Camera B runs only ANPR — no forced
"process everything on every camera."

**3. How does Model 3 work?**
`CameraAdapter` is an abstract interface (`open`/`read`/`pos_msec`/`fps`/`resolution`/
`release`). Concrete adapters: `WebcamAdapter`, `VideoFileAdapter`, `RTSPAdapter` (real,
used by every camera in production here), `MockVMSAdapter` (real synthetic frames, proves
a "Generic VMS" is pluggable end-to-end), `ONVIFAdapter` (honest stub — registered,
dispatches correctly, fails loudly with a clear message since no ONVIF device was
available to test), and `SentinelGridAdapter` (real — see Q6). A new vendor adapter is one
new class registered in one factory dict; nothing in detector/ANPR/rules-engine changes.

**4. How does Model 4 work?**
Central `Camera` registry, camera provisioning (manual + two independent real catalogue
syncs — official Gujarat contract and the Sentinel Camera Grid), camera groups, per-camera
and system-wide health diagnostics, 5-role RBAC (Administrator/Control Room
Operator/Investigator/Supervisor/Auditor) enforced server-side, audit logging on every
mutating action, centralized alert/incident/evidence management.

**5. Are the cameras real?**
Yes, both catalogues are real integrations, not mocks:
- **Sentinel Camera Grid** (`cctv.corp8.cloud`): 30 real cameras, live-verified this
  session — real login, real discovery, real RTSP connection to `cam04` (1920×1080,
  25fps), real YOLOv8+ByteTrack detections (100 in one run: cars, trucks, motorbikes,
  people), real zone_entry alerts, real incidents, real Evidence snapshot files confirmed
  on disk.
- **Official Gujarat catalogue**: register-only client tested against the documented
  contract shape; live connectivity depends on the official sandbox host being supplied.
- The dashboard also supports webcam/uploaded-video-file sources for demo reliability
  when no external network is available.

**6. How did you integrate the Sentinel Camera Grid?**
Discovery isn't a bare public JSON file, despite appearances — `GET /cameras.json`
unauthenticated redirects to a session-cookie login page. `pipeline/sentinel_grid.py` logs
in (`POST /auth/login`, email/password from `.env`) inside one `httpx.AsyncClient` so the
session cookie carries automatically, then fetches `/cameras.json` and upserts the results
into the Camera registry — register-only, exactly like the official catalogue sync, never
auto-connects.

**7. How does RTSP authentication work?**
`SentinelGridAdapter.open()` builds `rtsp://<email>:<password>@host:port/stream/<id>` in
memory, fresh, only at connect time, from `.env` credentials plus the camera's bare grid
id. The URL is never stored in the database (`source_uri` holds just the id, e.g.
`"cam04"`), never logged, never returned by any API response, never sent to the frontend.
The `@` in the email is percent-encoded (`%40`) so it doesn't break URL parsing.

**8. Why RTSP TCP?**
The relevant sandbox documentation states UDP fails across NAT/firewalls. OpenCV's FFmpeg
backend has no per-`VideoCapture` API for transport selection, so we set the process-level
`OPENCV_FFMPEG_CAPTURE_OPTIONS=rtsp_transport;tcp` environment variable immediately before
every RTSP `VideoCapture` is constructed (not once at import — a test asserts the ordering).

**9. How do you handle PTS?**
`CAP_PROP_POS_MSEC` reliability is backend-dependent, and we don't pretend otherwise
(`pipeline/timing.py`). It's trusted for `video_file`/`rtsp`/`sentinel_grid` (real RTSP
underneath), never for `webcam` (documented as unreliable there). A trusted reading is
anchored to the wall-clock time the session opened to produce an absolute
`source_timestamp`, stored separately from `timestamp` (processing time) — never
conflated. A reading that hasn't advanced since the last one is rejected (guards a known
stuck/backward-jump failure mode). Verified live: real detections carry a real
`source_timestamp` distinct from processing time.

**10. How do you handle reconnects?**
Exponential backoff, capped: 1s/2s/4s/8s/16s/30s(max), never a tight loop. Per-camera
counters (attempts, successful reconnects, last error, current state) surfaced via
`/api/cameras/{id}/diagnostics`, including a richer connection-lifecycle state
(`CONNECTING`/`CONNECTED`/`PROCESSING`/`DEGRADED`/`RECONNECTING`/`DISCONNECTED`/
`AUTH_ERROR`/`ERROR`) kept separate from the stable 3-value DB status column.

**11. How do you scale to many cameras?**
Honestly: not attempted at scale in this build, and we say so. What *is* built to scale
without a redesign: the adapter interface (any number of adapters, one factory), the
catalogue-sync pattern (same endpoint whether it registers 2 cameras or 30), and per-camera
isolated state (own YOLO/tracker instance, own stats dict, own ring buffer — no shared
mutable state between cameras, so adding a camera never risks another one). The documented
path to statewide scale is message queues, distributed GPU workers, an event bus, object
storage, and a real search index — intentionally not built for a hackathon prototype, but
the adapter boundary is exactly where that would attach.

**12. What happens if one camera fails?**
Its own worker task degrades/reconnects/goes offline independently — verified live (a
camera pointed at a nonexistent device failed through backoff to offline while a second,
healthy camera kept running uninterrupted, `last_frame_at` advancing throughout). A
top-level supervisor (`_camera_loop_supervised`) guarantees even an unanticipated exception
marks the camera offline with a full traceback logged, rather than the task silently
vanishing.

**13. How does selective analytics reduce compute?**
Each camera only runs the detector classes an operator actually enabled
(`want_person`/`want_vehicle` passed into YOLO's class filter) and only runs ANPR OCR on
vehicle-class detections when `ai_anpr` is on for that camera. A camera set to
vehicle-only never pays for person-detection inference at all.

**14. How does ANPR work?**
Real EasyOCR on the vehicle detection's cropped bounding box, gated before it's ever
trusted: a normalized read only becomes a `Vehicle`/`Plate` database record if it matches
a plausible Indian plate format regex AND clears a minimum confidence threshold — a single
noisy OCR frame is never persisted as a sighting.

**15. How does tracking work?**
Ultralytics' built-in ByteTrack via `model.track(persist=True)`, one YOLO/tracker instance
per camera (never shared — concurrent cameras can't corrupt each other's track IDs,
verified under test and live). `track_id` persists across frames for the same object
within one camera; a scene cut (video loop) produces a fresh low track id afterward, not a
corrupted one — verified from real per-frame data.

**16. Is appearance similarity facial recognition?**
No, explicitly not. `pipeline/appearance.py` computes a compact HSV color-histogram
signature of a person's bounding-box crop — a non-biometric visual-similarity signal
(clothing/color, roughly). `GET /api/persons/{id}/similar` returns a ranked candidate list
for an investigator to review manually; it never claims to know who anyone is, and every
UI surface for it carries an explicit "not identity verification" banner.

**17. How is evidence preserved?**
Every alert now gets a real snapshot: the ANPR/watchlist path already had one; a
hardening pass closed the gap for bare zone_entry alerts (no plate match) by capturing one
from the actual processed frame the alert fired on, whenever none exists yet — verified
live (13 real Evidence rows, one confirmed on disk, 737,865 bytes, a real JPEG). Every
alert also gets a bounded MP4 clip (5s pre-event ring buffer + 10s post-event) from the
camera's own live buffer. Evidence file/package access requires a short-lived signed
resource token issued only to an authenticated user, and every access is logged to the
audit trail — never a raw, unauthenticated file link. `sha256` verification is available
on demand.

**18. How do you prevent evidence filename collisions?**
`{camera_code}_{alert_id}_{microsecond-timestamp}.jpg` — `alert_id` is a globally-unique
generated id, so collision is not just unlikely, it's structurally impossible even across
concurrent cameras firing in the same microsecond. Tested directly (two cameras, same
instant, distinct files, both exist on disk).

**19. How is user access controlled?**
JWT-based sessions, bcrypt-hashed passwords, 5 roles enforced server-side on every
sensitive endpoint (not just hidden in the UI — a wrong-role request gets a real 403).
Short-lived, resource-scoped signed tokens for the handful of endpoints browsers hit via
plain `<img>`/`<a>` navigation (evidence files, camera streams) instead of dropping auth
for them. Login rate-limited (5 failed attempts/60s per username). Upload hardening
(server-generated filenames, extension allow-list, streamed size cap).

**20. What is actually implemented vs future?**
See `FINAL_PROJECT_STATUS.md` for the full itemized table. Short version — implemented and
live-verified: discovery, RTSP, AI pipeline, PTS, alerts, incidents, evidence, RBAC,
audit. Implemented but not live-verified at scale: official-catalogue live connectivity
(depends on sandbox host being supplied), reconnect under a real dropped grid stream.
Prototype: recording retention policy, loitering/schedule rules (built and tested, not
live-fired against the real grid). Future: ONVIF (interface only), statewide
Kafka/K8s/vector-DB architecture, facial recognition (deliberately never planned).

**21. What is the biggest limitation?**
Scale is unproven beyond a small number of cameras (2 concurrent local, 1 real external
grid camera at a time) — the architecture is designed not to need a redesign to scale,
but that's a design claim, not a load-test result, and we don't blur that line.

**22. Why is this better than simply using a VMS?**
A VMS shows you video. It doesn't detect, track, read plates, correlate a sighting across
cameras, explain why an alert fired, or hand an investigator a chain-of-custody evidence
package. SENTINEL sits on top of whatever VMS/cameras already exist and adds exactly that
intelligence layer — without asking a department to rip out their existing investment.

**23. How does SENTINEL work with existing VMS systems?**
Through the adapter interface (Model 3): a VMS's RTSP/ONVIF output, or its own catalogue
API, is normalized behind `CameraAdapter` the same way the official Gujarat catalogue and
the Sentinel Camera Grid already are — two independent real examples of exactly this
pattern working, not a hypothetical.

**24. What happens at 80,000+ camera scale?**
Not attempted, not claimed as tested. The documented target architecture (message
queues, distributed GPU inference workers, an event bus, object storage, a real search
index, edge inference) is exactly what the adapter/registry boundary in this build is
designed to sit behind without a rewrite — but that boundary being in place is a design
property we can demonstrate today, not a proof of statewide throughput.
