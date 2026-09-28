"""Real aggregates from the DB at request time, no hard-coded demo numbers."""
from datetime import datetime, timedelta
from sqlalchemy import extract, func
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from .. import models
from ..db import get_db
from ..security import get_current_user

router = APIRouter(prefix="/api/analytics", tags=["analytics"])


@router.get("/overview")
def overview(db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    # Retired cameras are history, not part of the operational fleet.
    active = db.query(models.Camera).filter(models.Camera.retired == False)  # noqa: E712
    total_cameras = active.count()
    online_cameras = active.filter(models.Camera.status == "online").count()
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
    """Detections per hour over the last 24 hours.

    extract(), not strftime(): SQLAlchemy passes unknown functions straight
    through and strftime is SQLite only. On a real PostgreSQL server:

        (psycopg.errors.UndefinedFunction) function strftime(unknown,
        timestamp without time zone) does not exist

    so the 24h chart 500'd in production and passed every SQLite test.
    extract() is translated per dialect; the label is formatted in Python.
    """
    since = datetime.utcnow() - timedelta(hours=24)
    parts = (
        extract("year", models.Detection.timestamp).label("y"),
        extract("month", models.Detection.timestamp).label("m"),
        extract("day", models.Detection.timestamp).label("d"),
        extract("hour", models.Detection.timestamp).label("h"),
    )
    rows = (
        db.query(*parts, func.count().label("count"))
        .filter(models.Detection.timestamp >= since)
        .group_by(*parts)
        .order_by(*parts)
        .all()
    )
    return [
        {"hour": f"{int(r.y):04d}-{int(r.m):02d}-{int(r.d):02d} {int(r.h):02d}:00", "count": r.count}
        for r in rows
    ]


@router.get("/alerts-by-type")
def alerts_by_type(db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    rows = db.query(models.Alert.severity, func.count()).group_by(models.Alert.severity).all()
    return [{"severity": s, "count": c} for s, c in rows]


@router.get("/camera-uptime")
def camera_uptime(db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    cams = db.query(models.Camera).filter(models.Camera.retired == False).all()  # noqa: E712
    return [{"camera_code": c.camera_code, "status": c.status, "fps": c.fps, "error_count": c.error_count} for c in cams]


@router.get("/ai-performance")
def ai_performance(db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    """Measured detection volume and ANPR read rate. No accuracy numbers;
    precision/recall need labelled ground truth we don't have here (doc §65).
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


# Below this many reviewed alerts a rate is noise; one dismissed alert would
# print "100% false-positive rate". Raise it once real usage builds up.
MIN_FEEDBACK_SAMPLE_SIZE = 20


@router.get("/alert-precision")
def alert_precision(db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    """Alert quality from operator feedback.

    Only from Alert.feedback (routers/alerts.py submit_feedback), never from
    status, which is workflow, not accuracy. Under MIN_FEEDBACK_SAMPLE_SIZE
    it reports insufficient_sample instead of a rate.
    """
    total_alerts = db.query(models.Alert).count()
    confirmed = db.query(models.Alert).filter(models.Alert.feedback == "confirmed").count()
    false_positive = db.query(models.Alert).filter(models.Alert.feedback == "false_positive").count()
    needs_review = db.query(models.Alert).filter(models.Alert.feedback == "needs_review").count()
    reviewed = confirmed + false_positive + needs_review
    dismissed = db.query(models.Alert).filter(models.Alert.status == "dismissed").count()

    result = {
        "total_alerts": total_alerts,
        "reviewed_alerts": reviewed,
        "confirmed": confirmed,
        "false_positive": false_positive,
        "needs_review": needs_review,
        "dismissed_status": dismissed,
        "min_sample_size": MIN_FEEDBACK_SAMPLE_SIZE,
        "sample_sufficient": reviewed >= MIN_FEEDBACK_SAMPLE_SIZE,
    }
    if reviewed < MIN_FEEDBACK_SAMPLE_SIZE:
        result["precision"] = None
        result["false_positive_rate"] = None
        result["note"] = (
            f"insufficient_sample: only {reviewed} alert(s) have operator feedback "
            f"(need {MIN_FEEDBACK_SAMPLE_SIZE}) — no rate is reported to avoid a "
            "misleading figure from a handful of reviews."
        )
    else:
        result["precision"] = round(confirmed / reviewed, 4)
        result["false_positive_rate"] = round(false_positive / reviewed, 4)
        result["note"] = f"Computed from {reviewed} operator-reviewed alert(s)."
    return result
