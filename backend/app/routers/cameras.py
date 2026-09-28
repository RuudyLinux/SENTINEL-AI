import asyncio
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, Query
from sqlalchemy.orm import Session

from .. import models, schemas
from ..db import get_db
from ..security import get_current_user, require_roles
from ..config import settings
from ..audit import log_action
from ..pipeline import worker
from ..pipeline.worker import start_worker, stop_worker, RUNNING, CAMERA_STATS
from ..pipeline.source import CameraSource
from ..pipeline.egress_policy import blocked_reason
from ..pipeline.catalog import fetch_catalog, upsert_from_catalog, CatalogError
from ..pipeline.sentinel_grid import fetch_grid_cameras, upsert_grid_cameras, SentinelGridError
from ..pipeline import supervisor, ai_capacity, recorder
from ..self_heal import engine as self_heal
from .. import geo

router = APIRouter(prefix="/api/cameras", tags=["cameras"])


# Every table with a foreign key to cameras.id belongs in exactly one of these
# lists (enforced by test_end_to_end_hardening.py). Blockers are history that
# must outlive the camera, so deleting refuses with a 409 naming them.
CAMERA_BLOCKER_MODELS = {
    "detections": models.Detection,
    "alerts": models.Alert,
    "incidents": models.Incident,
    "evidence": models.Evidence,
    "plates": models.Plate,
    "tracks": models.Track,
    # zones block too, but only ACTIVE ones, counted separately in
    # delete_camera. retired zones are in CAMERA_CASCADE_MODELS
}

# Cascade rows only describe the camera (retired config, recovery telemetry)
# and are deleted with it. The audit log never references camera rows.
CAMERA_CASCADE_MODELS = (models.Zone, models.SelfHealEvent)


@router.get("", response_model=list[schemas.CameraOut])
def list_cameras(
    include_retired: bool = False,
    db: Session = Depends(get_db), user: models.User = Depends(get_current_user),
):
    """Active cameras. include_retired=true adds retired ones, for screens that
    show history (an alert still names the camera it came from)."""
    query = db.query(models.Camera)
    if not include_retired:
        query = query.filter(models.Camera.retired == False)  # noqa: E712
    cameras = query.order_by(models.Camera.created_at.desc()).all()
    # Live state lives in memory (CAMERA_STATS), attached here so the list
    # needs no per-row diagnostics call. None if the worker never ran.
    for camera in cameras:
        stats = CAMERA_STATS.get(camera.id, {})
        camera.grid_state = stats.get("grid_state")  # type: ignore[attr-defined]
        camera.reconnect_count = stats.get("reconnects")  # type: ignore[attr-defined]
        camera.last_error = stats.get("last_error")  # type: ignore[attr-defined]
        camera.ai_blocked = bool(stats.get("ai_blocked"))  # type: ignore[attr-defined]
        camera.recording = recorder.is_recording(camera.id)  # type: ignore[attr-defined]
    return cameras


@router.get("/nearby", response_model=list[schemas.NearbyCameraOut])
def nearby_cameras(
    lat: float = Query(..., ge=-90, le=90),
    lng: float = Query(..., ge=-180, le=180),
    radius_m: float = Query(1000, gt=0, le=50_000),
    limit: int = Query(20, ge=1, le=200),
    exclude_id: str | None = None,
    db: Session = Depends(get_db), user: models.User = Depends(get_current_user),
):
    """Active cameras within radius_m metres, nearest first. PostGIS on
    PostgreSQL, haversine elsewhere (app/geo.py). Cameras at 0,0 (unknown
    position) are never returned."""
    if lat == 0 and lng == 0:
        raise HTTPException(status_code=400, detail="0,0 is the stored form of an unknown position, not a place")
    return [
        schemas.NearbyCameraOut(
            id=c.id, camera_code=c.camera_code, name=c.name, location=c.location or "",
            lat=c.lat, lng=c.lng, status=c.status, distance_m=round(d, 1),
        )
        for c, d in geo.nearby_cameras(db, lat, lng, radius_m, limit, exclude_id)
    ]


