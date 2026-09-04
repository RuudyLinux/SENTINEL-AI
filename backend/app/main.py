import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

logger = logging.getLogger("sentinel.main")

from .db import Base, engine, SessionLocal, ensure_columns, ensure_indexes
from . import models
from .seed import run_seed
from .ws import manager
from .pipeline.worker import start_worker, stop_worker, RUNNING
from .pipeline import supervisor
from .security import get_user_from_token
from .config import settings

from .routers import (
    auth, cameras, streams, detections, vehicles, persons, search,
    alerts, watchlists, zones, rules, incidents, evidence, users, audit,
    analytics, system, self_heal, camera_control,
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
          analytics, system, self_heal, camera_control):
    app.include_router(r.router)


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
    # Hot-path query indexes — additive, safe to run every startup.
    ensure_indexes("detections", ["timestamp", "camera_id"])
    ensure_indexes("alerts", ["severity", "status", "camera_id"])
    ensure_indexes("incidents", ["status"])
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
