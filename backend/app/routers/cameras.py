import os
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from sqlalchemy.orm import Session

from .. import models, schemas
from ..db import get_db
from ..security import get_current_user, require_roles
from ..config import settings
from ..audit import log_action
from ..pipeline.worker import start_worker, stop_worker
from ..pipeline.source import CameraSource

router = APIRouter(prefix="/api/cameras", tags=["cameras"])


@router.get("", response_model=list[schemas.CameraOut])
def list_cameras(db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    return db.query(models.Camera).order_by(models.Camera.created_at.desc()).all()


@router.post("/upload-video")
async def upload_video(file: UploadFile = File(...), user: models.User = Depends(require_roles("Administrator", "Control Room Operator"))):
    """Accepts a local video file to use as a simulated camera feed."""
    dest = settings.uploads_dir / file.filename
    with open(dest, "wb") as f:
        f.write(await file.read())
    return {"path": str(dest), "filename": file.filename}


@router.post("/test-connection")
def test_connection(source_type: str = Form(...), source_uri: str = Form(...)):
    if source_type == "rtsp":
        return {"ok": False, "detail": "RTSP is not supported in this build — no real CCTV source available. Use webcam or an uploaded video file."}
    src = CameraSource(source_type, source_uri)
    try:
        ok = src.open()
        detail = "Source opened and produced a frame." if ok else "Source could not be opened."
        if ok:
            read_ok, _ = src.read()
            ok = ok and read_ok
        return {"ok": ok, "detail": detail}
    finally:
        src.release()


@router.post("", response_model=schemas.CameraOut)
async def create_camera(
    payload: schemas.CameraCreate,
    db: Session = Depends(get_db),
    user: models.User = Depends(require_roles("Administrator", "Control Room Operator")),
):
    # async def so this runs on the event loop (not a worker thread) — start_worker
    # calls asyncio.create_task, which needs a running loop in this thread.
    if db.query(models.Camera).filter(models.Camera.camera_code == payload.camera_code).first():
        raise HTTPException(status_code=400, detail="camera_code already exists")
    camera = models.Camera(**payload.model_dump())
    db.add(camera)
    db.commit()
    db.refresh(camera)
    log_action(db, user, "create_camera", resource=camera.camera_code)
    start_worker(camera.id)
    return camera


@router.get("/{camera_id}", response_model=schemas.CameraOut)
def get_camera(camera_id: str, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    camera = db.query(models.Camera).filter(models.Camera.id == camera_id).first()
    if not camera:
        raise HTTPException(status_code=404, detail="Camera not found")
    return camera


@router.get("/{camera_id}/health")
def camera_health(camera_id: str, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    camera = db.query(models.Camera).filter(models.Camera.id == camera_id).first()
    if not camera:
        raise HTTPException(status_code=404, detail="Camera not found")
    return {
        "status": camera.status, "fps": camera.fps, "resolution": camera.resolution,
        "latency_ms": camera.latency_ms, "error_count": camera.error_count,
        "last_frame_at": camera.last_frame_at,
    }


@router.post("/{camera_id}/restart")
async def restart_camera(camera_id: str, db: Session = Depends(get_db), user: models.User = Depends(require_roles("Administrator", "Control Room Operator"))):
    camera = db.query(models.Camera).filter(models.Camera.id == camera_id).first()
    if not camera:
        raise HTTPException(status_code=404, detail="Camera not found")
    stop_worker(camera_id)
    start_worker(camera_id)
    log_action(db, user, "restart_camera", resource=camera.camera_code)
    return {"ok": True}


@router.delete("/{camera_id}")
def delete_camera(camera_id: str, db: Session = Depends(get_db), user: models.User = Depends(require_roles("Administrator"))):
    camera = db.query(models.Camera).filter(models.Camera.id == camera_id).first()
    if not camera:
        raise HTTPException(status_code=404, detail="Camera not found")
    stop_worker(camera_id)
    db.delete(camera)
    db.commit()
    log_action(db, user, "delete_camera", resource=camera_id)
    return {"ok": True}
