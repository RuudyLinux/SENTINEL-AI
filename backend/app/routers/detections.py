from datetime import datetime
from typing import Optional
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from .. import models, schemas
from ..db import get_db
from ..security import get_current_user

router = APIRouter(prefix="/api/detections", tags=["detections"])


@router.get("", response_model=list[schemas.DetectionOut])
def list_detections(
    camera_id: Optional[str] = None,
    cls: Optional[str] = None,
    from_ts: Optional[datetime] = Query(None, alias="from"),
    to_ts: Optional[datetime] = Query(None, alias="to"),
    limit: int = 100,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    q = db.query(models.Detection)
    if camera_id:
        q = q.filter(models.Detection.camera_id == camera_id)
    if cls:
        q = q.filter(models.Detection.cls == cls)
    if from_ts:
        q = q.filter(models.Detection.timestamp >= from_ts)
    if to_ts:
        q = q.filter(models.Detection.timestamp <= to_ts)
    return q.order_by(models.Detection.timestamp.desc()).limit(limit).all()
