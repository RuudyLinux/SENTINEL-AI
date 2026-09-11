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

    # Datastore. Empty means "derive a SQLite URL from db_path" — the exact
    # pre-V2 behavior, so an existing checkout and the test suite need no
    # config change. Production sets this to a PostgreSQL URL, e.g.
    # postgresql+psycopg://user:pass@host:5432/sentinel
    # Development on SQLite / production on PostgreSQL is the supported split.
    database_url: str = ""
    # Connection pool sizing — applies to BOTH backends (see db.py's
    # _engine_kwargs). Must comfortably exceed the camera concurrency cap:
    # each running camera worker holds a Session — one pooled connection —
    # for the ENTIRE lifetime of its stream, not just for one query, so a
    # pool smaller than the worker count means a worker blocks waiting for a
    # connection. Real bug this fixes: the SQLite branch previously set no
    # pool size at all, silently taking SQLAlchemy's default (size=5,
    # max_overflow=10 = 15 total) — confirmed live, bulk-starting cameras via
    # POST /api/cameras/bulk crashed multiple workers with
    # `QueuePool limit of size 5 overflow 10 reached`. 30 total is a
    # comfortable default for a single-machine deployment either way; a held
    # SQLite connection costs nothing beyond a local file handle, so there is
    # no real reason to size it any smaller than PostgreSQL's default.
    db_pool_size: int = 20
    db_max_overflow: int = 10
    # Recycle before typical server/proxy idle timeouts so a long-lived camera
    # worker's connection is replaced proactively rather than failing in use.
    # PostgreSQL only (see db.py) — meaningless for a local SQLite file, which
    # has no server-side idle-connection concept to recycle against.
    db_pool_recycle_seconds: int = 1800

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

    # Confidence-aware intelligence (V2 gap fix): a watchlist match on a plate
    # read BELOW this floor must not carry the same alert severity as one read
    # confidently. The match still fires — a real watchlist hit is never
    # silenced for being uncertain — but is capped at HIGH instead of
    # auto-CRITICAL, and the alert says so explicitly, so an operator sees
    # "watchlist match, needs confirmation" rather than treating a 48%-
    # confidence read as equivalent evidence to a 94%-confidence one. Set
    # above plate_min_confidence (the gate for recording a plate at all) and
    # below plate_stable_confidence (temporal fusion's own "trust this"
    # threshold) — matching that same read is exactly the case this floor is
    # for: it passed the gate, but fusion has not corroborated it yet.
    watchlist_high_confidence_floor: float = 0.60

    # Human-in-the-loop ANPR review (10/10 roadmap P7): a Plate sighting read
    # below this confidence is stored (already true — the quality gate above
    # is the only thing that discards a read) AND flagged `pending_review` so
    # an operator sees it in the review queue rather than the system silently
    # treating an uncertain read as settled intelligence. Same value as
    # `watchlist_high_confidence_floor` by design — both express "this is the
    # confidence level at which this platform trusts a read without a human"
    # — kept as a separate setting because the review floor governs EVERY
    # plate, not only ones that also happen to match a watchlist.
    plate_review_confidence_floor: float = 0.60

    # --- V2 plate pipeline (plate localization + per-track confidence voting) ---
    # Master switch. False restores the exact pre-V2 behavior: whole vehicle
    # crop -> OCR -> one Plate row per passing frame. Kept as a real escape
    # hatch, not decoration — the V2 path changes both OCR cost and Plate row
    # cardinality, and an operator must be able to revert that in one env var
    # without a code change.
    plate_pipeline_v2: bool = True
    # Optional dedicated license-plate detection weights. Deliberately empty by
    # default: this repo bundles only yolov8n.pt (COCO), which has no plate
    # class, and a plate model is a real asset a deployment supplies. Empty (or
    # a path that does not exist) falls back to classical CV localization —
    # see pipeline/plate_detect.py. Never fabricated, never auto-downloaded.
    plate_model_name: str = ""
    plate_detect_confidence: float = 0.25
    # Plate crops are upscaled to this glyph height before OCR. Effective glyph
    # height dominates OCR accuracy on real CCTV frames far more than the OCR
    # engine choice does.
    plate_ocr_target_height: int = 64
    # Temporal aggregation: a track's plate is considered settled once this
    # many agreeing reads clear this peak confidence. Until then every
    # inference cycle re-reads it.
    plate_min_reads_for_stability: int = 3
    plate_stable_confidence: float = 0.60
    # Once settled, re-verify at most this often instead of every cycle — the
    # main OCR cost saving, while still catching a genuine mid-track correction.
    plate_reverify_seconds: float = 10.0
    # A track not seen for this long is dropped from the in-memory accumulator.
    # ByteTrack never announces that a track id retired, so this is what bounds
    # the dict on a camera running for days.
    plate_track_ttl_seconds: float = 120.0
    # How often a still-visible vehicle's sighting row is refreshed so its
    # last_seen/dwell stays truthful. Bounds write pressure for a vehicle
    # parked in frame — without it the row would be rewritten every frame.
    plate_sighting_refresh_seconds: float = 5.0

    # --- Event correlation ---
    # A new CRITICAL alert about a vehicle that ALREADY has an open incident
    # within this window is attached to that incident rather than opening
    # another one. 15 minutes is long enough to cover a vehicle crossing several
    # cameras in one run, short enough that a genuinely separate visit hours
    # later is treated as a new event.
    incident_correlation_window_seconds: float = 900.0
    # A vehicle is reported as LIVE only if it was seen this recently. Beyond
    # it, the UI must say "last known", never imply the vehicle is on camera now.
    vehicle_live_window_seconds: float = 120.0

    # --- Live event stream (see ws.py) ---
    # Detections are coalesced into one batch frame per interval instead of one
    # frame each: N cameras x their inference rate would otherwise be N x rate
    # React state updates per second in every open dashboard. 250ms still reads
    # as instant to an operator. Alerts and incidents are never batched.
    ws_batch_interval_seconds: float = 0.25
    # Hard cap per batch. If flushing ever stalls, the buffer must not grow
    # without bound — oldest events are dropped and the batch reports the real
    # count rather than implying the stream was complete.
    ws_batch_max_events: int = 200

    # How long shutdown waits for fire-and-forget background work (event-clip
    # encodes, self-heal log writes) before cancelling it. Comfortably above
    # clip_post_event_seconds so a clip already collecting frames for a real
    # alert gets to finish and persist its Evidence row.
    shutdown_drain_seconds: float = 15.0

    # --- Metrics ---
    # Shared secret a Prometheus scraper presents as a bearer token. Empty by
    # default: with no token configured, /api/metrics is reachable only with an
    # Administrator JWT. There is deliberately no unauthenticated mode — camera
    # counts, plate-recognition rates and alert volumes are operationally
    # sensitive. Set it in .env only, never in source.
    metrics_token: str = ""

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

    # Per-endpoint concurrency cap for POST /api/cameras/test-connection
    # (final deep-debug pass, workstream C1). Each probe occupies a thread
    # from the SHARED asyncio `to_thread` executor for up to
    # `source_open_timeout_seconds` (20s) — measured: 8 sequential probes of
    # unreachable/loopback/malformed URIs each took exactly 20.0s. The
    # default executor is bounded (min(32, cpu_count+4) workers), and the
    # SAME pool runs every camera worker's frame reads, inference offloads
    # and DB commits — so an authorized-but-lower-trust operator firing a
    # burst of probes could starve unrelated live camera processing for the
    # full timeout. This caps how many probes may be in flight at once;
    # beyond it the endpoint fails fast with 429 rather than queueing (a
    # queued probe still holds a request and still ends up waiting 20s).
    camera_test_connection_max_concurrent: int = 3

    # Optional egress policy for operator-supplied camera sources (workstream
    # C3, see pipeline/egress_policy.py). When True, a source whose host
    # resolves to a loopback, link-local, private, reserved, multicast or
    # unspecified address is refused instead of opened.
    #
    # Default False on purpose, and this is NOT timidity: a great many real
    # deployments run their cameras on exactly the private ranges this blocks
    # — a police camera LAN is not the public internet — so defaulting it on
    # would break working installations to defend against a user who is
    # already authorized (the endpoint requires Administrator or Control Room
    # Operator). Turn it on for a hardened deployment where the backend must
    # never be able to reach internal infrastructure.
    camera_source_block_private_networks: bool = False

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

    # --- Privacy / governance controls (10/10 roadmap P13) ---
    # How long evidence is retained before it becomes eligible for purge via
    # POST /api/governance/purge-expired. NOT a legal-compliance claim: this
    # platform has no way to know which retention period applies to a given
    # deployment's jurisdiction, agency policy, or an active investigation —
    # an operator/administrator must set this according to the applicable
    # law/policy for their deployment. See docs/PRIVACY_GOVERNANCE.md. None
    # (the default) means "no automatic expiry" — evidence is retained
    # indefinitely until an administrator explicitly configures a period,
    # never auto-deleted based on an assumed default.
    evidence_retention_days: int | None = None

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
