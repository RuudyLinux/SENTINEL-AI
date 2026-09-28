import asyncio
import logging
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

logger = logging.getLogger("sentinel.main")

from . import log_redaction  # noqa: E402

# before any request: uvicorn logs full URLs and some carry a token
log_redaction.install()

# installed at startup, shut down at exit. module level because to_thread
# uses the loop's default executor, not anything on `app`
_executor: "ThreadPoolExecutor | None" = None

from .db import Base, engine, SessionLocal, ensure_columns, ensure_indexes
from . import models, background
from .seed import run_seed
from .ws import manager
from .pipeline.worker import start_worker, stop_worker, RUNNING
from .pipeline import supervisor, recorder
from .security import get_user_from_token, resource_token_expiry
from .config import settings

from .routers import (
    auth, cameras, streams, detections, vehicles, persons, search,
    alerts, watchlists, zones, rules, incidents, evidence, users, audit,
    analytics, system, self_heal, camera_control, metrics, review, governance,
)
from .self_heal import engine as self_heal_engine


@asynccontextmanager
async def lifespan(app: FastAPI):
    await _on_startup()
    yield
    await _on_shutdown()


app = FastAPI(title="SENTINEL VISION API", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.cors_allowed_origins.split(",") if o.strip()],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

for r in (auth, cameras, streams, detections, vehicles, persons, search,
          alerts, watchlists, zones, rules, incidents, evidence, users, audit,
          analytics, system, self_heal, camera_control, metrics, review, governance):
    app.include_router(r.router)


def _cuda_available() -> bool:
    try:
        import torch
        return bool(torch.cuda.is_available())
    except Exception:
        return False


def _size_thread_pool(camera_count: int) -> int:
    """Threads the shared executor needs for this many cameras.

    Each camera parks a thread on a blocking source.read(), in the same pool
    as DB commits, inference and probes. Python's default min(32, cpu+4)
    doesn't know about cameras, so past that they queue on each other and
    flicker online/degraded with no error: a read waiting for a thread looks
    just like one waiting for the camera.
    """
    if settings.worker_thread_pool_size > 0:
        return settings.worker_thread_pool_size
    # Two per camera: each loop can hold one waiting for a frame or the GPU
    # while its DB commit needs another. At one per camera, a camera that had
    # already written couldn't get a thread to commit while every other
    # writer sat in SQLite's 30s busy wait on its lock ("database is locked"
    # everywhere with 30 cameras).
    return max(32, min(settings.worker_thread_pool_max, 2 * camera_count + settings.worker_thread_pool_headroom))


def _ignore_client_reset(loop: asyncio.AbstractEventLoop, context: dict) -> None:
    """Windows' Proactor loop logs a full traceback (ConnectionResetError in
    _call_connection_lost) every time a browser leaves an MJPEG stream.
    Nothing failed. Swallow that one, pass everything else on."""
    exc = context.get("exception")
    if isinstance(exc, ConnectionResetError) and "_call_connection_lost" in str(context.get("handle", "")):
        return
    loop.default_exception_handler(context)


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
    asyncio.get_running_loop().set_exception_handler(_ignore_client_reset)
    logger.info(
        "shared thread pool: %d workers for %d registered camera(s)", size, camera_count,
    )


def _mark_all_cameras_offline(db) -> int:
    """No worker survives a restart, so every camera starts offline until its
    worker reports in. Otherwise a camera online when the process was killed
    stayed "online" with nothing behind it ("4/34 online" with 2 workers)."""
    reset = (
        db.query(models.Camera)
        .filter(models.Camera.status != "offline")
        .update({models.Camera.status: "offline"}, synchronize_session=False)
    )
    db.commit()
    return reset


def _resume_local_workers(db) -> list[str]:
    """Restart workers for local cameras (webcam, video_file, mock_vms).
    rtsp/onvif and grid cameras need an operator start (POST /{id}/start).
    Retired cameras never resume."""
    started = []
    for camera in db.query(models.Camera).filter(models.Camera.retired == False).all():  # noqa: E712
        if camera.source_type in ("webcam", "video_file", "mock_vms"):
            start_worker(camera.id)
            started.append(str(camera.id))
    return started


