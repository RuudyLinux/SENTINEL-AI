import asyncio
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from sqlalchemy.orm import Session

from .. import models, schemas
from ..db import get_db
from ..security import get_current_user, require_roles
from ..config import settings
from ..audit import log_action
from ..pipeline.worker import start_worker, stop_worker, RUNNING, CAMERA_STATS
from ..pipeline.source import CameraSource
from ..pipeline.catalog import fetch_catalog, upsert_from_catalog, CatalogError
from ..pipeline.sentinel_grid import fetch_grid_cameras, upsert_grid_cameras, SentinelGridError

router = APIRouter(prefix="/api/cameras", tags=["cameras"])


@router.get("", response_model=list[schemas.CameraOut])
def list_cameras(db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    return db.query(models.Camera).order_by(models.Camera.created_at.desc()).all()


@router.post("/catalog/sync")
async def sync_camera_catalog(
    db: Session = Depends(get_db),
    user: models.User = Depends(require_roles("Administrator", "Control Room Operator")),
):
    """Registers cameras from the official Gujarat Police camera catalogue
    (GET {CAMERA_CATALOG_BASE_URL}/api/ingest) into the Camera Registry.

    REGISTER only — this never starts AI processing on any camera. Start
    processing explicitly per-camera via POST /{camera_id}/start (or select
    + bulk-start from the Cameras screen) once a camera is registered.
    """
    try:
        raw_records = await fetch_catalog()
        summary = upsert_from_catalog(db, raw_records)
    except CatalogError as exc:
        log_action(db, user, "sync_camera_catalog", result="FAILURE")
        raise HTTPException(status_code=502, detail=str(exc))
    log_action(db, user, "sync_camera_catalog", resource=str(summary["total_in_catalogue"]))
    return summary


@router.post("/sentinel-grid/sync")
async def sync_sentinel_grid(
    db: Session = Depends(get_db),
    user: models.User = Depends(require_roles("Administrator", "Control Room Operator")),
):
    """Real Sentinel Camera Grid discovery (final integration task): logs into
    https://cctv.corp8.cloud (session-cookie web login, not a bare public JSON
    endpoint — see pipeline/sentinel_grid.py) with SENTINEL_GRID_EMAIL/PASSWORD
    from .env, fetches /cameras.json, and REGISTERS the discovered cameras
    (source_type="sentinel_grid", grouped "Sentinel Grid"). Register only — never
    starts AI processing; use POST /{camera_id}/start per camera afterward, same
    contract as the official-catalogue sync above.
    """
    try:
        raw_records = await fetch_grid_cameras()
        summary = upsert_grid_cameras(db, raw_records)
    except SentinelGridError as exc:
        log_action(db, user, "sync_sentinel_grid", result="FAILURE")
        raise HTTPException(status_code=502, detail=str(exc))
    log_action(db, user, "sync_sentinel_grid", resource=str(summary["total_in_grid"]))
    return summary


UPLOAD_CHUNK_BYTES = 1024 * 1024  # 1MB — stream to disk, never buffer the whole file in RAM


@router.post("/upload-video")
async def upload_video(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: models.User = Depends(require_roles("Administrator", "Control Room Operator")),
):
    """Accepts a local video file to use as a simulated camera feed.

    Hardened (P0-F): never trusts the client-provided filename for the path
    on disk (generated UUID name instead — avoids path traversal / overwrite),
    validates extension against an allow-list, enforces a size cap while
    streaming in chunks (never reads the whole upload into memory), and
    deletes any partial file if the cap is exceeded.
    """
    original_name = file.filename or "upload"
    ext = Path(original_name).suffix.lower()
    if ext not in settings.allowed_video_extensions:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type '{ext or '(none)'}'. Allowed: {', '.join(settings.allowed_video_extensions)}",
        )

    safe_name = f"{uuid.uuid4().hex}{ext}"
    dest = settings.uploads_dir / safe_name
    max_bytes = settings.max_upload_mb * 1024 * 1024
    written = 0
    try:
        with open(dest, "wb") as f:
            while True:
                chunk = await file.read(UPLOAD_CHUNK_BYTES)
                if not chunk:
                    break
                written += len(chunk)
                if written > max_bytes:
                    raise HTTPException(status_code=413, detail=f"File exceeds {settings.max_upload_mb}MB limit")
                f.write(chunk)
    except HTTPException:
        dest.unlink(missing_ok=True)
        raise
    except Exception:
        dest.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail="Upload failed")

    log_action(db, user, "upload_video", resource=safe_name)
    return {"path": str(dest), "filename": safe_name, "original_filename": original_name}


