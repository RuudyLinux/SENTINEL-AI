import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

logger = logging.getLogger("sentinel.main")

#: Installed at startup, shut down at exit. Held here rather than on `app`
#: because `asyncio.to_thread` reaches the loop's default executor, not the
#: application object.
_executor: "ThreadPoolExecutor | None" = None

from .db import Base, engine, SessionLocal, ensure_columns, ensure_indexes
from . import models, background
from .seed import run_seed
from .ws import manager
from .pipeline.worker import start_worker, stop_worker, RUNNING
from .pipeline import supervisor
from .security import get_user_from_token
from .config import settings

from .routers import (
    auth, cameras, streams, detections, vehicles, persons, search,
    alerts, watchlists, zones, rules, incidents, evidence, users, audit,
    analytics, system, self_heal, camera_control, metrics, review, governance,
)
from .self_heal import engine as self_heal_engine


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup and shutdown in one place (FastAPI's on_event startup/shutdown
    # hooks are deprecated in favor of this) — same two phases as before,
    # just expressed as the code before/after the single `yield` rather than
    # two separate decorated functions.
    await _on_startup()
    yield
    await _on_shutdown()


app = FastAPI(title="SENTINEL VISION API", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    # Configurable (CORS_ALLOWED_ORIGINS, comma-separated) rather than
    # hardcoded — default preserves the exact local-demo origin unchanged.
    allow_origins=[o.strip() for o in settings.cors_allowed_origins.split(",") if o.strip()],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

for r in (auth, cameras, streams, detections, vehicles, persons, search,
          alerts, watchlists, zones, rules, incidents, evidence, users, audit,
          analytics, system, self_heal, camera_control, metrics, review, governance):
    app.include_router(r.router)


def _size_thread_pool(camera_count: int) -> int:
    """How many threads the shared executor needs for this many cameras.

    Every camera worker parks one thread on a blocking `source.read()` for as
    long as the stream takes to deliver a frame, and that is the same pool
    that serves DB commits, inference offloads and connection probes. Python's
    default (`min(32, cpu_count + 4)`) does not know how many cameras exist,
    so past that number of cameras the workers simply queue against each
    other -- which presents as cameras flickering between `online` and
    `degraded` with no error, since a read waiting for a thread is
    indistinguishable from a read waiting for a camera.
    """
    if settings.worker_thread_pool_size > 0:
        return settings.worker_thread_pool_size
    return max(32, min(settings.worker_thread_pool_max, camera_count + settings.worker_thread_pool_headroom))


async def _install_thread_pool() -> None:
    global _executor
    db = SessionLocal()
    try:
        camera_count = db.query(models.Camera).count()
    finally:
        db.close()
    size = _size_thread_pool(camera_count)
    _executor = ThreadPoolExecutor(max_workers=size, thread_name_prefix="sentinel-worker")
    asyncio.get_running_loop().set_default_executor(_executor)
    logger.info(
        "shared thread pool: %d workers for %d registered camera(s)", size, camera_count,
    )


async def _on_startup():
    Base.metadata.create_all(bind=engine)
    # Additive-only migration for columns added after a DB already existed
    # (create_all never alters existing tables) — see db.ensure_columns.
    ensure_columns(
        "cameras",
        {
            "external_catalog_id": "VARCHAR", "catalog_codec": "VARCHAR",
            "catalog_live_status": "VARCHAR", "catalog_synced_at": "DATETIME",
            "catalog_stale": "BOOLEAN", "whep_url": "VARCHAR", "hls_url": "VARCHAR",
        },
        backfill_defaults={"catalog_codec": "''", "catalog_live_status": "''", "catalog_stale": "0"},
        # whep_url/hls_url deliberately NOT backfilled — genuinely optional,
        # existing cameras correctly migrate to NULL (never fabricated).
    )
    ensure_columns("detections", {"source_timestamp": "DATETIME"})
    ensure_columns("plates", {"source_timestamp": "DATETIME"})
    ensure_columns("alerts", {"source_timestamp": "DATETIME"})
    ensure_columns(
        "evidence",
        {"alert_id": "VARCHAR", "detection_id": "VARCHAR", "event_type": "VARCHAR", "source_timestamp": "DATETIME"},
        backfill_defaults={"event_type": "''"},
    )
    # Camera groups (Model 2/4), person appearance-similarity signatures (Phase 5),
    # loitering rule support (Phase 6) — see README.md → "Capability breakdown".
    ensure_columns("cameras", {"camera_group": "VARCHAR"}, backfill_defaults={"camera_group": "''"})
    ensure_columns("detections", {"appearance_signature": "JSON"})
    ensure_columns("zones", {"loitering_seconds": "FLOAT"})
    # V2 plate pipeline: per-track sighting fields on `plates`, and the
    # bookkeeping columns on `tracks` (a table declared since the first schema
    # but never written until V2 — see models.Track). Backfills are chosen so an
    # existing row migrates to a TRUE statement about itself: a pre-V2 Plate row
    # was one single-frame read, so reads_count=1 is correct, while track_id /
    # plate_bbox / last_seen stay NULL because that information genuinely was
    # never captured and must not be invented.
    ensure_columns(
        "plates",
        {
            "track_id": "VARCHAR", "last_seen": "DATETIME", "reads_count": "INTEGER",
            "vehicle_class": "VARCHAR", "detection_confidence": "FLOAT",
            "vehicle_bbox": "JSON", "plate_bbox": "JSON",
        },
        backfill_defaults={"reads_count": "1", "vehicle_class": "''", "detection_confidence": "0.0"},
    )
    ensure_columns(
        "tracks",
        {"detection_count": "INTEGER", "plate_reads": "INTEGER"},
        backfill_defaults={"detection_count": "0", "plate_reads": "0"},
    )
    # Explainable risk score on alerts. Existing alerts keep risk_score=0 and an
    # empty factor list — correct, because no assessment was ever made for them;
    # a retroactively computed score would be a claim about a decision that was
    # not taken at the time.
    ensure_columns(
        "alerts", {"risk_score": "INTEGER", "risk_factors": "JSON"},
        backfill_defaults={"risk_score": "0"},
    )
    # Human-in-the-loop ANPR review (10/10 roadmap P7). Existing rows keep
    # review_status=auto_accepted — correct for a read written before review
    # existed, since it was already treated as usable without one.
    ensure_columns(
        "plates",
        {
            "review_status": "VARCHAR", "reviewed_by": "VARCHAR",
            "reviewed_at": "DATETIME", "corrected_text": "VARCHAR",
        },
        backfill_defaults={"review_status": "'auto_accepted'"},
    )
    # Alert feedback / false-positive measurement (10/10 roadmap P6). Left
    # NULL for existing alerts — no review ever happened for them, and
    # defaulting to "confirmed" would fabricate one.
    ensure_columns(
        "alerts",
        {"feedback": "VARCHAR", "feedback_reason": "VARCHAR", "feedback_by": "VARCHAR", "feedback_at": "DATETIME"},
    )
    # Evidence provenance completion (10/10 roadmap P8) — model/rule version
    # active AT CAPTURE. NULL for pre-existing rows: genuinely not recorded.
    ensure_columns("evidence", {"model_version": "VARCHAR", "rule_version": "VARCHAR"})
    # Tamper-evident audit chain (10/10 roadmap P9). Existing rows keep
    # chain_seq/prev_hash/entry_hash NULL — they predate the chain and
    # verify_chain() reports them honestly as unchained rather than pretending
    # a retroactive hash covers writes it never actually witnessed.
    ensure_columns("audit_logs", {"chain_seq": "INTEGER", "prev_hash": "VARCHAR", "entry_hash": "VARCHAR"})
    # ANPR explainability: which preprocessing variant produced the read, how
    # many variants agreed, whether the temporal layer corroborated it, and the
    # plate crop OCR actually looked at. All left NULL for existing rows —
    # genuinely unrecorded. `corroborated` in particular is NOT backfilled to
    # true: a pre-existing row was written under a pipeline that persisted on a
    # single read, so claiming it was corroborated would assert evidence that
    # was never gathered. NULL reads as "unknown", which is the truth.
    ensure_columns(
        "plates",
        {
            "ocr_variant": "VARCHAR", "variants_agreeing": "INTEGER",
            "corroborated": "BOOLEAN", "plate_crop_path": "VARCHAR",
        },
    )
    # Corroboration on the vehicle (A1 precision hardening). NOT backfilled to
    # true: existing rows were written by a pipeline that escalated watchlist
    # alerts on confidence alone, so claiming they were corroborated would
    # assert evidence that was never gathered. NULL reads as "not corroborated",
    # which caps their watchlist alerts at HIGH until a fresh corroborated
    # sighting arrives — the safe direction for a missing safety signal.
    ensure_columns("vehicles", {"plate_corroborated": "BOOLEAN"})
    ensure_indexes("plates", ["review_status"])
    ensure_indexes("alerts", ["feedback"])
    ensure_indexes("audit_logs", ["chain_seq"])
    # Hot-path query indexes — additive, safe to run every startup.
    ensure_indexes("detections", ["timestamp", "camera_id", "track_id"])
    ensure_indexes("alerts", ["severity", "status", "camera_id"])
    ensure_indexes("incidents", ["status"])
    # Historical plate search and route reconstruction both scan `plates` —
    # these are what keep an investigation query fast as the table grows.
    ensure_indexes("plates", ["plate_text_normalized", "vehicle_id", "camera_id", "timestamp", "track_id"])
    ensure_indexes("tracks", ["camera_id", "yolo_track_id", "vehicle_id"])
    ensure_indexes("vehicles", ["plate_text", "last_seen"])
    ensure_indexes("alerts", ["risk_score", "vehicle_id"])
    ensure_indexes("incident_alerts", ["incident_id", "alert_id"])
    db = SessionLocal()
    try:
        run_seed(db)
        # Resume detection workers for any cameras registered from a previous run.
        # rtsp/onvif are deliberately excluded — those require an explicit operator
        # start (see routers/cameras.py POST /{id}/start), same as catalog-synced
        # cameras. mock_vms behaves like webcam/video_file: purely local, safe to
        # auto-resume.
        for camera in db.query(models.Camera).all():
            if camera.source_type in ("webcam", "video_file", "mock_vms"):
                start_worker(camera.id)
    finally:
        db.close()

    # Size the shared thread pool BEFORE any camera worker is started: every
    # one of them parks a thread on a blocking read, and the default pool does
    # not scale with the number of cameras. See _size_thread_pool.
    await _install_thread_pool()

    # Real Sentinel Camera Grid 24/7 auto-connect: discover the real catalogue
    # (register-only, safe if the grid is unreachable/unconfigured — logged,
    # never fatal to startup) and start the connection supervisor, which
    # connects eligible real cameras up to a resource-safety cap and
    # reconnects any that drop. Never enables AI (see supervisor.py header).
    await supervisor.discover_and_register()
    supervisor.start_supervisor()

    # Self-Heal: reload the open-problem index from real recorded events so
    # GET /api/self-heal/problems reflects true state across a restart, not
    # just this process's in-memory history since boot.
    self_heal_engine.rebuild_open_problems()


async def _on_shutdown():
    # Stops the supervisor's sweep loop and every camera worker IT manages
    # (supervisor.AUTO_MANAGED — sentinel_grid cameras only).
    await supervisor.stop_supervisor()
    # Bug fix: _on_startup also starts workers directly for every webcam/
    # video_file/mock_vms camera (start_worker() above), completely
    # bypassing the supervisor — those tasks are never added to
    # AUTO_MANAGED, so stop_supervisor() alone never touches them. Without
    # this, they were abandoned at process exit instead of going through
    # _camera_loop's `finally: source.release()`, leaking the asyncio task
    # and cv2.VideoCapture handle. Stopping every remaining key in RUNNING
    # (a dict, so this snapshot avoids mutating it while iterating — stop_worker
    # pops from RUNNING) covers ALL camera workers, supervisor-managed or not.
    #
    # Each stop is independently guarded: RUNNING is process-global, so one
    # camera's cleanup raising (e.g. a task tied to an event loop that's
    # already been closed by something else) must never abort cleanup of
    # the rest, nor propagate out of shutdown and fail whatever caller is
    # waiting on it — same defensive stance worker.py already takes
    # everywhere else around per-camera cleanup.
    # Audit finding: stop_worker() only requests cancellation via
    # task.cancel() — the task's own `finally: source.release()` only runs
    # once it's next scheduled, which isn't guaranteed before uvicorn tears
    # down the event loop unless this actually awaits it. Collected here
    # (rather than trusting return_exceptions elsewhere) so a shutdown
    # deterministically waits for every camera's real cleanup, not just the
    # cancellation request.
    pending_tasks = []
    for camera_id in list(RUNNING.keys()):
        try:
            task = stop_worker(camera_id)
            if task is not None:
                pending_tasks.append(task)
        except Exception:
            logger.exception("shutdown: stop_worker failed for camera %s, continuing", camera_id)
    if pending_tasks:
        await asyncio.gather(*pending_tasks, return_exceptions=True)
    # Camera workers are stopped, so nothing new is being spawned — now let the
    # fire-and-forget work they started finish. An event-clip task waits up to
    # clip_post_event_seconds before writing its Evidence row, and an untracked
    # one was simply destroyed when the loop closed, silently losing evidence
    # for a real alert (see app/background.py).
    await background.drain(settings.shutdown_drain_seconds)
    # Same deterministic-cleanup contract as the camera workers above: the
    # live-event batcher holds a timer task and a buffer of events not yet
    # sent, so it is stopped (and given a final flush) here rather than being
    # abandoned when the loop is torn down.
    await manager.shutdown()
    # Last, because everything above may still hand work to it. Threads parked
    # on a blocking socket read do not notice a shutdown request, so this does
    # not wait for them -- the loop is going away regardless, and the camera
    # workers' own `finally: source.release()` has already run above.
    global _executor
    if _executor is not None:
        _executor.shutdown(wait=False, cancel_futures=True)
        _executor = None


@app.get("/api/health")
def health():
    return {"ok": True, "service": "sentinel-vision-backend"}


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket, token: str | None = None):
    # Real gap found in a hardening pass: this endpoint broadcasts live
    # detection/alert events (worker.py, rules_engine.py) and previously had
    # NO authentication at all — anyone who could reach the backend got the
    # live surveillance feed without logging in. Browsers can't attach an
    # Authorization header to a WebSocket handshake, so the token travels as
    # a query parameter instead (same reasoning as the existing resource-
    # token endpoints for evidence/streams) and is validated with the same
    # JWT before the connection is ever accepted.
    db = SessionLocal()
    try:
        user = get_user_from_token(token, db)
    finally:
        db.close()
    if user is None:
        await ws.close(code=4401)
        return
    await manager.connect(ws)
    try:
        while True:
            await ws.receive_text()  # dashboard doesn't need to send anything; keep the socket alive
    except WebSocketDisconnect:
        manager.disconnect(ws)
