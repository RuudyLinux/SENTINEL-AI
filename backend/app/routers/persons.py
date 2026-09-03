"""Person module: detection/tracking only. No face recognition or
re-identification is implemented — privacy-sensitive and marked ADVANCED
in the doc; out of scope for this build (see README non-goals).
"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from .. import models, schemas
from ..db import get_db
from ..security import get_current_user

router = APIRouter(prefix="/api/persons", tags=["persons"])


@router.get("/detections", response_model=list[schemas.DetectionOut])
def person_detections(db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    return (
        db.query(models.Detection)
        .filter(models.Detection.cls == "person")
        .order_by(models.Detection.timestamp.desc())
        .limit(200)
        .all()
    )
