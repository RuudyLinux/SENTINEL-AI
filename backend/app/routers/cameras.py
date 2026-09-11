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
from ..pipeline import supervisor
from ..self_heal import engine as self_heal

router = APIRouter(prefix="/api/cameras", tags=["cameras"])


@router.get("", response_model=list[schemas.CameraOut])
def list_cameras(db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    cameras = db.query(models.Camera).order_by(models.Camera.created_at.desc()).all()
    # Richer connection-lifecycle state (LIVE/CONNECTING/PROCESSING/DEGRADED/
    # RECONNECTING/DISCONNECTED/AUTH_ERROR/ERROR) lives in-memory in
    # CAMERA_STATS, not the DB — attached here (transient attribute, not
    # persisted) so the Camera Grid can show it without a per-camera
    # diagnostics round-trip for every row on every poll. None for a camera
    # whose worker has never run this process, never fabricated.
    for camera in cameras:
        stats = CAMERA_STATS.get(camera.id, {})
        camera.grid_state = stats.get("grid_state")  # type: ignore[attr-defined]
        # Same stats dict, same reasoning — surfaces real reconnect/error
        # diagnostics (Camera Grid UI) without a per-camera round-trip.
        camera.reconnect_count = stats.get("reconnects")  # type: ignore[attr-defined]
        camera.last_error = stats.get("last_error")  # type: ignore[attr-defined]
    return cameras


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
        # "not configured" is a distinct condition from a real network/host
        # failure (Self-Heal Part B): never retried automatically either
        # way, but surfaced with the right status so Problems/Health don't
        # conflate "nobody has set CAMERA_CATALOG_BASE_URL yet" with "the
        # configured host is actually down".
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

# C2 (final deep-debug pass): content validation to go with the extension
# allow-list, which by itself accepted an arbitrary blob renamed `.mp4`
# (measured: a PE executable `MZ\x90\x00` and a ZIP `PK\x03\x04` were both
# stored happily).
#
# Deliberately a DENY-list of things that are definitively not video, not an
# allow-list of known containers. An allow-list would reject legitimate but
# unusual encodings the deployment might genuinely use, breaking working
# functionality to defend against a payload that — verified — is never served
# back over HTTP by anything (uploads_dir has no StaticFiles mount and no
# FileResponse; it is only ever handed to cv2.VideoCapture). So: refuse what
# is unambiguously an executable/archive/document/image, pass everything
# else through, including unrecognized-but-plausible video.
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
    """Returns why `head` is definitively not video, or None to allow it."""
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
                if written == 0:
                    # C2: checked on the FIRST chunk, before any more of the
                    # upload is accepted — an executable or archive is
                    # rejected after 1MB, not after 500MB.
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
            # BUG-B fix (final deep-debug pass): a 0-byte upload was accepted
            # with a 200 and left a 0-byte file on disk forever. It is not a
            # video by any definition — registering it as a camera source
            # produces a camera that can never open its stream, failing
            # through the full reconnect/backoff budget before going offline,
            # for a file that was never openable in the first place. Rejected
            # at the door instead, and the empty file cleaned up by the
            # HTTPException handler below (same path as the size-cap
            # rejection). Measured: previously `size=0` orphan retained.
            raise HTTPException(status_code=400, detail="Uploaded file is empty (0 bytes).")
    except HTTPException:
        dest.unlink(missing_ok=True)
        raise
    except Exception:
        dest.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail="Upload failed")

    log_action(db, user, "upload_video", resource=safe_name)
    return {"path": str(dest), "filename": safe_name, "original_filename": original_name}


