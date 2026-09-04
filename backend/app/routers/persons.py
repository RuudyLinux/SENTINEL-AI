"""Person module: detection/tracking, plus cross-camera appearance-SIMILARITY
search (Phase 5). No face recognition and no identity resolution anywhere in this
module — `/{detection_id}/similar` ranks other person detections by how visually
similar their crop's color signature is (pipeline/appearance.py), for an
investigator to review and confirm manually. It never claims to know who anyone
is, and detections with no stored signature are simply excluded, never guessed at.
"""
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from .. import models, schemas
from ..db import get_db
from ..security import get_current_user
from ..pipeline.correlate import find_similar_person_detections

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


@router.get("/{detection_id}/similar")
def similar_person_detections(
    detection_id: str,
    min_similarity: float = Query(0.6, ge=0.0, le=1.0),
    exclude_camera_id: str | None = None,
    after: datetime | None = None,
    before: datetime | None = None,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    """Appearance-SIMILARITY candidates only — not identity verification. Returns
    [] (not a 404) when the reference detection has no stored signature or simply
    has no candidates above `min_similarity`."""
    reference = db.query(models.Detection).filter(models.Detection.id == detection_id).first()
    if reference is None:
        raise HTTPException(status_code=404, detail="Detection not found")
    if reference.cls != "person":
        raise HTTPException(status_code=400, detail="Reference detection is not a person detection")
    results = find_similar_person_detections(
        db, detection_id, min_similarity=min_similarity,
        exclude_camera_id=exclude_camera_id, after=after, before=before,
    )
    return {"reference_detection_id": detection_id, "candidates": results}
