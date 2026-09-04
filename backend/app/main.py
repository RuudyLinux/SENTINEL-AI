from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from .db import Base, engine, SessionLocal, ensure_columns, ensure_indexes
from . import models
from .seed import run_seed
from .ws import manager
from .pipeline.worker import start_worker
from .pipeline import supervisor
from .security import get_user_from_token
from .config import settings

from .routers import (
    auth, cameras, streams, detections, vehicles, persons, search,
    alerts, watchlists, zones, rules, incidents, evidence, users, audit,
    analytics, system,
)


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
          analytics, system):
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
    # loitering rule support (Phase 6) — see HYBRID_ARCHITECTURE.md.
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


async def _on_shutdown():
    # Stops the supervisor's sweep loop and every camera worker it manages
    # cleanly — no orphaned asyncio task or RTSP/cv2 resource left behind at
    # process exit.
    await supervisor.stop_supervisor()


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
