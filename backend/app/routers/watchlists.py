from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import models, schemas
from ..db import get_db
from ..security import get_current_user, require_roles
from ..audit import log_action
from ..pipeline.anpr import normalize_plate
from .. import watchlist

router = APIRouter(prefix="/api/watchlists", tags=["watchlists"])


@router.get("", response_model=list[schemas.WatchlistOut])
def list_watchlist(
    entity_type: str | None = None,
    include_inactive: bool = False,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    """In-force entries by default, include_inactive=true for everything.

    Listing every row made a deactivated or expired entry look exactly like a
    live one, so Deactivate seemed to do nothing. The rows are kept for audit.
    """
    q = watchlist.entries_in_force(db) if not include_inactive else db.query(models.WatchlistEntry)
    if entity_type:
        q = q.filter(models.WatchlistEntry.entity_type == entity_type)
    return q.order_by(models.WatchlistEntry.valid_from.desc()).all()


@router.post("", response_model=schemas.WatchlistOut)
def create_watchlist_entry(
    payload: schemas.WatchlistCreate,
    db: Session = Depends(get_db),
    user: models.User = Depends(require_roles("Administrator", "Supervisor", "Investigator")),
):
    """Add one entity to the watchlist.

    409 if an in-force entry for the same (entity_type, identifier) exists.
    Clicking SAVE three times made three, and since plate_entry_in_force takes
    .first(), deactivating one left the plate just as flagged, with nothing on
    screen to say why.

    409 naming the existing entry rather than silently merging, since the
    operator may have wanted a different priority or reason. Deactivated or
    expired entries don't block re-adding.
    """
    identifier = normalize_plate(payload.identifier) if payload.entity_type == "plate" else payload.identifier
    existing = watchlist.entries_in_force(db).filter(
        models.WatchlistEntry.entity_type == payload.entity_type,
        models.WatchlistEntry.identifier == identifier,
    ).first()
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"{identifier} is already on the {payload.entity_type} watchlist "
                f"(priority {existing.priority}). Deactivate the existing entry before adding it again."
            ),
        )
    entry = models.WatchlistEntry(
        entity_type=payload.entity_type, identifier=identifier, reason=payload.reason,
        priority=payload.priority, valid_until=payload.valid_until, added_by=user.id,
    )
    db.add(entry)
    if payload.entity_type == "plate":
        # Recomputed, not just True: an entry with valid_until already past
        # isn't in force.
        # The flush is needed, SessionLocal has autoflush=False, so the
        # recompute wouldn't see the new entry and would leave it unflagged.
        db.flush()
        vehicle = db.query(models.Vehicle).filter(models.Vehicle.plate_text == identifier).first()
        if vehicle:
            watchlist.refresh_vehicle_flag(db, vehicle)
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
    if entry.entity_type == "plate":
        # The cached flag has to follow the entry. Without this a deactivated
        # plate stayed "⚠ WATCHLIST" everywhere and kept getting snapshots.
        # Recomputed, not cleared: another in-force entry keeps it set. Flush
        # first, same autoflush=False reason as create.
        db.flush()
        vehicle = db.query(models.Vehicle).filter(models.Vehicle.plate_text == entry.identifier).first()
        if vehicle:
            watchlist.refresh_vehicle_flag(db, vehicle)
    db.commit()
    log_action(db, user, "deactivate_watchlist_entry", resource=entry_id)
    return {"ok": True}
