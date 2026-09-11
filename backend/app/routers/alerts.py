from datetime import datetime
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import models, schemas
from ..db import get_db
from ..security import get_current_user
from ..audit import log_action

router = APIRouter(prefix="/api/alerts", tags=["alerts"])

# P6: the only feedback values the platform will compute precision/FP-rate
# statistics from — a free-text value here would silently corrupt those
# aggregates, so it is validated, not merely stored.
_VALID_FEEDBACK = {"confirmed", "false_positive", "needs_review"}


@router.get("", response_model=list[schemas.AlertOut])
def list_alerts(
    severity: Optional[str] = None,
    status: Optional[str] = None,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    q = db.query(models.Alert)
    if severity:
        q = q.filter(models.Alert.severity == severity.upper())
    if status:
        q = q.filter(models.Alert.status == status)
    return q.order_by(models.Alert.timestamp.desc()).limit(200).all()


@router.get("/{alert_id}", response_model=schemas.AlertOut)
def get_alert(alert_id: str, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    a = db.query(models.Alert).filter(models.Alert.id == alert_id).first()
    if not a:
        raise HTTPException(status_code=404, detail="Alert not found")
    return a


@router.post("/{alert_id}/acknowledge", response_model=schemas.AlertOut)
def acknowledge(alert_id: str, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    a = db.query(models.Alert).filter(models.Alert.id == alert_id).first()
    if not a:
        raise HTTPException(status_code=404, detail="Alert not found")
    a.status = "acknowledged"
    a.acknowledged_by = user.id
    db.commit()
    log_action(db, user, "acknowledge_alert", resource=alert_id)
    return a


@router.post("/{alert_id}/escalate", response_model=schemas.AlertOut)
def escalate(alert_id: str, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    a = db.query(models.Alert).filter(models.Alert.id == alert_id).first()
    if not a:
        raise HTTPException(status_code=404, detail="Alert not found")
    a.status = "escalated"
    db.commit()
    log_action(db, user, "escalate_alert", resource=alert_id)
    return a


@router.post("/{alert_id}/dismiss", response_model=schemas.AlertOut)
def dismiss(alert_id: str, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
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
    db: Session = Depends(get_db), user: models.User = Depends(get_current_user),
):
    """Operator judgement on whether this alert was real (10/10 roadmap P6).

    This is separate from `status` (new/acknowledged/escalated/dismissed),
    which tracks WORKFLOW state, not accuracy — an alert can be dismissed for
    operational reasons while still being a genuine detection, or acknowledged
    and later found to be a false positive. `feedback` is the accuracy signal
    `GET /api/analytics/alert-precision` aggregates from; it is never inferred
    from `status`.
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
