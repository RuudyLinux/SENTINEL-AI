"""Central settings. Dev-mode secrets via .env — documented non-goal: no Vault/KMS in this build."""
from pathlib import Path

import cv2
from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent

# Values that must never be trusted as a real production JWT secret — the
# bundled dev default plus a couple of common placeholder strings someone
# might paste in without actually generating a random one.
_INSECURE_JWT_SECRETS = {
    "sentinel-vision-dev-secret-change-in-production", "", "changeme", "secret", "password",
}
_MIN_PRODUCTION_JWT_SECRET_LENGTH = 32


class Settings(BaseSettings):
    jwt_secret: str = "sentinel-vision-dev-secret-change-in-production"
    jwt_algorithm: str = "HS256"
    access_token_minutes: int = 480

    # CORS (hardening pass): was hardcoded to localhost:3000 in main.py —
    # correct for local demo, but not configurable for an actual deploy.
    # Comma-separated so it stays a plain env var, no JSON parsing needed;
    # default preserves today's exact behavior unchanged.
    cors_allowed_origins: str = "http://localhost:3000"

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
    # Consecutive bad reads tolerated before a camera is treated as dropped
    # and a real reconnect (release + reopen) is attempted, vs. reacting to
    # one blip. Real observed finding: individual h264 decode errors
    # ("error while decoding MB ...") on a live RTSP feed are FFmpeg
    # recovering from a corrupted macroblock, not necessarily a failed
    # `read()` — but they cluster during real network jitter, so raised
    # from 3 to 8 to ride out a short bad patch (~250-300ms at 30fps)
    # without a full reconnect cycle, while still catching a genuinely
    # dead stream in under a second.
    read_failures_before_reconnect: int = 8

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

    # 24/7 auto-connect supervisor (real camera connectivity task). Separate
    # from AI: this only keeps eligible real Sentinel Grid cameras' RTSP
    # connection alive — it never enables ai_person/ai_vehicle/ai_anpr on a
    # camera. Cap is a conservative starting point, NOT yet validated against
    # this machine's actual CPU/RAM/network cost of N concurrent real RTSP
    # decodes — do not raise it, or claim any concurrency figure, before
    # running the staged 1/3/5/10/30 connection test and recording the
    # measured safe number here.
    sentinel_grid_autoconnect: bool = True
    sentinel_grid_max_autoconnect: int = 5
    sentinel_grid_supervisor_sweep_seconds: float = 30.0
    # After a real AUTH_ERROR (credentials rejected by the grid, not just
    # "unconfigured"), the supervisor stops retrying for this long — a
    # rejected credential fails identically for every camera since the grid
    # login is one shared account, so retrying per-camera would just hammer
    # the same login endpoint repeatedly for no new information.
    sentinel_grid_auth_cooldown_seconds: float = 300.0
    # Staggered startup (concurrency optimization task). Real finding: firing
    # every eligible camera's start_worker() back-to-back in one sweep opens
    # that many simultaneous new RTSP TCP handshakes against the external
    # grid at once — measured (staged 10-camera test) as meaningfully less
    # reliable than bringing cameras up one at a time. This delay is inserted
    # BETWEEN successive worker starts within one sweep (never before the
    # first, never blocking anything else) — turns a burst into a rollout.
    # 3.0s is a conservative starting point within the requested 2-5s range;
    # re-measure before lowering it for a higher concurrency target.
    sentinel_grid_stagger_seconds: float = 3.0

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

    model_config = SettingsConfigDict(env_file=".env")

    @model_validator(mode="after")
    def _enforce_production_jwt_secret(self) -> "Settings":
        # Hardening-pass finding: nothing previously stopped DEMO_MODE=false
        # (the documented "this is a production deploy" signal — see seed.py,
        # which already gates demo accounts/watchlist seeding on it) from
        # running with the bundled dev JWT secret, or a short/placeholder
        # one. Demo mode is completely unaffected — this only fires once
        # DEMO_MODE=false is set, which is exactly the signal that a real
        # deploy is intended. Fails loudly at startup (import time), not
        # silently, and never once the app is already serving requests.
        if not self.demo_mode:
            if self.jwt_secret in _INSECURE_JWT_SECRETS or len(self.jwt_secret) < _MIN_PRODUCTION_JWT_SECRET_LENGTH:
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
