"""Is a watchlist entry in force right now. Asked here and nowhere else.

The four places that asked (rules_engine, correlate twice, incident summary)
each checked `active == True` and ignored valid_until, so an entry with an
end date kept matching forever.

Vehicle.watchlist_flag is a cache of the answer: set when the vehicle is
first seen and refreshed when its plate's entries change, so the UI and
snapshot capture can check a bool instead of querying per frame. Anything
that accuses (fires a watchlist alert, tells an investigator there's a
match) must ask the entry through this module; a cache can be stale.
"""
from datetime import datetime

from sqlalchemy import or_
from sqlalchemy.orm import Query, Session

from . import models


def entries_in_force(db: Session, now: datetime | None = None) -> Query:
    """Active and not expired. No valid_until = never expires."""
    at = now or datetime.utcnow()
    return db.query(models.WatchlistEntry).filter(
        models.WatchlistEntry.active == True,  # noqa: E712
        or_(models.WatchlistEntry.valid_until.is_(None), models.WatchlistEntry.valid_until > at),
    )


def plate_entry_in_force(db: Session, plate_text: str | None, now: datetime | None = None):
    """In-force plate entry for plate_text, or None. Identifiers are stored
    normalized and callers pass normalized plates, so this compares like
    with like.
    """
    if not plate_text:
        return None
    return entries_in_force(db, now).filter(
        models.WatchlistEntry.entity_type == "plate",
        models.WatchlistEntry.identifier == plate_text,
    ).first()


def refresh_vehicle_flag(db: Session, vehicle, now: datetime | None = None) -> bool:
    """Recompute a vehicle's cached flag from the entries in force. Call it
    whenever a plate's entries change; deactivation used to leave the flag
    on forever (still "⚠ WATCHLIST", still prioritised, still snapshotted).
    Doesn't commit.
    """
    if vehicle is None:
        return False
    in_force = plate_entry_in_force(db, vehicle.plate_text, now) is not None
    if bool(vehicle.watchlist_flag) != in_force:
        vehicle.watchlist_flag = in_force
    return in_force
