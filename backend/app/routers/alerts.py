from datetime import datetime
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from .. import models, schemas
from ..db import get_db
from ..security import get_current_user, require_operational_role
from ..audit import log_action

router = APIRouter(prefix="/api/alerts", tags=["alerts"])

# the only feedback values precision/FP stats are computed from, so validated
# instead of free text
_VALID_FEEDBACK = {"confirmed", "false_positive", "needs_review"}


@router.get("", response_model=list[schemas.AlertOut])
def list_alerts(
    severity: Optional[str] = None,
    status: Optional[str] = None,
    camera_id: Optional[str] = None,
    limit: int = Query(default=200, ge=1, le=500),
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    """Most recent alerts, filtered by any mix of the params.

    limit defaults to 200 (what the Alert Center always showed) but is now
    settable, bounded like the other lists: ge=1 because SQLite reads
    LIMIT -1 as no limit, le=500 so one request can't scan the table.

    camera_id filters server side. The per-camera view used to filter the
    200 most recent system-wide on the client, so a camera with older alerts
    showed an empty list, same as one with none.
    """
    q = db.query(models.Alert)
    if severity:
        q = q.filter(models.Alert.severity == severity.upper())
    if status:
        q = q.filter(models.Alert.status == status)
    if camera_id:
        q = q.filter(models.Alert.camera_id == camera_id)
    return q.order_by(models.Alert.timestamp.desc()).limit(limit).all()


@router.get("/{alert_id}", response_model=schemas.AlertOut)
def get_alert(alert_id: str, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    a = db.query(models.Alert).filter(models.Alert.id == alert_id).first()
    if not a:
        raise HTTPException(status_code=404, detail="Alert not found")
    return a


@router.post("/{alert_id}/acknowledge", response_model=schemas.AlertOut)
def acknowledge(alert_id: str, db: Session = Depends(get_db), user: models.User = Depends(require_operational_role)):
    a = db.query(models.Alert).filter(models.Alert.id == alert_id).first()
    if not a:
        raise HTTPException(status_code=404, detail="Alert not found")
    a.status = "acknowledged"
    a.acknowledged_by = user.id
    db.commit()
    log_action(db, user, "acknowledge_alert", resource=alert_id)
    return a


@router.post("/{alert_id}/escalate", response_model=schemas.AlertOut)
def escalate(alert_id: str, db: Session = Depends(get_db), user: models.User = Depends(require_operational_role)):
    a = db.query(models.Alert).filter(models.Alert.id == alert_id).first()
    if not a:
        raise HTTPException(status_code=404, detail="Alert not found")
    a.status = "escalated"
    db.commit()
    log_action(db, user, "escalate_alert", resource=alert_id)
    return a


@router.post("/{alert_id}/dismiss", response_model=schemas.AlertOut)
def dismiss(alert_id: str, db: Session = Depends(get_db), user: models.User = Depends(require_operational_role)):
    a = db.query(models.Alert).filter(models.Alert.id == alert_id).first()
    if not a:
        raise HTTPException(status_code=404, detail="Alert not found")
    a.status = "dismissed"
    db.commit()
    log_action(db, user, "dismiss_alert", resource=alert_id)
    return a


@router.post("/{alert_id}/feedback", response_model=schemas.AlertOut)
def submit_feedback(
    alert_id: str, payload: schemas.AlertFeedbackRequest,
    db: Session = Depends(get_db), user: models.User = Depends(require_operational_role),
):
    """Operator verdict on whether the alert was real.

    Not the same as status (new/acknowledged/escalated/dismissed), which is
    workflow: an alert can be dismissed for operational reasons and still be
    a real detection. feedback is what GET /api/analytics/alert-precision
    uses; it's never inferred from status.
    """
    if payload.feedback not in _VALID_FEEDBACK:
        raise HTTPException(status_code=400, detail=f"feedback must be one of {sorted(_VALID_FEEDBACK)}")
    a = db.query(models.Alert).filter(models.Alert.id == alert_id).first()
    if not a:
        raise HTTPException(status_code=404, detail="Alert not found")
    a.feedback = payload.feedback
    a.feedback_reason = payload.reason
    a.feedback_by = user.id
    a.feedback_at = datetime.utcnow()
    db.commit()
    log_action(db, user, f"alert_feedback:{payload.feedback}", resource=alert_id)
    return a
