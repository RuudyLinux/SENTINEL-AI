import os
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from .. import models
from ..config import settings
from ..db import get_db
from ..security import get_current_user, require_roles
from ..audit import log_action
from ..pipeline.worker import RUNNING, start_worker, stop_worker
from ..pipeline.demo_scenario import trigger_scenario, DemoScenarioError
from ..seed import reset_demo_data, DEMO_CAMERAS
from ..ws import manager

router = APIRouter(prefix="/api/system", tags=["system"])


def _require_demo_mode():
    if not settings.demo_mode:
        raise HTTPException(status_code=403, detail="Not available: DEMO_MODE is off (this is a production instance).")


@router.get("/status")
def system_status(db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    """Live subsystem checks; each one actually exercises what it reports on."""
    running_workers = sum(1 for t in RUNNING.values() if not t.done())
    total_cameras = db.query(models.Camera).filter(models.Camera.retired == False).count()  # noqa: E712

    try:
        db.execute(text("SELECT 1"))
        db_ok = True
    except Exception:
        db_ok = False

    storage_ok = os.access(settings.evidence_dir, os.W_OK) and os.access(settings.uploads_dir, os.W_OK)

    return {
        "timestamp": datetime.utcnow().isoformat(),
        "subsystems": [
            {"name": "API", "status": "OPERATIONAL"},  # this response returning at all proves it
            {"name": "DATABASE", "status": "OPERATIONAL" if db_ok else "DOWN"},
            {"name": "AI MODEL / PIPELINE", "status": "OPERATIONAL" if running_workers > 0 or total_cameras == 0 else "DEGRADED"},
            {"name": "WEBSOCKET", "status": "OPERATIONAL", "connected_clients": len(manager.active)},
            {"name": "CAMERA NETWORK", "status": "OPERATIONAL" if total_cameras == 0 or running_workers > 0 else "DEGRADED"},
            {"name": "STORAGE", "status": "OPERATIONAL" if storage_ok else "DEGRADED"},
        ],
        "cameras_registered": total_cameras,
        "camera_workers_running": running_workers,
    }


@router.post("/demo/reset")
async def demo_reset(db: Session = Depends(get_db), user: models.User = Depends(require_roles("Administrator"))):
    """Back to a clean demo state. DEMO_MODE only. Stops running workers
    first (their rows are about to go), wipes transactional data and
    re-creates the demo cameras and watchlist entry (seed.reset_demo_data).

    Then starts the two demo cameras' workers, like startup does for
    video_file cameras. Without that LATEST_FRAMES was empty when
    /demo/trigger-scenario came next, and the scenario (rightly) refuses to
    fake a frame, so the demo produced no evidence at all.

    async def because start_worker calls create_task, which needs the loop;
    sync handlers run in a thread without one.
    """
    _require_demo_mode()
    for camera in db.query(models.Camera).all():
        stop_worker(camera.id)
    summary = reset_demo_data(db)
    demo_codes = [c["camera_code"] for c in DEMO_CAMERAS]
    for camera in db.query(models.Camera).filter(
        models.Camera.camera_code.in_(demo_codes), models.Camera.retired == False,  # noqa: E712
    ).all():
        start_worker(camera.id)
    log_action(db, user, "demo_reset", resource=",".join(summary["cameras"]))
    return summary


@router.post("/demo/trigger-scenario")
async def demo_trigger_scenario(db: Session = Depends(get_db), user: models.User = Depends(require_roles("Administrator", "Control Room Operator"))):
    """Fire the demo scenario (watchlist plate on C-014, then C-019) through
    the real correlation/alert path; pipeline/demo_scenario.py says exactly
    what's real. DEMO_MODE only, needs /demo/reset (or C-014 and C-019
    registered) first."""
    _require_demo_mode()
    try:
        result = await trigger_scenario(db, user)
    except DemoScenarioError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return result
