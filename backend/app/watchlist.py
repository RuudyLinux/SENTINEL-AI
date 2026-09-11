"""Whether a watchlist entry is IN FORCE right now — asked in one place.

Four call sites (rules_engine, correlate twice, the incident summary) each
asked this question independently as `active == True`, and every one of them
ignored `valid_until`. The column is on the model, accepted by the create
schema, and stored by the API, but nothing in the codebase ever compared it to
the clock — so an entry given an explicit expiry date kept matching plates
forever. An operator who sets an end date reasonably believes the entry stops
at that date; it did not.

`Vehicle.watchlist_flag` is a CACHE of the answer, not the answer. It is
written when a vehicle is first seen (correlate.upsert_vehicle_for_plate) and
refreshed whenever the entries covering its plate change, so the UI and the
worker's snapshot capture can test a boolean instead of running this query per
frame. Anything that decides whether to ACCUSE — fire a watchlist alert, or
tell an investigator "this vehicle matches an active watchlist entry" — must
consult the entry itself through this module, because a cache can be stale and
an accusation should not be.
"""
from datetime import datetime

from sqlalchemy import or_
from sqlalchemy.orm import Query, Session

from . import models


def entries_in_force(db: Session, now: datetime | None = None) -> Query:
    """Entries that are active AND not past their expiry.

    An entry with no `valid_until` never expires, which is the existing
    behaviour for the entries that have none.
    """
    at = now or datetime.utcnow()
    return db.query(models.WatchlistEntry).filter(
        models.WatchlistEntry.active == True,  # noqa: E712
        or_(models.WatchlistEntry.valid_until.is_(None), models.WatchlistEntry.valid_until > at),
    )


def plate_entry_in_force(db: Session, plate_text: str | None, now: datetime | None = None):
    """The in-force plate entry covering `plate_text`, or None.

    Plate identifiers are stored normalized by the create endpoint, and every
    caller here passes an already-normalized plate (the pipeline normalizes at
    OCR time), so this compares like with like.
    """
    if not plate_text:
        return None
    return entries_in_force(db, now).filter(
        models.WatchlistEntry.entity_type == "plate",
        models.WatchlistEntry.identifier == plate_text,
    ).first()


def refresh_vehicle_flag(db: Session, vehicle, now: datetime | None = None) -> bool:
    """Recompute a vehicle's cached flag from the entries actually in force.

    Called wherever the entry set for a plate changes. Deactivating an entry
    used to leave `watchlist_flag` set forever: the vehicle kept showing
    "⚠ WATCHLIST" across the UI, kept being CRITICAL-prioritised on the
    tracking page, and kept having snapshot evidence captured of it — for a
    plate an operator had explicitly removed from the watchlist. Does not
    commit; the caller owns the transaction.
    """
    if vehicle is None:
        return False
    in_force = plate_entry_in_force(db, vehicle.plate_text, now) is not None
    if bool(vehicle.watchlist_flag) != in_force:
        vehicle.watchlist_flag = in_force
    return in_force
