from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from .db import Base, engine, SessionLocal
from . import models
from .seed import run_seed
from .ws import manager
from .pipeline.worker import start_worker

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
    db = SessionLocal()
    try:
        run_seed(db)
        # Resume detection workers for any cameras registered from a previous run.
        for camera in db.query(models.Camera).all():
            if camera.source_type in ("webcam", "video_file"):
                start_worker(camera.id)
    finally:
        db.close()


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