@router.post("/test-connection")
async def test_connection(source_type: str = Form(...), source_uri: str = Form(...)):
    src = CameraSource(source_type, source_uri)
    try:
        # Enforced independently of CAP_PROP_OPEN_TIMEOUT_MSEC, which isn't
        # reliably honored by every OpenCV/FFmpeg build (Phase 4 finding —
        # measured ~30s instead of a configured 5s against an unreachable
        # RTSP endpoint) — this endpoint must still respond in bounded time.
        try:
            ok = await asyncio.wait_for(asyncio.to_thread(src.open), timeout=settings.source_open_timeout_seconds)
        except asyncio.TimeoutError:
            return {"ok": False, "detail": f"Source did not respond within {settings.source_open_timeout_seconds:.0f}s"}
        except NotImplementedError as exc:
            # e.g. the ONVIF interface stub — an honest, expected failure, not a crash.
            return {"ok": False, "detail": str(exc)}
        detail = "Source opened and produced a frame." if ok else "Source could not be opened."
        if ok:
            read_ok, _ = await asyncio.to_thread(src.read)
            ok = ok and read_ok
            if not read_ok:
                detail = "Source opened but produced no frame."
        return {"ok": ok, "detail": detail}
    finally:
        await asyncio.to_thread(src.release)


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


@router.patch("/{camera_id}", response_model=schemas.CameraOut)
def update_camera(
    camera_id: str,
    payload: schemas.CameraUpdate,
    db: Session = Depends(get_db),
    user: models.User = Depends(require_roles("Administrator", "Control Room Operator")),
):
    """In-place edit of name/location/camera_group/lat/lng/analytics toggles. Does not
    touch source_type/source_uri (a reconnect operation, not an edit) or restart
    the camera worker — analytics toggles take effect on the next processed frame
    (worker.py._process_frame reads camera.ai_* live)."""
    camera = db.query(models.Camera).filter(models.Camera.id == camera_id).first()
    if not camera:
        raise HTTPException(status_code=404, detail="Camera not found")
    updates = payload.model_dump(exclude_unset=True)
    for field, value in updates.items():
        setattr(camera, field, value)
    db.commit()
    db.refresh(camera)
    log_action(db, user, "update_camera", resource=camera.camera_code)
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


@router.get("/{camera_id}/diagnostics")
def camera_diagnostics(camera_id: str, db: Session = Depends(get_db), user: models.User = Depends(require_roles("Administrator", "Control Room Operator"))):
    """Phase 4 — temporary diagnostic surface for the multi-camera
    concurrency investigation: per-camera loop/inference timing, drop and
    reconnect counts, and whether the worker task is actually alive (vs.
    silently dead with an unretrieved exception). Not a general metrics
    system — in-memory, process-local, reset on restart."""
    camera = db.query(models.Camera).filter(models.Camera.id == camera_id).first()
    if not camera:
        raise HTTPException(status_code=404, detail="Camera not found")

    task = RUNNING.get(camera_id)
    task_state = "not_started"
    task_error = None
    if task is not None:
        if not task.done():
            task_state = "running"
        elif task.cancelled():
            task_state = "cancelled"
        else:
            exc = task.exception()
            if exc is not None:
                task_state = "died_with_exception"
                task_error = f"{type(exc).__name__}: {exc}"
            else:
                task_state = "finished"

    return {
        "camera_id": camera_id,
        "db_status": camera.status,
        "db_error_count": camera.error_count,
        "worker_task_state": task_state,
        "worker_task_error": task_error,
        **CAMERA_STATS.get(camera_id, {}),
    }


@router.get("/diagnostics/system")
def system_diagnostics(user: models.User = Depends(require_roles("Administrator", "Control Room Operator"))):
    """Process-wide CPU/RAM + torch/cv2 thread configuration — the other
    half of the Phase 4 concurrency investigation (per-camera numbers alone
    don't show contention between cameras)."""
    import psutil
    import torch
    import cv2 as _cv2

    proc = psutil.Process()
    return {
        "process_cpu_percent": proc.cpu_percent(interval=0.2),
        "process_rss_mb": proc.memory_info().rss / (1024 * 1024),
        "system_cpu_percent": psutil.cpu_percent(interval=0.2),
        "system_cpu_count": psutil.cpu_count(),
        "torch_num_threads": torch.get_num_threads(),
        "cv2_num_threads": _cv2.getNumThreads(),
        "cameras_running": sum(1 for t in RUNNING.values() if not t.done()),
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


@router.post("/{camera_id}/start")
async def start_camera(camera_id: str, db: Session = Depends(get_db), user: models.User = Depends(require_roles("Administrator", "Control Room Operator"))):
    """CONNECT to a registered camera and begin AI processing — the
    deliberate second step after catalogue sync (which only REGISTERS)."""
    camera = db.query(models.Camera).filter(models.Camera.id == camera_id).first()
    if not camera:
        raise HTTPException(status_code=404, detail="Camera not found")
    start_worker(camera_id)
    log_action(db, user, "start_camera", resource=camera.camera_code)
    return {"ok": True}


@router.post("/{camera_id}/stop")
def stop_camera(camera_id: str, db: Session = Depends(get_db), user: models.User = Depends(require_roles("Administrator", "Control Room Operator"))):
    camera = db.query(models.Camera).filter(models.Camera.id == camera_id).first()
    if not camera:
        raise HTTPException(status_code=404, detail="Camera not found")
    stop_worker(camera_id)
    camera.status = "offline"
    db.commit()
    log_action(db, user, "stop_camera", resource=camera.camera_code)
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