async def _on_startup():
    Base.metadata.create_all(bind=engine)
    # create_all never alters existing tables, so columns added later get
    # added here (db.ensure_columns, additive only)
    ensure_columns(
        "cameras",
        {
            "external_catalog_id": "VARCHAR", "catalog_codec": "VARCHAR",
            "catalog_live_status": "VARCHAR", "catalog_synced_at": "DATETIME",
            "catalog_stale": "BOOLEAN", "whep_url": "VARCHAR", "hls_url": "VARCHAR",
        },
        backfill_defaults={"catalog_codec": "''", "catalog_live_status": "''", "catalog_stale": "0"},
        # whep_url/hls_url stay NULL, they're optional
    )
    ensure_columns("detections", {"source_timestamp": "DATETIME"})
    ensure_columns("plates", {"source_timestamp": "DATETIME"})
    ensure_columns("alerts", {"source_timestamp": "DATETIME"})
    ensure_columns(
        "evidence",
        {"alert_id": "VARCHAR", "detection_id": "VARCHAR", "event_type": "VARCHAR", "source_timestamp": "DATETIME"},
        backfill_defaults={"event_type": "''"},
    )
    # camera groups, person appearance signatures, loitering
    ensure_columns("cameras", {"camera_group": "VARCHAR"}, backfill_defaults={"camera_group": "''"})
    ensure_columns("detections", {"appearance_signature": "JSON"})
    ensure_columns("zones", {"loitering_seconds": "FLOAT"})
    # V2 plate pipeline fields on plates and tracks. Backfills have to be true
    # of the old row: a pre-V2 plate was one single-frame read so
    # reads_count=1, but track_id/plate_bbox/last_seen were never captured and
    # stay NULL.
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
    # risk score. old alerts get 0 and no factors, nothing was assessed then
    ensure_columns(
        "alerts", {"risk_score": "INTEGER", "risk_factors": "JSON"},
        backfill_defaults={"risk_score": "0"},
    )
    # ANPR review. old reads were already treated as usable, so auto_accepted
    ensure_columns(
        "plates",
        {
            "review_status": "VARCHAR", "reviewed_by": "VARCHAR",
            "reviewed_at": "DATETIME", "corrected_text": "VARCHAR",
        },
        backfill_defaults={"review_status": "'auto_accepted'"},
    )
    # alert feedback. NULL for old alerts, nobody reviewed them
    ensure_columns(
        "alerts",
        {"feedback": "VARCHAR", "feedback_reason": "VARCHAR", "feedback_by": "VARCHAR", "feedback_at": "DATETIME"},
    )
    # model/rule version at capture. NULL on old rows, never recorded
    ensure_columns("evidence", {"model_version": "VARCHAR", "rule_version": "VARCHAR"})
    # Audit chain. Old rows stay NULL and verify_chain() reports them as
    # unchained; a retroactive hash would vouch for writes it never saw.
    ensure_columns("audit_logs", {"chain_seq": "INTEGER", "prev_hash": "VARCHAR", "entry_hash": "VARCHAR"})
    # ANPR explainability, NULL on old rows. corroborated especially isn't
    # backfilled to true: those rows came from a pipeline that persisted on a
    # single read. NULL = unknown, which is accurate.
    ensure_columns(
        "plates",
        {
            "ocr_variant": "VARCHAR", "variants_agreeing": "INTEGER",
            "corroborated": "BOOLEAN", "plate_crop_path": "VARCHAR",
        },
    )
    # Not backfilled to true either, old rows escalated on confidence alone.
    # NULL = not corroborated, capping watchlist alerts at HIGH until a fresh
    # corroborated sighting comes in.
    ensure_columns("vehicles", {"plate_corroborated": "BOOLEAN"})
    # every existing camera is active
    ensure_columns("cameras", {"retired": "BOOLEAN NOT NULL DEFAULT 0"})
    ensure_indexes("plates", ["review_status"])
    ensure_indexes("alerts", ["feedback"])
    ensure_indexes("audit_logs", ["chain_seq"])
    # hot-path indexes, additive, fine to run every startup
    ensure_indexes("detections", ["timestamp", "camera_id", "track_id"])
    ensure_indexes("alerts", ["severity", "status", "camera_id"])
    ensure_indexes("incidents", ["status"])
    # plate search and route reconstruction both scan plates
    ensure_indexes("plates", ["plate_text_normalized", "vehicle_id", "camera_id", "timestamp", "track_id"])
    ensure_indexes("tracks", ["camera_id", "yolo_track_id", "vehicle_id"])
    ensure_indexes("vehicles", ["plate_text", "last_seen"])
    ensure_indexes("alerts", ["risk_score", "vehicle_id"])
    ensure_indexes("incident_alerts", ["incident_id", "alert_id"])
    # List sort keys and the per-alert correlation lookup; measured in
    # alembic/versions/20260928_0600_list_and_lookup_indexes.py.
    ensure_indexes("detections", [("camera_id", "timestamp")])
    ensure_indexes("alerts", ["timestamp"])
    ensure_indexes("audit_logs", ["timestamp"])
    ensure_indexes("evidence", ["incident_id", "created_at"])
    ensure_indexes("incidents", ["vehicle_id", "created_at"])
    db = SessionLocal()
    try:
        run_seed(db)
        reset = _mark_all_cameras_offline(db)
        if reset:
            logger.info("startup: marked %d camera(s) offline until their workers report in", reset)
        _resume_local_workers(db)
    finally:
        db.close()

    # size the pool before the grid supervisor starts its workers
    # (see _size_thread_pool)
    await _install_thread_pool()

    # load the shared YOLO model once before cameras need it (CUDA init is
    # slow, and every camera's first frame would wait on it)
    if _cuda_available():
        try:
            from .pipeline import detector
            await asyncio.to_thread(detector.warmup)
        except Exception:
            logger.exception("startup: model warmup failed, cameras will load it on first use")

    # Grid auto-connect: discover and register the catalogue (logged and
    # skipped if the grid is down or unconfigured), then the supervisor keeps
    # eligible cameras connected and reconnects drops. It never enables AI.
    await supervisor.discover_and_register()
    supervisor.start_supervisor()

    # rebuild open problems from recorded events so they survive a restart
    self_heal_engine.rebuild_open_problems()


