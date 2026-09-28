"""Whether a watchlist entry is in force right now; the single place this is
decided (active and within its validity window).

Vehicle.watchlist_flag caches the answer for cheap UI and snapshot checks.
Anything that accuses (raises a watchlist alert, reports a match to an
investigator) must ask through this module, since a cache can be stale.
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
    """In-force plate entry for plate_text, or None. Both sides are normalized
    plates.
    """
    if not plate_text:
        return None
    return entries_in_force(db, now).filter(
        models.WatchlistEntry.entity_type == "plate",
        models.WatchlistEntry.identifier == plate_text,
    ).first()


def refresh_vehicle_flag(db: Session, vehicle, now: datetime | None = None) -> bool:
    """Recompute a vehicle's cached flag from the entries in force. Call it
    whenever a plate's entries change. Doesn't commit.
    """
    if vehicle is None:
        return False
    in_force = plate_entry_in_force(db, vehicle.plate_text, now) is not None
    if bool(vehicle.watchlist_flag) != in_force:
        vehicle.watchlist_flag = in_force
    return in_force
