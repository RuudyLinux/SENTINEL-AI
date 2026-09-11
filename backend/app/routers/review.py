"""Human-in-the-loop ANPR review (10/10 roadmap P7).

A Plate sighting read below `settings.plate_review_confidence_floor` is
never discarded — it already passed the real quality gate
(`anpr.passes_anpr_gate`) and is genuine, if uncertain, intelligence — but is
flagged `pending_review` (see `anpr.review_status_for`) so an operator can
accept, correct, or reject it rather than the system silently treating an
uncertain read as settled fact. Every action is audited and stamps
reviewer/timestamp, which is also exactly the labelled (OCR-said, human-said)
data pair a future ANPR accuracy improvement effort would need.

Deliberately does NOT re-point the Plate's `vehicle_id` on a correction —
that would ripple into watchlist matching, route reconstruction and risk
scoring, and needs its own dedicated verification before being wired live.
This module's scope is recording the correction and unblocking the review
queue, not silently changing what the rest of the platform already believes.
"""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import models, schemas
from ..audit import log_action
from ..db import get_db
from ..pipeline.anpr import normalize_plate
from ..security import get_current_user

router = APIRouter(prefix="/api/review", tags=["review"])


@router.get("/queue", response_model=list[schemas.PlateOut])
def review_queue(
    limit: int = 100, db: Session = Depends(get_db), user: models.User = Depends(get_current_user),
):
    """Plate sightings genuinely waiting on an operator, newest first."""
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
def accept_read(plate_id: str, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
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
    db: Session = Depends(get_db), user: models.User = Depends(get_current_user),
):
    """Operator supplies the true plate text. `plate_text_raw` (literal OCR
    output) and `plate_text_normalized` (grammar-repaired OCR output) are left
    untouched — `corrected_text` is a THIRD, separate fact: what a human
    confirmed it actually is. All three stay queryable for audit."""
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
    db: Session = Depends(get_db), user: models.User = Depends(get_current_user),
):
    """Operator determines this read is not usable intelligence (e.g. the
    plate is genuinely unreadable, or the localizer boxed a bumper sticker).
    The row is kept — never deleted — with review_status=rejected so it is
    excluded from the active queue but remains in the audit trail."""
    plate = _get_plate(db, plate_id)
    plate.review_status = "rejected"
    plate.reviewed_by = user.id
    plate.reviewed_at = datetime.utcnow()
    db.commit()
    log_action(db, user, "anpr_review_reject", resource=f"{plate_id}:{payload.reason or ''}")
    return plate