@router.post("/catalog/sync")
async def sync_camera_catalog(
    db: Session = Depends(get_db),
    user: models.User = Depends(require_roles("Administrator", "Control Room Operator")),
):
    """Register cameras from the Gujarat Police catalogue
    (GET {CAMERA_CATALOG_BASE_URL}/api/ingest). Register only, never starts
    AI; use POST /{camera_id}/start per camera."""
    try:
        raw_records = await fetch_catalog()
        summary = upsert_from_catalog(db, raw_records)
    except CatalogError as exc:
        log_action(db, user, "sync_camera_catalog", result="FAILURE")
        # "not configured" isn't the host being down, keep them apart so
        # Problems/Health don't mix them up
        not_configured = "not configured" in str(exc)
        await self_heal.record_event(
            component="camera_catalog", error_type="MISSING_CONFIG" if not_configured else "CONNECTION_ERROR",
            severity="warning" if not_configured else "critical", message=str(exc),
            recovery_action="NONE" if not_configured else "NONE", attempt=1, max_attempts=1,
            status="CONFIG_REQUIRED" if not_configured else "FAILED", endpoint="/api/cameras/catalog/sync",
        )
        raise HTTPException(status_code=502, detail=str(exc))
    log_action(db, user, "sync_camera_catalog", resource=str(summary["total_in_catalogue"]))
    return summary


@router.post("/sentinel-grid/sync")
async def sync_sentinel_grid(
    db: Session = Depends(get_db),
    user: models.User = Depends(require_roles("Administrator", "Control Room Operator")),
):
    """Log into the Sentinel Camera Grid (cookie login, see
    pipeline/sentinel_grid.py) with SENTINEL_GRID_EMAIL/PASSWORD, fetch
    /cameras.json and register what it finds as source_type="sentinel_grid".
    Register only, same as the catalogue sync."""
    try:
        raw_records = await fetch_grid_cameras()
        summary = upsert_grid_cameras(db, raw_records)
    except SentinelGridError as exc:
        log_action(db, user, "sync_sentinel_grid", result="FAILURE")
        raise HTTPException(status_code=502, detail=str(exc))
    log_action(db, user, "sync_sentinel_grid", resource=str(summary["total_in_grid"]))
    return summary


UPLOAD_CHUNK_BYTES = 1024 * 1024  # stream to disk, never hold the whole file

# Content check in addition to the extension allow-list. A deny-list: odd but
# valid encodings must still pass, and uploads are only ever read by
# cv2.VideoCapture, never served back over HTTP.
_NON_VIDEO_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"MZ", "a Windows executable"),
    (b"\x7fELF", "an ELF executable"),
    (b"PK\x03\x04", "a ZIP archive (or Office document)"),
    (b"Rar!", "a RAR archive"),
    (b"\x1f\x8b", "a gzip archive"),
    (b"7z\xbc\xaf\x27\x1c", "a 7-Zip archive"),
    (b"%PDF", "a PDF document"),
    (b"#!", "a script"),
    (b"\xca\xfe\xba\xbe", "a Java class file"),
    (b"\x89PNG", "a PNG image"),
    (b"\xff\xd8\xff", "a JPEG image"),
    (b"GIF8", "a GIF image"),
    (b"BM", "a bitmap image"),
    (b"<!DOCTYPE", "an HTML document"),
    (b"<html", "an HTML document"),
    (b"<?xml", "an XML document"),
)


def _rejected_content_reason(head: bytes) -> "str | None":
    for signature, description in _NON_VIDEO_SIGNATURES:
        if head.startswith(signature):
            return description
    return None


