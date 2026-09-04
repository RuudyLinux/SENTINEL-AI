from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from .db import Base, engine, SessionLocal, ensure_columns, ensure_indexes
from . import models
from .seed import run_seed
from .ws import manager
from .pipeline.worker import start_worker
from .pipeline import supervisor

from .routers import (
    auth, cameras, streams, detections, vehicles, persons, search,
    alerts, watchlists, zones, rules, incidents, evidence, users, audit,
    analytics, system,
)

app = FastAPI(title="SENTINEL VISION API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

for r in (auth, cameras, streams, detections, vehicles, persons, search,
          alerts, watchlists, zones, rules, incidents, evidence, users, audit,
          analytics, system):
    app.include_router(r.router)


@app.on_event("startup")
async def on_startup():
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


@app.on_event("shutdown")
async def on_shutdown():
    # Stops the supervisor's sweep loop and every camera worker it manages
    # cleanly — no orphaned asyncio task or RTSP/cv2 resource left behind at
    # process exit.
    await supervisor.stop_supervisor()


@app.get("/api/health")
def health():
    return {"ok": True, "service": "sentinel-vision-backend"}


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await manager.connect(ws)
    try:
        while True:
            await ws.receive_text()  # dashboard doesn't need to send anything; keep the socket alive
    except WebSocketDisconnect:
        manager.disconnect(ws)
