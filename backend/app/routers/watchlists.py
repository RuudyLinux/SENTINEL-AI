from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import models, schemas
from ..db import get_db
from ..security import get_current_user, require_roles
from ..audit import log_action
from ..pipeline.anpr import normalize_plate

router = APIRouter(prefix="/api/watchlists", tags=["watchlists"])


@router.get("", response_model=list[schemas.WatchlistOut])
def list_watchlist(entity_type: str | None = None, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    q = db.query(models.WatchlistEntry)
    if entity_type:
        q = q.filter(models.WatchlistEntry.entity_type == entity_type)
    return q.order_by(models.WatchlistEntry.valid_from.desc()).all()


@router.post("", response_model=schemas.WatchlistOut)
def create_watchlist_entry(
    payload: schemas.WatchlistCreate,
    db: Session = Depends(get_db),
    user: models.User = Depends(require_roles("Administrator", "Supervisor", "Investigator")),
):
    identifier = normalize_plate(payload.identifier) if payload.entity_type == "plate" else payload.identifier
    entry = models.WatchlistEntry(
        entity_type=payload.entity_type, identifier=identifier, reason=payload.reason,
        priority=payload.priority, valid_until=payload.valid_until, added_by=user.id,
    )
    db.add(entry)
    if payload.entity_type == "plate":
        vehicle = db.query(models.Vehicle).filter(models.Vehicle.plate_text == identifier).first()
        if vehicle:
            vehicle.watchlist_flag = True
    db.commit()
    db.refresh(entry)
    log_action(db, user, "create_watchlist_entry", resource=identifier)
    return entry


@router.delete("/{entry_id}")
def deactivate_entry(entry_id: str, db: Session = Depends(get_db), user: models.User = Depends(require_roles("Administrator", "Supervisor"))):
    entry = db.query(models.WatchlistEntry).filter(models.WatchlistEntry.id == entry_id).first()
    if not entry:
        raise HTTPException(status_code=404, detail="Entry not found")
    entry.active = False
    db.commit()
    log_action(db, user, "deactivate_watchlist_entry", resource=entry_id)
    return {"ok": True}
