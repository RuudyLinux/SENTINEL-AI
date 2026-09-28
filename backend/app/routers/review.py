"""ANPR review queue.

Gate-passing reads below plate_review_confidence_floor, and uncorroborated
reads, are marked pending_review for an operator to accept, correct or reject.
Every action is audited. A correction is recorded but doesn't re-point the
Plate's vehicle_id, which would affect watchlist matching, routes and risk.
"""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from .. import models, schemas
from ..audit import log_action
from ..db import get_db
from ..pipeline.anpr import normalize_plate
from ..security import get_current_user, require_operational_role

router = APIRouter(prefix="/api/review", tags=["review"])


@router.get("/queue", response_model=list[schemas.PlateOut])
def review_queue(
    # bounded like detections.py, SQLite treats LIMIT -1 as no limit
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db), user: models.User = Depends(get_current_user),
):
    """Plate sightings waiting on an operator, newest first."""
    return (
        db.query(models.Plate)
        .filter(models.Plate.review_status == "pending_review")
        .order_by(models.Plate.timestamp.desc())
        .limit(min(limit, 500))
        .all()
    )


def _get_plate(db: Session, plate_id: str) -> models.Plate:
    plate = db.query(models.Plate).filter(models.Plate.id == plate_id).first()
    if not plate:
        raise HTTPException(status_code=404, detail="Plate sighting not found")
    return plate


@router.post("/{plate_id}/accept", response_model=schemas.PlateOut)
def accept_read(plate_id: str, db: Session = Depends(get_db), user: models.User = Depends(require_operational_role)):
    """Operator confirms the OCR read as-is despite its low confidence."""
    plate = _get_plate(db, plate_id)
    plate.review_status = "auto_accepted"
    plate.reviewed_by = user.id
    plate.reviewed_at = datetime.utcnow()
    db.commit()
    log_action(db, user, "anpr_review_accept", resource=plate_id)
    return plate


@router.post("/{plate_id}/correct", response_model=schemas.PlateOut)
def correct_read(
    plate_id: str, payload: schemas.PlateReviewCorrectRequest,
    db: Session = Depends(get_db), user: models.User = Depends(require_operational_role),
):
    """Record the operator's plate text. The OCR output (plate_text_raw) and the
    repaired read (plate_text_normalized) are kept alongside corrected_text."""
    plate = _get_plate(db, plate_id)
    corrected = normalize_plate(payload.corrected_text)
    if not corrected:
        raise HTTPException(status_code=400, detail="corrected_text must not be empty")
    plate.corrected_text = corrected
    plate.review_status = "corrected"
    plate.reviewed_by = user.id
    plate.reviewed_at = datetime.utcnow()
    db.commit()
    log_action(db, user, "anpr_review_correct", resource=f"{plate_id}:{corrected}")
    return plate


@router.post("/{plate_id}/reject", response_model=schemas.PlateOut)
def reject_read(
    plate_id: str, payload: schemas.PlateReviewRejectRequest,
    db: Session = Depends(get_db), user: models.User = Depends(require_operational_role),
):
    """Mark the read unusable. The row stays (review_status=rejected), out of the
    queue but in the audit trail."""
    plate = _get_plate(db, plate_id)
    plate.review_status = "rejected"
    plate.reviewed_by = user.id
    plate.reviewed_at = datetime.utcnow()
    db.commit()
    log_action(db, user, "anpr_review_reject", resource=f"{plate_id}:{payload.reason or ''}")
    return plate