@router.post("/upload-video")
async def upload_video(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: models.User = Depends(require_roles("Administrator", "Control Room Operator")),
):
    """Upload a local video to use as a simulated camera feed.

    The client filename is never used on disk (UUID name, no traversal or
    overwrite). Extension is allow-listed, size is capped while streaming in
    chunks, and a partial file is removed on rejection.
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
                if written == 0:
                    # check the first chunk, so an exe is refused after 1MB not 500MB
                    reason = _rejected_content_reason(chunk[:16])
                    if reason is not None:
                        raise HTTPException(
                            status_code=400,
                            detail=f"Uploaded file is {reason}, not a video, despite its '{ext}' name.",
                        )
                written += len(chunk)
                if written > max_bytes:
                    raise HTTPException(status_code=413, detail=f"File exceeds {settings.max_upload_mb}MB limit")
                f.write(chunk)
        if written == 0:
            # Reject empty files; the handler below removes them.
            raise HTTPException(status_code=400, detail="Uploaded file is empty (0 bytes).")
    except HTTPException:
        dest.unlink(missing_ok=True)
        raise
    except Exception:
        dest.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail="Upload failed")

    log_action(db, user, "upload_video", resource=safe_name)
    return {"path": str(dest), "filename": safe_name, "original_filename": original_name}


# Caps concurrent source probes per process. Created lazily so it binds to the
# running event loop.
_probe_semaphore: "asyncio.Semaphore | None" = None


def _get_probe_semaphore() -> asyncio.Semaphore:
    global _probe_semaphore
    if _probe_semaphore is None:
        _probe_semaphore = asyncio.Semaphore(settings.camera_test_connection_max_concurrent)
    return _probe_semaphore


@router.post("/test-connection")
async def test_connection(
    source_type: str = Form(...),
    source_uri: str = Form(...),
    user: models.User = Depends(require_roles("Administrator", "Control Room Operator")),
):
    """Probe a camera source before registering it.

    Requires the same role as creating a camera: an open probe endpoint would be
    an SSRF oracle and could tie up worker threads.
    """
    # refuse internal targets before spending a probe slot. Off by default,
    # see pipeline/egress_policy.py (doesn't stop DNS rebinding)
    refusal = blocked_reason(source_type, source_uri)
    if refusal is not None:
        raise HTTPException(status_code=400, detail=refusal)

    # Fail fast when the probe budget is spent rather than queueing behind
    # probes that may each wait out the full open timeout.
    semaphore = _get_probe_semaphore()
    if semaphore.locked():
        raise HTTPException(
            status_code=429,
            detail=(
                f"Too many camera probes in flight (limit {settings.camera_test_connection_max_concurrent}). "
                "Each probe can hold a worker thread for up to "
                f"{settings.source_open_timeout_seconds:.0f}s, and that thread pool is shared with live "
                "camera processing. Retry shortly."
            ),
        )

    async with semaphore:
        src = CameraSource(source_type, source_uri)
        try:
            # our own timeout, CAP_PROP_OPEN_TIMEOUT_MSEC isn't honored by every
            # OpenCV/FFmpeg build (saw ~30s against a dead RTSP host with 5s set)
            try:
                ok = await asyncio.wait_for(asyncio.to_thread(src.open), timeout=settings.source_open_timeout_seconds)
            except asyncio.TimeoutError:
                return {"ok": False, "detail": f"Source did not respond within {settings.source_open_timeout_seconds:.0f}s"}
            except NotImplementedError as exc:
                # e.g. the ONVIF stub, expected
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
    # async so it runs on the event loop; start_worker needs create_task
    if db.query(models.Camera).filter(models.Camera.camera_code == payload.camera_code).first():
        raise HTTPException(status_code=400, detail="camera_code already exists")
    # same egress check as test-connection, otherwise you'd just skip the probe
    refusal = blocked_reason(payload.source_type, payload.source_uri)
    if refusal is not None:
        raise HTTPException(status_code=400, detail=refusal)
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
    """Edit name, location, group, coordinates and analytics toggles. Source
    changes need a reconnect, not an edit. A running worker picks up toggle
    changes within a few seconds."""
    camera = db.query(models.Camera).filter(models.Camera.id == camera_id).first()
    if not camera:
        raise HTTPException(status_code=404, detail="Camera not found")
    updates = payload.model_dump(exclude_unset=True)
    turning_ai_on = (updates.get("ai_person") or updates.get("ai_vehicle")) and not (camera.ai_person or camera.ai_vehicle)
    running = camera_id in RUNNING and not RUNNING[camera_id].done()
    if turning_ai_on and running:
        refusal = ai_capacity.blocked(camera_id)
        if refusal is not None:
            raise HTTPException(status_code=409, detail=refusal)
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
    # same attachment as list_cameras; without it the detail page showed
    # DISCONNECTED while the camera was processing
    stats = CAMERA_STATS.get(camera.id, {})
    camera.grid_state = stats.get("grid_state")  # type: ignore[attr-defined]
    camera.reconnect_count = stats.get("reconnects")  # type: ignore[attr-defined]
    camera.last_error = stats.get("last_error")  # type: ignore[attr-defined]
    camera.ai_blocked = bool(stats.get("ai_blocked"))  # type: ignore[attr-defined]
    camera.recording = recorder.is_recording(camera.id)  # type: ignore[attr-defined]
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
    """Per-camera timing, drop and reconnect counts, and worker liveness.
    In memory; reset on restart."""
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
    """Process CPU/RAM and torch/cv2 threads, to see contention between cameras."""
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
        # Unexpected lifecycle transitions: still applied, counted here so gaps
        # in the state table are visible. Should stay empty.
        "illegal_state_transitions": {
            f"{frm}->{to}": count for (frm, to), count in worker.ILLEGAL_TRANSITIONS.items()
        },
        "ai_device": "cuda" if torch.cuda.is_available() else "cpu",
        "gpu_memory_allocated_mb": (torch.cuda.memory_allocated() // 2**20) if torch.cuda.is_available() else None,
        "ai_cameras": sorted(ai_capacity.holders()),
        "max_ai_cameras": settings.max_ai_cameras,
        "ai_waiting": ai_capacity.waiting(),
        "ai_rotation_seconds": settings.ai_rotation_seconds,
    }


@router.post("/{camera_id}/restart")
async def restart_camera(camera_id: str, db: Session = Depends(get_db), user: models.User = Depends(require_roles("Administrator", "Control Room Operator"))):
    """Restart through the supervisor (like the bulk restart) so a grid camera
    stays in the reconnect sweep."""
    camera = _active_camera_or_error(db, camera_id)
    await supervisor.restart(camera_id, str(camera.source_type))
    log_action(db, user, "restart_camera", resource=camera.camera_code)
    return {"ok": True}


@router.post("/{camera_id}/start")
async def start_camera(camera_id: str, db: Session = Depends(get_db), user: models.User = Depends(require_roles("Administrator", "Control Room Operator"))):
    """Connect a registered camera. AI flags are unchanged (PATCH to change
    them). Grid cameras are also marked auto-managed for reconnection."""
    camera = _active_camera_or_error(db, camera_id)
    if camera.source_type == "sentinel_grid":
        supervisor.connect(camera_id)
    else:
        start_worker(camera_id)
    log_action(db, user, "start_camera", resource=camera.camera_code)
    return {"ok": True}


@router.post("/{camera_id}/stop")
def stop_camera(camera_id: str, db: Session = Depends(get_db), user: models.User = Depends(require_roles("Administrator", "Control Room Operator"))):
    """Disconnect. A grid camera leaves the supervisor's auto-managed set first
    so the next sweep doesn't reconnect it."""
    camera = db.query(models.Camera).filter(models.Camera.id == camera_id).first()
    if not camera:
        raise HTTPException(status_code=404, detail="Camera not found")
    if camera.source_type == "sentinel_grid":
        supervisor.disconnect(camera_id)
    else:
        stop_worker(camera_id)
    camera.status = "offline"
    db.commit()
    log_action(db, user, "stop_camera", resource=camera.camera_code)
    return {"ok": True}


