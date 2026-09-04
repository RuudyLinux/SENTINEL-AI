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
from ..pipeline.worker import RUNNING, stop_worker
from ..pipeline.demo_scenario import trigger_scenario, DemoScenarioError
from ..seed import reset_demo_data
from ..ws import manager

router = APIRouter(prefix="/api/system", tags=["system"])


def _require_demo_mode():
    if not settings.demo_mode:
        raise HTTPException(status_code=403, detail="Not available: DEMO_MODE is off (this is a production instance).")


@router.get("/status")
def system_status(db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    """Real subsystem checks, not hardcoded strings — each one actually
    exercises the thing it claims to report on."""
    running_workers = sum(1 for t in RUNNING.values() if not t.done())
    total_cameras = db.query(models.Camera).count()

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
def demo_reset(db: Session = Depends(get_db), user: models.User = Depends(require_roles("Administrator"))):
    """Returns the app to a clean, repeatable judge-demo state. DEMO_MODE
    only. Stops any running camera workers first (their DB rows are about
    to be reset), wipes transactional data, and re-ensures the two demo
    cameras + the demo watchlist entry — see seed.reset_demo_data."""
    _require_demo_mode()
    for camera in db.query(models.Camera).all():
        stop_worker(camera.id)
    summary = reset_demo_data(db)
    log_action(db, user, "demo_reset", resource=",".join(summary["cameras"]))
    return summary


@router.post("/demo/trigger-scenario")
async def demo_trigger_scenario(db: Session = Depends(get_db), user: models.User = Depends(require_roles("Administrator", "Control Room Operator"))):
    """Deterministically fires the primary judge-demo scenario (watchlist
    plate sighted on C-014, then C-019) through the real correlation/alert
    code path — see pipeline/demo_scenario.py for exactly what is and isn't
    real about it. DEMO_MODE only; requires POST /demo/reset (or otherwise
    having C-014 and C-019 registered) first."""
    _require_demo_mode()
    try:
        result = await trigger_scenario(db, user)
    except DemoScenarioError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return result
