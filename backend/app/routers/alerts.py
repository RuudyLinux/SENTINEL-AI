from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import models, schemas
from ..db import get_db
from ..security import get_current_user
from ..audit import log_action

router = APIRouter(prefix="/api/alerts", tags=["alerts"])


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