@router.get("/{camera_id}/recording")
def recording_status(camera_id: str, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    camera = db.query(models.Camera).filter(models.Camera.id == camera_id).first()
    if not camera:
        raise HTTPException(status_code=404, detail="Camera not found")
    return recorder.status(camera_id) or {"camera_id": camera_id, "recording": False}


@router.post("/{camera_id}/recording/start")
def start_recording(camera_id: str, db: Session = Depends(get_db), user: models.User = Depends(require_roles("Administrator", "Control Room Operator"))):
    """REC: record the annotated live view to MP4 until stopped (or
    recording_max_seconds). Saved as hashed Evidence when it ends."""
    camera = _active_camera_or_error(db, camera_id)
    try:
        rec = recorder.start(
            camera_id, str(camera.camera_code), lambda: worker.LATEST_FRAMES.get(camera_id),
            user_id=str(user.id), username=str(user.username),
        )
    except recorder.RecordingError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    log_action(db, user, "start_recording", resource=camera.camera_code)
    return rec.as_dict()


@router.post("/{camera_id}/recording/stop")
async def stop_recording(camera_id: str, db: Session = Depends(get_db), user: models.User = Depends(require_roles("Administrator", "Control Room Operator"))):
    """Stop REC and wait for the MP4 and its evidence row."""
    camera = db.query(models.Camera).filter(models.Camera.id == camera_id).first()
    if not camera:
        raise HTTPException(status_code=404, detail="Camera not found")
    if not recorder.is_recording(camera_id):
        raise HTTPException(status_code=409, detail="This camera is not recording.")
    rec = await asyncio.to_thread(recorder.stop, camera_id)
    log_action(db, user, "stop_recording", resource=camera.camera_code,
               result="SUCCESS" if rec and rec.evidence_id else "FAILURE")
    return rec.as_dict() if rec else {"camera_id": camera_id, "recording": False}


def _active_camera_or_error(db: Session, camera_id: str) -> models.Camera:
    camera = db.query(models.Camera).filter(models.Camera.id == camera_id).first()
    if not camera:
        raise HTTPException(status_code=404, detail="Camera not found")
    if bool(camera.retired):
        raise HTTPException(status_code=409, detail="Camera is retired; reinstate it before connecting")
    return camera


@router.post("/{camera_id}/retire")
def retire_camera(camera_id: str, db: Session = Depends(get_db), user: models.User = Depends(require_roles("Administrator"))):
    """Take a camera out of service permanently, keeping its history.

    Stops the worker, removes it from supervision and hides it from active views
    and counts; historical records keep pointing at it.
    """
    camera = db.query(models.Camera).filter(models.Camera.id == camera_id).first()
    if not camera:
        raise HTTPException(status_code=404, detail="Camera not found")
    if camera.source_type == "sentinel_grid":
        supervisor.disconnect(camera_id)
    else:
        stop_worker(camera_id)
    camera.retired = True
    camera.status = "offline"
    db.commit()
    log_action(db, user, "retire_camera", resource=camera.camera_code)
    return {"ok": True, "camera_code": camera.camera_code, "retired": True}


@router.post("/{camera_id}/reinstate")
def reinstate_camera(camera_id: str, db: Session = Depends(get_db), user: models.User = Depends(require_roles("Administrator"))):
    """Undo a retirement. Comes back offline; connecting is a separate step."""
    camera = db.query(models.Camera).filter(models.Camera.id == camera_id).first()
    if not camera:
        raise HTTPException(status_code=404, detail="Camera not found")
    camera.retired = False
    db.commit()
    log_action(db, user, "reinstate_camera", resource=camera.camera_code)
    return {"ok": True, "camera_code": camera.camera_code, "retired": False}


@router.delete("/{camera_id}")
def delete_camera(camera_id: str, db: Session = Depends(get_db), user: models.User = Depends(require_roles("Administrator"))):
    """Delete a camera that has no operational history.

    Refused with a 409 naming the blockers while detections, alerts, incidents,
    evidence, plates, tracks or active zones reference it, since deleting would
    orphan chain-of-custody records (docs/PRIVACY_GOVERNANCE.md). Retired zones
    are configuration, not evidence, and are deleted with the camera.
    """
    camera = db.query(models.Camera).filter(models.Camera.id == camera_id).first()
    if not camera:
        raise HTTPException(status_code=404, detail="Camera not found")

    retired_zones = db.query(models.Zone).filter(
        models.Zone.camera_id == camera_id, models.Zone.active == False,  # noqa: E712
    ).all()
    retired_zone_ids = [z.id for z in retired_zones]

    # counted, not just checked, so the message says what's in the way
    blockers = {
        label: db.query(model).filter(model.camera_id == camera_id).count()
        for label, model in CAMERA_BLOCKER_MODELS.items()
    }
    blockers["zones"] = db.query(models.Zone).filter(
        models.Zone.camera_id == camera_id, models.Zone.active == True,  # noqa: E712
    ).count()
    held = {name: count for name, count in blockers.items() if count}
    if held:
        log_action(db, user, "delete_camera", resource=camera_id, result="FAILURE")
        raise HTTPException(
            status_code=409,
            detail=(
                "Camera has operational history and cannot be deleted: "
                + ", ".join(f"{count} {name}" for name, count in sorted(held.items()))
                + ". Deleting it would detach that history (including any evidence) from its "
                  "source camera. Disconnect the camera instead, or remove its records through "
                  "the audited retention workflow first."
            ),
        )

    stop_worker(camera_id)
    if retired_zone_ids:
        # AlertRule has a FK to the zone. A rule on a retired zone is already
        # inert (rules_engine only looks at active zones), so it goes too
        db.query(models.AlertRule).filter(models.AlertRule.zone_id.in_(retired_zone_ids)).delete(
            synchronize_session=False
        )
        db.query(models.Zone).filter(models.Zone.id.in_(retired_zone_ids)).delete(synchronize_session=False)
    cascaded = {}
    for model in CAMERA_CASCADE_MODELS:
        if model is models.Zone:
            continue  # done above along with its rules
        removed = db.query(model).filter(model.camera_id == camera_id).delete(synchronize_session=False)
        if removed:
            cascaded[model.__tablename__] = removed
    db.delete(camera)
    db.commit()
    self_heal.forget_camera(camera_id)
    # record what went with the camera, "camera deleted" alone hides it
    trailer = ""
    if retired_zone_ids:
        trailer += f" (+{len(retired_zone_ids)} retired zones)"
    if cascaded:
        trailer += " (+" + ", ".join(f"{n} {t}" for t, n in sorted(cascaded.items())) + ")"
    log_action(db, user, "delete_camera", resource=f"{camera_id}{trailer}")
    return {"ok": True}
