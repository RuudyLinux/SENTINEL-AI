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
    # Connection pool (db.py). Each running camera worker holds a session for
    # the life of its stream, so the pool must exceed the worker count.
    db_pool_size: int = 20
    db_max_overflow: int = 10
    # recycle before server/proxy idle timeouts kill a long-lived worker
    # connection. PostgreSQL only
    db_pool_recycle_seconds: int = 1800

    db_path: Path = BASE_DIR / "sentinel.db"
    uploads_dir: Path = BASE_DIR / "uploads"
    evidence_dir: Path = BASE_DIR / "evidence_store"

    # Detection model and thresholds, chosen on the grid-frame benchmark in
    # docs/AI_ACCURACY.md. Weights download on first use.
    model_name: str = "yolo11s.pt"
    # Stamped on every detection and evidence row. Empty derives it from
    # model_name; set it only for a custom model.
    model_version: str = ""
    rule_version: str = "rules-1.0"
    # inference on every Nth frame. None resolves by hardware below (3 CPU, 1 CUDA)
    detect_every_n_frames: "int | None" = None
    confidence_threshold: float = 0.30
    detector_imgsz: int = 960
    detector_iou: float = 0.5
    # Boxes from this confidence feed ByteTrack (its second association stage
    # keeps tracks through glare); only >= confidence_threshold are published.
    tracker_feed_confidence: float = 0.10
    # Cameras allowed to run AI at once (ai_capacity.py). None resolves by
    # hardware: 1 on CPU, 64 with CUDA, where all cameras share one model.
    max_ai_cameras: "int | None" = None
    # Seconds a camera keeps an AI slot before handing it to a waiting camera.
    # 0 = fixed slots, and AI is refused when the machine is full.
    ai_rotation_seconds: float = 60.0
    tracker_config: str = str(Path(__file__).resolve().parent / "pipeline" / "bytetrack_sentinel.yaml")

    # a read only becomes a Vehicle/Plate record if it looks like a plate and
    # clears this
    plate_min_confidence: float = 0.35

    # A watchlist match on a read below this is capped at HIGH and labelled as
    # needing confirmation. The match still fires.
    watchlist_high_confidence_floor: float = 0.60

    # CRITICAL also requires the read to be corroborated across frames. OCR
    # confidence alone doesn't separate right from wrong reads (see
    # docs/ANPR_ACCURACY.md), and a false CRITICAL risks a wrongful stop.
    # False = confidence only.
    watchlist_require_corroboration: bool = True

    # Plate sightings below this are stored as pending_review. Applies to every
    # plate, not only watchlist hits.
    plate_review_confidence_floor: float = 0.60

    # V2 plate pipeline (localization + per-track voting). False = one Plate row
    # per passing frame from whole-crop OCR.
    plate_pipeline_v2: bool = True
    # Trained plate detector (morsetechlab yolov11-license-plate-detection,
    # AGPL-3.0). Not in git and never auto-downloaded; see README. If the file
    # is missing, classical localization is used (plate_detect.py).
    plate_model_name: str = "license-plate-finetune-v1n.pt"
    plate_detect_confidence: float = 0.25
    # With the trained detector, a crop with no detected plate isn't OCR'd
    # whole; a detector miss almost always means nothing legible.
    plate_whole_crop_fallback_with_model: bool = False
    # Crops are upscaled to this glyph height before OCR. Glyph height matters
    # far more than which OCR engine on real CCTV.
    plate_ocr_target_height: int = 64

    # Preprocessing variants (plate_preprocess.VARIANT_NAMES), comma-separated.
    # Each is a full extra OCR pass, so the default is a single variant; more
    # variants are for diagnostics (see docs/ANPR_ACCURACY.md).
    plate_preprocess_variants: str = "clahe"
    # used when the above is empty or invalid, so OCR always gets one image
    plate_preprocess_default_variant: str = "clahe"

    # Passing reads that must agree before a plate becomes a vehicle sighting.
    # 2 means one lucky frame can't create a vehicle record.
    plate_min_observations: int = 2
    # Reads that never reach consensus. False: persist as pending_review, so a
    # car seen once isn't lost. True: drop them.
    plate_require_consensus: bool = False
    # Variants that must agree before a multi-variant read counts as
    # corroborated; unused with a single variant.
    plate_min_variants_agreeing: int = 2

    # Save the OCR'd plate region next to the snapshot for reviewers (opt-in,
    # one small image per sighting).
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

    # A CRITICAL alert for a vehicle with an open incident inside this window
    # joins that incident instead of opening a new one.
    incident_correlation_window_seconds: float = 900.0
    # Minimum confidence for zone alerts. Lower-confidence boxes are still
    # tracked and drawn; an alert costs an operator more than a box on screen.
    zone_alert_min_confidence: float = 0.40
    # Inference frames a tracked object must be seen in a zone before
    # zone_entry fires, so one-frame ghost boxes don't alert. Untracked
    # detections can't be counted and fire immediately.
    zone_entry_min_frames: int = 2
    # Seen more recently than this = LIVE. Older, the UI says "last known".
    vehicle_live_window_seconds: float = 120.0

    # Detections are broadcast in batches at this interval (ws.py); alerts and
    # incidents are sent immediately.
    ws_batch_interval_seconds: float = 0.25
    # cap per batch; if flushing stalls the oldest are dropped and the batch
    # reports the real count
    ws_batch_max_events: int = 200

    # How long shutdown waits for background work before cancelling. Longer than
    # clip_post_event_seconds so an in-progress clip can finish.
    shutdown_drain_seconds: float = 15.0

    # Bearer token for a Prometheus scraper (.env only). Empty = /api/metrics
    # requires an Administrator JWT; there is no anonymous mode.
    metrics_token: str = ""

    # reconnect backoff, for a failed initial open and a mid-stream drop
    reconnect_max_attempts: int = 5
    reconnect_backoff_base: float = 1.0
    reconnect_backoff_max: float = 30.0
    # Consecutive bad reads before reconnecting. RTSP decode errors cluster
    # during network jitter, so this rides out a short bad patch.
    read_failures_before_reconnect: int = 8

    max_upload_mb: int = 500
    allowed_video_extensions: tuple[str, ...] = (".mp4", ".avi", ".mov", ".mkv", ".webm")

    # Seed demo accounts. Set DEMO_MODE=false for a real deployment.
    demo_mode: bool = True

    # short-lived signed tokens for things browsers load via plain <img>/<a>
    # and can't attach a bearer header to
    evidence_token_ttl_seconds: int = 300
    stream_token_ttl_seconds: int = 3600

    # Gujarat Police camera catalogue. No host hardcoded; sync fails with a
    # clear error while empty
    camera_catalog_base_url: str = ""
    camera_catalog_timeout_seconds: float = 8.0

    # Sentinel Camera Grid. Credentials come from .env only and are never
    # logged or returned. Empty = not configured; callers fail explicitly.
    sentinel_grid_base_url: str = "https://cctv.corp8.cloud"
    sentinel_grid_email: str = ""
    sentinel_grid_password: str = ""
    sentinel_grid_rtsp_host: str = "103.250.160.189"
    sentinel_grid_rtsp_port: int = 8554
    # a real login sometimes timed out at 8s and then worked at 20s on retry,
    # network jitter to the grid
    sentinel_grid_timeout_seconds: float = 20.0

    # Grid supervisor: keeps every registered grid camera connected. It never
    # changes ai_* flags. The stagger below, not this cap, protects the grid
    # from connection bursts.
    sentinel_grid_autoconnect: bool = True
    sentinel_grid_max_autoconnect: int = 100
    sentinel_grid_supervisor_sweep_seconds: float = 30.0
    # After a real AUTH_ERROR the supervisor backs off this long. It's one
    # shared login, so retrying per camera just hammers the same endpoint.
    sentinel_grid_auth_cooldown_seconds: float = 300.0
    # Delay between worker starts in one sweep; simultaneous RTSP handshakes
    # are less reliable.
    sentinel_grid_stagger_seconds: float = 3.0

    # Max concurrent test-connection probes. Each can hold a thread for up to
    # source_open_timeout_seconds; beyond the cap the API returns 429.
    camera_test_connection_max_concurrent: int = 3

    # Size of asyncio's default executor, shared by camera reads, inference,
    # hashing and probes. Python's default is too small for many cameras:
    # starved reads look like dead streams. 0 = one thread per camera plus
    # headroom, capped; a positive value pins it.
    worker_thread_pool_size: int = 0
    # added to the camera count so API, DB and inference still get threads
    worker_thread_pool_headroom: int = 24
    worker_thread_pool_max: int = 160

    # Shared runtime state (runtime_state.py): alert cooldowns, self-heal dedup,
    # login rate limiting. Empty = per-process memory, fine for one process.
    # A Redis URL shares them across processes. Unreachable at startup falls
    # back to in-process (logged, never fatal).
    redis_url: str = ""
    # startup ping budget. short because it blocks startup, and a slow Redis
    # is as good as none here
    redis_connect_timeout_seconds: float = 2.0

    # Refuse camera sources that resolve to loopback, private, link-local,
    # reserved or multicast addresses (egress_policy.py). Off by default because
    # camera LANs use private ranges; enable where the backend must never
    # reach internal infrastructure.
    camera_source_block_private_networks: bool = False

    # the official sandbox needs TCP ("UDP fails across NAT/firewalls")
    rtsp_force_tcp: bool = True
    # RTSP transport for grid cameras. UDP delivers far more throughput than TCP
    # over the long path to the grid; use "tcp" where UDP is blocked.
    sentinel_grid_rtsp_transport: str = "udp"
    # UDP socket buffer per stream (ffmpeg buffer_size)
    rtsp_udp_buffer_bytes: int = 4_194_304

    # Enforced at the asyncio level too, because not every OpenCV/FFmpeg build
    # honours CAP_PROP_OPEN_TIMEOUT_MSEC. Grid streams can take over 10 s to
    # handshake through the relay.
    source_open_timeout_seconds: float = 20.0

    # event clips: bounded ring buffer and bounded post-event wait, never a
    # full recording
    clip_pre_event_seconds: float = 5.0
    clip_post_event_seconds: float = 10.0
    clip_fps: float = 10.0  # nominal playback rate; source frames may be variable-interval

    # Manual recordings (REC button, recorder.py), stored as hashed evidence.
    # Length and concurrency are capped; each one is an ffmpeg encode.
    recording_max_seconds: float = 1800.0
    recording_max_concurrent: int = 4
    recording_fps: float = 10.0

    # Days before evidence may be purged (POST /api/governance/purge-expired).
    # The right period depends on jurisdiction and policy, so an admin sets it
    # (docs/PRIVACY_GOVERNANCE.md). None = keep forever.
    evidence_retention_days: int | None = None
    # Raw detections older than this may be purged by an Administrator. None =
    # never. Detections referenced by an alert, plate or evidence are kept.
    detection_retention_days: int | None = None

    model_config = SettingsConfigDict(env_file=".env")

    @model_validator(mode="after")
    def _derive_model_version(self) -> "Settings":
        if not self.model_version:
            self.model_version = f"{Path(self.model_name).stem}-coco-1.0"
        return self

    @model_validator(mode="after")
    def _resolve_ai_rate_for_hardware(self) -> "Settings":
        # CPU inference is saturated at every 3rd frame; with CUDA every frame is
        # analysed (see docs/AI_ACCURACY.md).
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
        # A real deployment (DEMO_MODE=false) must not start with the bundled or
        # a placeholder JWT secret. Fails at import.
        insecure = self.jwt_secret in _INSECURE_JWT_SECRETS or len(self.jwt_secret) < _MIN_PRODUCTION_JWT_SECRET_LENGTH
        if self.demo_mode:
            # The bundled secret is public; use a random per-process secret for
            # demos (a restart logs everyone out).
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

# Pin OpenCV to one thread; torch's OpenMP setup already ends up there.
cv2.setNumThreads(1)
