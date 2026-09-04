"""Central settings. Dev-mode secrets via .env — documented non-goal: no Vault/KMS in this build."""
from pathlib import Path

import cv2
from pydantic_settings import BaseSettings

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    jwt_secret: str = "sentinel-vision-dev-secret-change-in-production"
    jwt_algorithm: str = "HS256"
    access_token_minutes: int = 480

    db_path: Path = BASE_DIR / "sentinel.db"
    uploads_dir: Path = BASE_DIR / "uploads"
    evidence_dir: Path = BASE_DIR / "evidence_store"

    # Detection pipeline
    model_name: str = "yolov8n.pt"
    model_version: str = "yolov8n-coco-1.0"
    rule_version: str = "rules-1.0"
    detect_every_n_frames: int = 3  # throttle inference for CPU
    confidence_threshold: float = 0.4

    # ANPR quality gate: a normalized OCR read only becomes a Vehicle/Plate
    # correlation record if it looks like a plate AND clears this confidence.
    plate_min_confidence: float = 0.35

    # Stream reconnect (P0-A): backoff schedule used both on initial open
    # failure and on a dropped mid-stream read.
    reconnect_max_attempts: int = 5
    reconnect_backoff_base: float = 1.0
    reconnect_backoff_max: float = 30.0

    # Upload hardening (P0-F)
    max_upload_mb: int = 500
    allowed_video_extensions: tuple[str, ...] = (".mp4", ".avi", ".mov", ".mkv", ".webm")

    # Demo accounts (P0-G): seeded only when true. Default True so the
    # documented local/judge demo flow (`admin`/`sentinel123`, ...) keeps
    # working out of the box; set DEMO_MODE=false in .env for a real deploy.
    demo_mode: bool = True

    # Resource-token TTLs (P0-E): short-lived signed tokens for endpoints
    # that browsers hit via plain <img>/<a> navigation and can't attach a
    # bearer header to.
    evidence_token_ttl_seconds: int = 300
    stream_token_ttl_seconds: int = 3600

    # Official Gujarat Police camera catalogue (Phase 3 P0). Never hardcode
    # a host — this is empty until set via .env / environment, and sync
    # fails with a clear error while it's empty rather than guessing one.
    camera_catalog_base_url: str = ""
    camera_catalog_timeout_seconds: float = 8.0

    # Real Sentinel Camera Grid integration (Final integration task). The web
    # host and RTSP/WHEP endpoint are the ones supplied for this task (not
    # secret — publicly given), so they default here for convenience; the
    # email/password are genuinely secret and default to empty — set them in
    # .env only, NEVER hardcoded, NEVER logged, NEVER returned by any API
    # response. sentinel_grid_email/password being empty means the grid
    # integration is not configured; callers must fail with a clear error,
    # not silently no-op or fabricate cameras.
    sentinel_grid_base_url: str = "https://cctv.corp8.cloud"
    sentinel_grid_email: str = ""
    sentinel_grid_password: str = ""
    sentinel_grid_rtsp_host: str = "103.250.160.189"
    sentinel_grid_rtsp_port: int = 8554
    # Submission-hardening regression check found this HTTP (login + cameras.json)
    # timeout too — same class of issue as source_open_timeout_seconds below: a
    # real login occasionally read-timed-out at 8s, then succeeded cleanly at 20s
    # on immediate retry (real network jitter to the grid, not a code bug).
    sentinel_grid_timeout_seconds: float = 20.0

    # RTSP transport (Phase 3 P0). The official sandbox requires TCP
    # ("UDP fails across NAT/firewalls") — centralized here as a safe,
    # overridable switch rather than hardcoded in the adapter.
    rtsp_force_tcp: bool = True

    # Phase 4 finding: CAP_PROP_OPEN_TIMEOUT_MSEC is set on every RTSP
    # capture (source.py) but is NOT reliably honored by every OpenCV/FFmpeg
    # build — measured on this build's bundled FFmpeg against a real (if
    # unreachable) RTSP endpoint, an unresponsive source actually hung for
    # ~30s, not the configured 5s. Enforced independently at the asyncio
    # level in worker.py instead of trusting the OpenCV property alone.
    #
    # Final integration task finding: this is a separate, additional
    # constraint from the one above — a genuinely REACHABLE, healthy real
    # Sentinel Camera Grid RTSP stream (cam04) measured 9.8s-13.8s across 4
    # real successful handshakes (real internet round-trip through the grid's
    # relay, not a failure). 8s, then 15s, both cut off real successful
    # connections, not just unresponsive ones. Raised to 20s for margin over
    # the observed variance.
    source_open_timeout_seconds: float = 20.0

    # Event video clips (Phase 3 P0). Bounded ring buffer + bounded
    # post-event wait — never unlimited, never a full stream recording.
    clip_pre_event_seconds: float = 5.0
    clip_post_event_seconds: float = 10.0
    clip_fps: float = 10.0  # nominal playback rate; source frames may be variable-interval

    class Config:
        env_file = ".env"


settings = Settings()
settings.uploads_dir.mkdir(parents=True, exist_ok=True)
settings.evidence_dir.mkdir(parents=True, exist_ok=True)

# Phase 6: a native FFmpeg/torch threading assertion crash was chased
# through the test suite ("fctx->async_lock failed at
# libavcodec/pthread_frame.c") — the actual root cause turned out to be a
# test-isolation bug (real camera workers left running by an earlier test,
# torn down abruptly at interpreter exit — fixed in tests/conftest.py's
# `client` fixture), NOT this setting; toggling cv2's thread count alone
# did not fix it. Kept anyway as a cheap, low-risk defensive measure: it
# matches what the live server already does in practice (Phase 4
# diagnostics measured cv2_num_threads=1 even before this was set
# explicitly, apparently as a side effect of torch's own OpenMP init) —
# measured on its own (Phase 4 diagnostics: cv2_num_threads reported 1 even
# before this was set explicitly) — making an accidental safe default explicit
# and guaranteed rather than relying on it.
cv2.setNumThreads(1)
