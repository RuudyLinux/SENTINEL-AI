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
    # Bounded, matching self_heal.py's convention. An unvalidated int here
    # meant `limit=-1` reached SQLite as `LIMIT -1`, which means NO limit, and
    # `limit=100000000` had the same effect — either one returns the entire
    # detections table (the largest table the platform writes, one row per
    # detected object per frame per camera) in a single JSON response, to any
    # authenticated user. Measured on a 120-row test database: the default
    # returned 100, `limit=-1` returned all 120, `limit=100000000` returned
    # all 120.
    limit: int = Query(100, ge=1, le=500),
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