# C1 (final deep-debug pass): bounds how many source probes may be in flight
# at once. Module-level so the cap is per-process, matching the resource it
# protects — the single shared `asyncio.to_thread` executor, which every
# camera worker also depends on. Created lazily on first use so it binds to
# the running loop rather than import time.
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
    """Probe a candidate camera source before registering it.

    Security fix: this was the ONLY camera route with no authorization at all,
    while every other one requires Administrator/Control Room Operator. Because
    it opens an operator-supplied URI, leaving it open made the backend an
    unauthenticated SSRF probe — anyone who could reach it could ask the server
    to connect to any internal host/port and learn from the ok/detail response
    whether something was listening there. It also tied up a worker thread for
    up to source_open_timeout_seconds per anonymous request.

    It is still only a probe against an operator-supplied source, which is
    inherent to onboarding a camera; requiring the same role as camera creation
    puts it behind the same trust boundary as the action it precedes.
    """
    # C1: fail fast instead of queueing when the probe budget is already
    # spent. A queued probe would still occupy a request AND still wait out
    # the full open timeout behind the ones ahead of it, so refusing is the
    # honest answer. (`locked()` is checked before acquiring rather than
    # using a zero timeout: two requests can both observe an unlocked
    # semaphore and one then waits briefly for the other — bounded by a
    # single probe, and it never exceeds the concurrency the semaphore
    # itself enforces, which is the resource being protected.)
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
    the camera worker — analytics toggles take effect within about a second on an
    already-running camera (worker.py._camera_loop refreshes ai_person/ai_vehicle/
    ai_anpr from the DB on a throttle, since expire_on_commit=False means the
    loop's long-lived `camera` object otherwise never sees this PATCH's commit)."""
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
    # Real bug found via the final freeze browser smoke test: the single-
    # camera detail page (GET /{id}) never got this attachment — only the
    # list endpoint did — so it always read grid_state as missing and
    # synthesized DISCONNECTED (deriveConnectionState's fallback for "no
    # grid_state") even while genuinely PROCESSING with real video/detections
    # flowing. Same attachment as list_cameras, single row.
    stats = CAMERA_STATS.get(camera.id, {})
    camera.grid_state = stats.get("grid_state")  # type: ignore[attr-defined]
    camera.reconnect_count = stats.get("reconnects")  # type: ignore[attr-defined]
    camera.last_error = stats.get("last_error")  # type: ignore[attr-defined]
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
    """Audit finding (PR #1 review): a real Sentinel Grid camera restarted
    via raw stop_worker/start_worker bypassed supervisor.py's AUTO_MANAGED/
    OPERATOR_DISCONNECTED bookkeeping entirely, so it silently dropped out
    of the 24/7 auto-reconnect sweep. Fixed by routing through
    supervisor.restart — the single shared implementation also used by the
    bulk Camera Control Center restart (routers/camera_control.py), so the
    two can never drift again."""
    camera = db.query(models.Camera).filter(models.Camera.id == camera_id).first()
    if not camera:
        raise HTTPException(status_code=404, detail="Camera not found")
    await supervisor.restart(camera_id, str(camera.source_type))
    log_action(db, user, "restart_camera", resource=camera.camera_code)
    return {"ok": True}


@router.post("/{camera_id}/start")
async def start_camera(camera_id: str, db: Session = Depends(get_db), user: models.User = Depends(require_roles("Administrator", "Control Room Operator"))):
    """CONNECT to a registered camera — the deliberate second step after
    catalogue sync (which only REGISTERS). This does NOT enable AI
    processing; that stays whatever `ai_person`/`ai_vehicle`/`ai_anpr` the
    camera already has (see `PATCH /{camera_id}` to change those) — connected
    and AI-processing are independent, by design. For a real Sentinel Grid
    camera this also marks it auto-managed, so the 24/7 supervisor
    (pipeline/supervisor.py) reconnects it automatically if it later drops."""
    camera = db.query(models.Camera).filter(models.Camera.id == camera_id).first()
    if not camera:
        raise HTTPException(status_code=404, detail="Camera not found")
    if camera.source_type == "sentinel_grid":
        supervisor.connect(camera_id)
    else:
        start_worker(camera_id)
    log_action(db, user, "start_camera", resource=camera.camera_code)
    return {"ok": True}


@router.post("/{camera_id}/stop")
def stop_camera(camera_id: str, db: Session = Depends(get_db), user: models.User = Depends(require_roles("Administrator", "Control Room Operator"))):
    """DISCONNECT — stops the worker (and, for a real Sentinel Grid camera,
    removes it from the 24/7 supervisor's auto-managed set first, so it is
    not immediately reconnected on the next sweep)."""
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


@router.delete("/{camera_id}")
def delete_camera(camera_id: str, db: Session = Depends(get_db), user: models.User = Depends(require_roles("Administrator"))):
    """BUG-C fix (final deep-debug pass, 2026-09-11): this used to delete the
    camera row unconditionally and return 200, silently orphaning every
    record that referenced it. Measured on a single probe camera: 1 detection,
    1 alert, 1 incident, 1 EVIDENCE row (with its capture-time SHA-256), 1
    plate sighting and 1 zone were all left pointing at a camera_id that no
    longer existed — including evidence attached to a still-open incident.

    Two things were wrong at once:

    - **Evidence/incident history was silently detached.** For a platform
      whose entire evidence story is chain-of-custody, quietly orphaning an
      open incident's evidence via an unrelated endpoint is the wrong
      outcome; `docs/PRIVACY_GOVERNANCE.md` already states that evidence and
      audit history are never silently destroyed. Deletion is now REFUSED
      (409) while dependent records exist, naming exactly what blocks it, so
      an operator makes that call deliberately (close/export the incident,
      or purge evidence through the audited governance workflow) instead of
      it happening as a side effect.
    - **SQLite and PostgreSQL disagree.** SQLite does not enforce foreign
      keys unless `PRAGMA foreign_keys=ON`, which this codebase does not set
      (see db.py for why it is not simply flipped on), while the
      Alembic-managed PostgreSQL schema always has. So this request quietly
      succeeded in dev/demo and would have raised a ForeignKeyViolation →
      500 in production: a divergence no test running on SQLite could catch.
      The guard below closes that gap from the application side, giving BOTH
      backends the same, explainable 409 instead of one silently corrupting
      and the other 500-ing.
    """
    camera = db.query(models.Camera).filter(models.Camera.id == camera_id).first()
    if not camera:
        raise HTTPException(status_code=404, detail="Camera not found")

    # Counted rather than just existence-checked: the message has to tell the
    # operator what is actually in the way, not merely that something is.
    blockers = {
        "detections": db.query(models.Detection).filter(models.Detection.camera_id == camera_id).count(),
        "alerts": db.query(models.Alert).filter(models.Alert.camera_id == camera_id).count(),
        "incidents": db.query(models.Incident).filter(models.Incident.camera_id == camera_id).count(),
        "evidence": db.query(models.Evidence).filter(models.Evidence.camera_id == camera_id).count(),
        "plates": db.query(models.Plate).filter(models.Plate.camera_id == camera_id).count(),
        "zones": db.query(models.Zone).filter(models.Zone.camera_id == camera_id).count(),
        "tracks": db.query(models.Track).filter(models.Track.camera_id == camera_id).count(),
    }
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
    db.delete(camera)
    db.commit()
    log_action(db, user, "delete_camera", resource=camera_id)
    return {"ok": True}
