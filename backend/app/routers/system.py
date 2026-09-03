from datetime import datetime
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from .. import models
from ..db import get_db
from ..security import get_current_user
from ..pipeline.worker import RUNNING

router = APIRouter(prefix="/api/system", tags=["system"])


@router.get("/status")
def system_status(db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    running_workers = sum(1 for t in RUNNING.values() if not t.done())
    total_cameras = db.query(models.Camera).count()
    return {
        "timestamp": datetime.utcnow().isoformat(),
        "subsystems": [
            {"name": "CAMERA NETWORK", "status": "OPERATIONAL" if total_cameras >= 0 else "DEGRADED"},
            {"name": "AI ENGINE", "status": "OPERATIONAL" if running_workers > 0 or total_cameras == 0 else "DEGRADED"},
            {"name": "DATABASE", "status": "OPERATIONAL"},
            {"name": "SEARCH", "status": "OPERATIONAL"},
            {"name": "STORAGE", "status": "OPERATIONAL"},
        ],
        "cameras_registered": total_cameras,
        "camera_workers_running": running_workers,
    }