async def _on_shutdown():
    # stops the sweep loop and the grid cameras it manages
    await supervisor.stop_supervisor()
    # Local cameras were started directly at startup and aren't in
    # AUTO_MANAGED, so stop everything left in RUNNING too (snapshot, since
    # stop_worker pops). Otherwise their tasks and VideoCaptures leaked at exit.
    #
    # Each stop is guarded on its own so one bad cleanup doesn't stop the rest
    # or blow up shutdown. And the tasks are awaited: cancel() only requests
    # it, and the release in their finally only runs once they're scheduled
    # again, which isn't guaranteed before uvicorn tears the loop down.
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
    # Workers are gone, now let their background work finish. A clip task
    # waits up to clip_post_event_seconds before writing Evidence, and one
    # killed with the loop loses evidence for a real alert (app/background.py).
    await background.drain(settings.shutdown_drain_seconds)
    # stop_worker already asked recordings to finish; wait so the files and
    # their evidence rows get written
    await asyncio.to_thread(recorder.stop_all, settings.shutdown_drain_seconds)
    # the ws batcher has a timer task and unsent events, stop it with a final flush
    await manager.shutdown()
    # Last, the above may still use it. Threads stuck on a socket read won't
    # notice, so don't wait; the workers' own release already ran.
    global _executor
    if _executor is not None:
        _executor.shutdown(wait=False, cancel_futures=True)
        _executor = None


@app.get("/api/health")
def health():
    return {"ok": True, "service": "sentinel-vision-backend"}


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket, token: str | None = None):
    # Live detection/alert feed, so it needs auth. Browsers can't set an
    # Authorization header on a WebSocket handshake, so the token comes as a
    # query param (like the evidence/stream tokens) and is checked before accept.
    db = SessionLocal()
    try:
        user = get_user_from_token(token, db)
    finally:
        db.close()
    if user is None:
        await ws.close(code=4401)
        return
    # Checked once at the handshake, and a socket lives as long as the tab, so
    # an expired 8h token kept getting events forever. Close when the token
    # expires (same as MJPEG); the client reconnects with its current token.
    deadline = resource_token_expiry(token)
    await manager.connect(ws)
    try:
        while True:
            remaining = (deadline - datetime.utcnow()).total_seconds() if deadline else 0
            if remaining <= 0:
                await ws.close(code=4401)
                break
            try:
                # dashboard never sends anything, this just notices a disconnect
                await asyncio.wait_for(ws.receive_text(), timeout=remaining)
            except asyncio.TimeoutError:
                continue
    except WebSocketDisconnect:
        pass
    finally:
        # any exit, or a dead socket stays in manager.active and every
        # broadcast keeps trying it
        manager.disconnect(ws)
