"""All numbers here are real aggregates computed from the DB at request time —
per the doc's AI-honesty rule, nothing here is a hard-coded demo number.
"""
from datetime import datetime, timedelta
from sqlalchemy import func
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from .. import models
from ..db import get_db
from ..security import get_current_user

router = APIRouter(prefix="/api/analytics", tags=["analytics"])


@router.get("/overview")
def overview(db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    total_cameras = db.query(models.Camera).count()
    online_cameras = db.query(models.Camera).filter(models.Camera.status == "online").count()
    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    detections_today = db.query(models.Detection).filter(models.Detection.timestamp >= today_start).count()
    active_alerts = db.query(models.Alert).filter(models.Alert.status == "new").count()
    critical_alerts = db.query(models.Alert).filter(models.Alert.status == "new", models.Alert.severity == "CRITICAL").count()
    high_alerts = db.query(models.Alert).filter(models.Alert.status == "new", models.Alert.severity == "HIGH").count()
    medium_alerts = db.query(models.Alert).filter(models.Alert.status == "new", models.Alert.severity == "MEDIUM").count()
    open_incidents = db.query(models.Incident).filter(models.Incident.status != "closed").count()
    plates_today = db.query(models.Plate).filter(models.Plate.timestamp >= today_start).count()

    return {
        "cameras": {"total": total_cameras, "online": online_cameras, "offline": total_cameras - online_cameras},
        "alerts": {"active": active_alerts, "critical": critical_alerts, "high": high_alerts, "medium": medium_alerts},
        "incidents": {"open": open_incidents},
        "ai_events": {"detections_today": detections_today, "plates_today": plates_today},
    }


@router.get("/events-by-hour")
def events_by_hour(db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    since = datetime.utcnow() - timedelta(hours=24)
    rows = (
        db.query(func.strftime("%Y-%m-%d %H:00", models.Detection.timestamp).label("hour"), func.count().label("count"))
        .filter(models.Detection.timestamp >= since)
        .group_by("hour")
        .order_by("hour")
        .all()
    )
    return [{"hour": r.hour, "count": r.count} for r in rows]


@router.get("/alerts-by-type")
def alerts_by_type(db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    rows = db.query(models.Alert.severity, func.count()).group_by(models.Alert.severity).all()
    return [{"severity": s, "count": c} for s, c in rows]


@router.get("/camera-uptime")
def camera_uptime(db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    cams = db.query(models.Camera).all()
    return [{"camera_code": c.camera_code, "status": c.status, "fps": c.fps, "error_count": c.error_count} for c in cams]


@router.get("/ai-performance")
def ai_performance(db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    """Reports measured detection volumes and ANPR read rate. No accuracy
    percentages are fabricated — precision/recall need a labeled ground-truth
    set, which is not available in this environment (doc §65 honesty rule).
    """
    total_detections = db.query(models.Detection).count()
    person_detections = db.query(models.Detection).filter(models.Detection.cls == "person").count()
    vehicle_detections = total_detections - person_detections
    total_plate_reads = db.query(models.Plate).count()
    plausible_plate_reads = db.query(models.Plate).filter(models.Plate.plate_text_normalized != "").count()
    avg_conf = db.query(func.avg(models.Detection.confidence)).scalar() or 0.0
    return {
        "total_detections": total_detections,
        "person_detections": person_detections,
        "vehicle_detections": vehicle_detections,
        "average_detection_confidence": round(float(avg_conf), 3),
        "total_plate_reads": total_plate_reads,
        "non_empty_plate_reads": plausible_plate_reads,
        "note": "Precision/recall/exact-match rate require a labeled test set; not computed here. See README.",
    }
