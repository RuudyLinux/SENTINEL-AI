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
    """In-force entries by default; `include_inactive=true` for the full history.

    This listed every row regardless of `active` or `valid_until`, so a
    deactivated or expired entry stayed on the watchlist page looking exactly
    like a live one — same identifier, same priority, same Deactivate button,
    no indication it was already off. Deactivation appeared to do nothing.
    The rows are still here for audit; they are no longer presented as in force.
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

    Refuses a second IN-FORCE entry for the same (entity_type, identifier).
    Found by clicking SAVE three times against the running system: three
    identical in-force entries for one plate. The extra rows are not the real
    failure — what they do to Deactivate is. `plate_entry_in_force` takes
    `.first()`, so removing one of three identical entries leaves the plate
    exactly as watchlisted as before: the operator performs the documented
    removal, watches the vehicle stay flagged, and has nothing on screen that
    explains why. This codebase already fixed the mirror image of that once
    (a stale `watchlist_flag` surviving deactivation).

    409 naming the existing entry rather than silently de-duplicating: the
    operator may have meant to change the priority or reason, and quietly
    discarding that input would hide it from them. A deactivated or expired
    entry does not block a new one — re-adding a plate that was removed, or
    whose watch period lapsed, is ordinary work.
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
        # Recomputed rather than set to True: an entry created with a
        # `valid_until` already in the past is not in force, and must not flag
        # the vehicle as though it were.
        #
        # The flush is required, not decorative: SessionLocal is built with
        # autoflush=False (app/db.py), so without it the recompute below runs
        # against a database that cannot see the entry just added, concludes
        # nothing is in force, and leaves the vehicle unflagged.
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
        # The vehicle's cached flag must follow the entry it was derived from.
        # Without this, deactivation removed the entry but left the vehicle
        # flagged permanently — still shown as "⚠ WATCHLIST" everywhere, still
        # having snapshot evidence captured of it. Recomputed, not cleared:
        # a second, still-in-force entry for the same plate keeps the flag set.
        # Flushed first for the same reason as the create path: autoflush=False
        # means the recompute would otherwise still see this entry as active.
        db.flush()
        vehicle = db.query(models.Vehicle).filter(models.Vehicle.plate_text == entry.identifier).first()
        if vehicle:
            watchlist.refresh_vehicle_flag(db, vehicle)
    db.commit()
    log_action(db, user, "deactivate_watchlist_entry", resource=entry_id)
    return {"ok": True}
