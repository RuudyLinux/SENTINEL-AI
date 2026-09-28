"""Central settings. Secrets come from .env; no Vault/KMS in this build."""
import secrets
from pathlib import Path

import cv2
from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent

# never accepted as a production JWT secret: the bundled dev default and the
# usual placeholders people paste in
_INSECURE_JWT_SECRETS = {
    "sentinel-vision-dev-secret-change-in-production", "", "changeme", "secret", "password",
}
_MIN_PRODUCTION_JWT_SECRET_LENGTH = 32


class Settings(BaseSettings):
    jwt_secret: str = "sentinel-vision-dev-secret-change-in-production"
    jwt_algorithm: str = "HS256"
    access_token_minutes: int = 480

    # comma-separated so it stays a plain env var
    cors_allowed_origins: str = "http://localhost:3000"

    # Empty = SQLite at db_path (dev and the test suite). Production sets a
    # PostgreSQL URL, e.g. postgresql+psycopg://user:pass@host:5432/sentinel
    database_url: str = ""
    # Pool sizing for both backends (db.py _engine_kwargs). Every running
    # camera worker holds a Session, so one connection, for the life of its
    # stream; the pool has to be bigger than the worker count. SQLite used to
    # get SQLAlchemy's default 5+10 and bulk-starting cameras hit
    # "QueuePool limit of size 5 overflow 10 reached". A held SQLite
    # connection is just a file handle, so no reason to go smaller.
    db_pool_size: int = 20
    db_max_overflow: int = 10
    # recycle before server/proxy idle timeouts kill a long-lived worker
    # connection. PostgreSQL only
    db_pool_recycle_seconds: int = 1800

    db_path: Path = BASE_DIR / "sentinel.db"
    uploads_dir: Path = BASE_DIR / "uploads"
    evidence_dir: Path = BASE_DIR / "evidence_store"

    # Detection. Picked on 24 audited real grid frames (docs/AI_ACCURACY.md):
    # yolo11s @ 960px, conf 0.30, IoU 0.5 gave vehicle P 0.860 / R 0.508 and
    # person P 0.959 / R 0.538, vs 0.842 / 0.354 and 1.000 / 0.288 for
    # yolov8s @ 640 / conf 0.40. yolov8m matched it at 3x the CPU (349 vs
    # 123 ms/frame). Weights download on first use.
    model_name: str = "yolo11s.pt"
    # Stamped on every detection and evidence row. Empty derives it from
    # model_name so switching models can't leave evidence naming the old one.
    # Set it only for a custom model's own version string.
    model_version: str = ""
    rule_version: str = "rules-1.0"
    # inference on every Nth frame. None resolves by hardware below (3 CPU, 1 CUDA)
    detect_every_n_frames: "int | None" = None
    confidence_threshold: float = 0.30
    detector_imgsz: int = 960
    detector_iou: float = 0.5
    # Boxes from this confidence go to ByteTrack (its second association stage
    # keeps tracks alive through glare), only >= confidence_threshold get
    # published. With bytetrack_sentinel.yaml track fragments went 30% -> 0%
    # by day and 40% -> 35% at night.
    tracker_feed_confidence: float = 0.10
    # Cameras allowed to run AI at once (pipeline/ai_capacity.py). One AI
    # camera on CPU holds ~92% CPU on the demo machine, a second saturates it.
    # With CUDA every camera shares one model on the GPU (detector.py), so all
    # of them can run AI and split its throughput. None resolves by hardware
    # below: 1 on CPU, 64 (i.e. every camera) with CUDA.
    max_ai_cameras: "int | None" = None
    # Seconds a camera keeps an AI slot before handing it to a waiting camera,
    # so with every camera connected they all get AI in turn. 0 = fixed slots:
    # whoever gets one keeps it, and the API refuses AI on a full machine.
    ai_rotation_seconds: float = 60.0
    tracker_config: str = str(Path(__file__).resolve().parent / "pipeline" / "bytetrack_sentinel.yaml")

    # a read only becomes a Vehicle/Plate record if it looks like a plate and
    # clears this
    plate_min_confidence: float = 0.35

    # A watchlist match on a read below this is capped at HIGH instead of
    # CRITICAL and the alert says it needs confirmation, so a 48% read isn't
    # treated like a 94% one. The match still fires. Sits between
    # plate_min_confidence (recorded at all) and plate_stable_confidence
    # (fusion trusts it), i.e. passed the gate but not corroborated yet.
    watchlist_high_confidence_floor: float = 0.60

    # CRITICAL also needs the read corroborated across frames
    # (plate_tracker.has_consensus), not just confident.
    #
    # OCR confidence doesn't separate right from wrong reads on the benchmark
    # (docs/ANPR_ACCURACY.md, "A1"): correct 0.262-0.990, wrong plate-shaped
    # 0.260-0.956, 6 of 7 wrong reads at or above the lowest correct one. No
    # threshold gets precision over 0.5. UP84AE9889 read as UP81AE9889 at 0.956
    # would open an incident on a car that was never there.
    #
    # On by default, a wrongful stop is worse than a delayed one. The match
    # still fires at HIGH labelled UNCORROBORATED. False = confidence only.
    watchlist_require_corroboration: bool = True

    # Plate sightings below this are stored and marked pending_review for the
    # review queue. Same value as watchlist_high_confidence_floor, separate
    # setting because this applies to every plate, not just watchlist hits.
    plate_review_confidence_floor: float = 0.60

    # V2 plate pipeline (localization + per-track voting). False = whole crop
    # -> OCR -> one Plate row per passing frame, the pre-V2 behaviour. V2
    # changes OCR cost and row counts, so it's revertable from one env var.
    plate_pipeline_v2: bool = True
    # Dedicated plate detector. On 17 real tracked vehicles
    # (docs/AI_ACCURACY.md) it found a plate on 9 vs 2 for the classical
    # localizer and lifted character accuracy on readable plates 0.00 -> 0.58
    # with no wrong plate published. Weights: morsetechlab
    # yolov11-license-plate-detection, AGPL-3.0 (same as ultralytics). Not in
    # git, never auto-downloaded (README "Plate detector"). Missing file =
    # logged, falls back to classical localization (pipeline/plate_detect.py).
    plate_model_name: str = "license-plate-finetune-v1n.pt"
    plate_detect_confidence: float = 0.25
    # With the trained detector, a crop where it finds no plate isn't OCR'd
    # whole. That fallback is for the classical localizer, which misses a lot;
    # a detector miss almost always means nothing legible, and the fallback
    # was ~200ms per vehicle per frame (OCR was 452 of 661ms).
    plate_whole_crop_fallback_with_model: bool = False
    # Crops are upscaled to this glyph height before OCR. Glyph height matters
    # far more than which OCR engine on real CCTV.
    plate_ocr_target_height: int = 64

    # Preprocessing variants (pipeline/plate_preprocess.py), comma-separated
    # names from VARIANT_NAMES. Each one is a full extra OCR pass per plate,
    # the priciest thing in the loop, so the default is the single variant
    # matching the old behaviour (upscale -> gray -> CLAHE).
    #
    # On the 25-plate corpus "original,sharpen,adaptive" takes CER
    # 0.3896 -> 0.3030 at 3x the cost, but exact match moves by one sample,
    # inside the noise. Opt-in for recovery/diagnostics only.
    plate_preprocess_variants: str = "clahe"
    # used when the above is empty or invalid, so OCR always gets one image
    plate_preprocess_default_variant: str = "clahe"

    # Persistence gate (pipeline/plate_tracker.py): how many passing reads must
    # agree on the text before it becomes a Vehicle/Plate sighting. 1 = persist
    # on the first read, 2 = one lucky frame can't make a vehicle record.
    # Reads under the threshold stay in the track's accumulator as untrusted.
    plate_min_observations: int = 2
    # What to do with a read that never gets there.
    # False (default): persist it anyway, forced to pending_review, so a car
    # seen in a single cycle isn't lost.
    # True: don't persist uncorroborated reads at all. Off by default,
    # silently dropping real observations is worse for investigations.
    plate_require_consensus: bool = False
    # Variants that must agree before a multi-variant read counts as
    # corroborated; unused with a single variant. On the corpus <=2 of 7
    # agreeing was right 0/13 times, >=5 of 7 was right 4/4.
    plate_min_variants_agreeing: int = 2

    # Save the plate region OCR read next to the evidence snapshot so a
    # reviewer can check it. One small image per new sighting; opt-in for
    # storage reasons.
    plate_debug_crops: bool = False
    # a track's plate is settled once this many agreeing reads clear this peak
    # confidence; until then every cycle re-reads it
    plate_min_reads_for_stability: int = 3
    plate_stable_confidence: float = 0.60
    # once settled, re-verify at most this often. main OCR cost saving
    plate_reverify_seconds: float = 10.0
    # ByteTrack never says a track id is gone, this TTL is what bounds the
    # accumulator on a camera running for days
    plate_track_ttl_seconds: float = 120.0
    # refresh rate for a still-visible vehicle's sighting row (last_seen/
    # dwell), so a parked car isn't rewritten every frame
    plate_sighting_refresh_seconds: float = 5.0

    # A new CRITICAL alert for a vehicle with an open incident inside this
    # window joins that incident. 15 min covers a run across several cameras;
    # a visit hours later is a new event.
    incident_correlation_window_seconds: float = 900.0
    # Zone alerts only from detections at least this confident. Boxes from
    # confidence_threshold up are still tracked and drawn; the benchmark shows
    # precision rising with the threshold (yolo11s @ 960: 0.80 at 0.25, 0.86
    # at 0.30), and an alert costs an operator more than a box on screen.
    zone_alert_min_confidence: float = 0.40
    # A tracked object has to be seen inside the zone on this many inference
    # frames before zone_entry fires, so a one-frame ghost box doesn't raise a
    # CRITICAL. Untracked detections (no ByteTrack id) can't be counted and
    # fire as before. 1 = old behaviour.
    zone_entry_min_frames: int = 2
    # Seen more recently than this = LIVE. Older, the UI says "last known".
    vehicle_live_window_seconds: float = 120.0

    # ws.py: detections go out as one batch per interval, otherwise N cameras
    # x inference rate React updates per second per dashboard. 250ms still
    # feels instant. Alerts and incidents are never batched.
    ws_batch_interval_seconds: float = 0.25
    # cap per batch; if flushing stalls the oldest are dropped and the batch
    # reports the real count
    ws_batch_max_events: int = 200

    # How long shutdown waits for background work (clip encodes, self-heal
    # writes) before cancelling. Above clip_post_event_seconds so a clip
    # already collecting frames can finish its Evidence row.
    shutdown_drain_seconds: float = 15.0

    # Bearer token for a Prometheus scraper. Empty = /api/metrics needs an
    # Administrator JWT. No anonymous mode, the numbers are operationally
    # sensitive. .env only.
    metrics_token: str = ""

    # reconnect backoff, for a failed initial open and a mid-stream drop
    reconnect_max_attempts: int = 5
    reconnect_backoff_base: float = 1.0
    reconnect_backoff_max: float = 30.0
    # Consecutive bad reads before a real reconnect. h264 decode errors on
    # live RTSP cluster during network jitter, so 8 (was 3) rides out
    # ~250-300ms of bad patch at 30fps and still catches a dead stream in
    # under a second.
    read_failures_before_reconnect: int = 8

    max_upload_mb: int = 500
    allowed_video_extensions: tuple[str, ...] = (".mp4", ".avi", ".mov", ".mkv", ".webm")

    # Demo accounts get seeded only when true. Default on so the documented
    # local demo (admin/sentinel123 ...) just works; DEMO_MODE=false for a
    # real deploy.
    demo_mode: bool = True

    # short-lived signed tokens for things browsers load via plain <img>/<a>
    # and can't attach a bearer header to
    evidence_token_ttl_seconds: int = 300
    stream_token_ttl_seconds: int = 3600

    # Gujarat Police camera catalogue. No host hardcoded; sync fails with a
    # clear error while empty
    camera_catalog_base_url: str = ""
    camera_catalog_timeout_seconds: float = 8.0

    # Sentinel Camera Grid. Host and RTSP endpoint were given publicly for
    # this project so they default here. Email/password are secret: .env
    # only, never logged or returned. Empty = grid not configured, and callers
    # fail clearly instead of doing nothing.
    sentinel_grid_base_url: str = "https://cctv.corp8.cloud"
    sentinel_grid_email: str = ""
    sentinel_grid_password: str = ""
    sentinel_grid_rtsp_host: str = "103.250.160.189"
    sentinel_grid_rtsp_port: int = 8554
    # a real login sometimes timed out at 8s and then worked at 20s on retry,
    # network jitter to the grid
    sentinel_grid_timeout_seconds: float = 20.0

    # 24/7 auto-connect supervisor. Only keeps grid cameras' RTSP connections
    # up; it never writes ai_* flags (sentinel_grid.py's upsert sets those).
    #
    # Every registered camera stays connected, so the cap covers the whole
    # catalog. What protects the grid from a connection burst is the stagger
    # below, not this cap: at scale the limit was the grid's tolerance for
    # simultaneous connects, not local CPU/RAM once the thread pool was sized
    # to the camera count. Re-measure before going past 100.
    sentinel_grid_autoconnect: bool = True
    sentinel_grid_max_autoconnect: int = 100
    sentinel_grid_supervisor_sweep_seconds: float = 30.0
    # After a real AUTH_ERROR the supervisor backs off this long. It's one
    # shared login, so retrying per camera just hammers the same endpoint.
    sentinel_grid_auth_cooldown_seconds: float = 300.0
    # Delay between worker starts in one sweep (not before the first). Opening
    # every RTSP handshake at once was measurably less reliable in a
    # 10-camera test. 3s is conservative, re-measure before lowering.
    sentinel_grid_stagger_seconds: float = 3.0

    # Max in-flight POST /api/cameras/test-connection probes. Each holds a
    # thread from the shared to_thread pool for up to
    # source_open_timeout_seconds (8 dead-URI probes took exactly 20.0s each),
    # and the same pool runs every camera's reads, inference and commits. Past
    # the cap it's a fast 429, not a queue.
    camera_test_connection_max_concurrent: int = 3

    # Size of asyncio's default executor, which every blocking call here
    # shares: camera reads, inference, DB commits (db_retry), hashing, probes.
    #
    # Python's default is min(32, cpu_count + 4), 20 on a 16-core box, no
    # matter how many cameras. With 34 cameras connected the online count
    # bounced (12, then 4) with no errors anywhere: reads weren't failing,
    # they were waiting for a thread. Looks exactly like a dead stream.
    #
    # 0 = size for the workload: one thread per camera plus headroom, capped.
    # Threads blocked on a socket cost memory, not CPU. Positive = pin it.
    worker_thread_pool_size: int = 0
    # added to the camera count so API, DB and inference still get threads
    worker_thread_pool_headroom: int = 24
    worker_thread_pool_max: int = 160

    # Distributed runtime state (app/runtime_state.py).
    # Empty: alert cooldown, self-heal dedup and login rate limiting live in
    # each process's memory, which is right for exactly one process.
    # A Redis URL (e.g. redis://redis:6379/0) shares them across processes: no
    # double-fired alerts, and a restart doesn't clear cooldowns or lockouts.
    # Configured but unreachable at startup fails open to in-process, logged
    # once, never fatal.
    redis_url: str = ""
    # startup ping budget. short because it blocks startup, and a slow Redis
    # is as good as none here
    redis_connect_timeout_seconds: float = 2.0

    # Egress policy for camera sources (pipeline/egress_policy.py). True
    # refuses sources resolving to loopback, link-local, private, reserved,
    # multicast or unspecified addresses.
    #
    # Off by default: real camera LANs live on exactly those private ranges,
    # and the endpoints already need Administrator or Control Room Operator.
    # Turn it on where the backend must never reach internal infrastructure.
    camera_source_block_private_networks: bool = False

    # the official sandbox needs TCP ("UDP fails across NAT/firewalls")
    rtsp_force_tcp: bool = True
    # Transport for Sentinel Grid cameras. Measured 2026-09-28 against the
    # grid: over TCP the whole account topped out at ~5 Mbit/s (30 streams
    # opened, 10 delivering, the rest at 0 fps and reconnecting); over UDP 30
    # streams pulled 51 Mbit/s with 27 delivering. TCP over that long path is
    # the bottleneck, not the grid. "tcp" if a network blocks UDP.
    sentinel_grid_rtsp_transport: str = "udp"
    # UDP socket buffer per stream (ffmpeg buffer_size)
    rtsp_udp_buffer_bytes: int = 4_194_304

    # CAP_PROP_OPEN_TIMEOUT_MSEC is set on RTSP captures (source.py) but not
    # every OpenCV/FFmpeg build honors it: a dead source hung ~30s with 5s
    # set, so worker.py enforces this at the asyncio level too.
    # Separately, a healthy grid stream (cam04) took 9.8-13.8s to handshake
    # through the relay; 8s and 15s both cut off real connections. 20s for margin.
    source_open_timeout_seconds: float = 20.0

    # event clips: bounded ring buffer and bounded post-event wait, never a
    # full recording
    clip_pre_event_seconds: float = 5.0
    clip_post_event_seconds: float = 10.0
    clip_fps: float = 10.0  # nominal playback rate; source frames may be variable-interval

    # Manual recordings (REC button, pipeline/recorder.py): the annotated live
    # view written to MP4 and stored as hashed evidence. Capped in length and
    # in how many run at once, each one is an ffmpeg encode.
    recording_max_seconds: float = 1800.0
    recording_max_concurrent: int = 4
    recording_fps: float = 10.0

    # Days before evidence can be purged via POST /api/governance/purge-expired.
    # Not a compliance claim: which period applies depends on jurisdiction,
    # agency policy and open investigations, so an admin has to set it (see
    # docs/PRIVACY_GOVERNANCE.md). None = keep forever, nothing auto-deleted.
    evidence_retention_days: int | None = None
    # Raw detections older than this can be purged by an Administrator (POST
    # /api/governance/purge-detections). None = never. Detections referenced
    # by an alert, plate read or evidence item are never eligible. Volume is
    # about 5 rows/s per AI camera, ~430k/day/camera.
    detection_retention_days: int | None = None

    model_config = SettingsConfigDict(env_file=".env")

    @model_validator(mode="after")
    def _derive_model_version(self) -> "Settings":
        if not self.model_version:
            self.model_version = f"{Path(self.model_name).stem}-coco-1.0"
        return self

    @model_validator(mode="after")
    def _resolve_ai_rate_for_hardware(self) -> "Settings":
        # Measured live on GRID-cam02, 2026-09-28 (docs/AI_ACCURACY.md): CPU at
        # every 3rd frame did 1.6 AI frames/s at ~58% system CPU, and every
        # frame doesn't help since inference already saturates it. RTX 3050 Ti
        # did 3.5 at every 3rd and 5.8 at every frame at ~11% CPU.
        if self.detect_every_n_frames is None or self.max_ai_cameras is None:
            try:
                import torch
                cuda = bool(torch.cuda.is_available())
            except Exception:
                cuda = False
            if self.detect_every_n_frames is None:
                self.detect_every_n_frames = 1 if cuda else 3
            if self.max_ai_cameras is None:
                self.max_ai_cameras = 64 if cuda else 1
        return self

    @model_validator(mode="after")
    def _enforce_production_jwt_secret(self) -> "Settings":
        # DEMO_MODE=false means a real deploy (seed.py gates demo data on it
        # too), and that must not run on the bundled or a placeholder secret.
        # Fails at import, before serving anything.
        insecure = self.jwt_secret in _INSECURE_JWT_SECRETS or len(self.jwt_secret) < _MIN_PRODUCTION_JWT_SECRET_LENGTH
        if self.demo_mode:
            # The bundled secret is public in this repo, anyone could mint an
            # Administrator token with it. Use a random per-process one; a
            # restart logs everyone out, which a demo can live with.
            if insecure:
                self.jwt_secret = secrets.token_urlsafe(48)
        elif insecure:
            raise RuntimeError(
                "DEMO_MODE=false (production mode) requires a real JWT_SECRET — at least "
                f"{_MIN_PRODUCTION_JWT_SECRET_LENGTH} characters, not the bundled dev default "
                "or a placeholder. Set JWT_SECRET in the environment/.env, e.g.:\n"
                '  python -c "import secrets; print(secrets.token_urlsafe(32))"'
            )
        return self


settings = Settings()
settings.uploads_dir.mkdir(parents=True, exist_ok=True)
settings.evidence_dir.mkdir(parents=True, exist_ok=True)

# Not the fix for the FFmpeg "fctx->async_lock failed" crash in tests (that
# was workers left running between tests, fixed in conftest's client
# fixture). Kept because the live server already ends up at 1 thread via
# torch's OpenMP init; this just makes it explicit.
cv2.setNumThreads(1)
