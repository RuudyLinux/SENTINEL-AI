"""Two cameras (each with its own session) first seeing the same new plate
at the same time could create two Vehicle rows: upsert_vehicle_for_plate
read then inserted with no unique constraint behind it.

Silent, not a crash: get_route builds the journey from Plate.vehicle_id, so
a split vehicle loses half its route, sightings and risk signal.
"""
import asyncio
import uuid

from app import models
from app.db import SessionLocal
from app.pipeline.correlate import upsert_vehicle_for_plate


def test_the_database_rejects_a_second_vehicle_row_for_the_same_plate():
    """The root cause, deterministic: two sessions both see "nothing yet"
    before either writes, then both insert. Without the constraint the second
    commit succeeded quietly; now it raises IntegrityError, which the
    upsert's recovery path catches.
    """
    from sqlalchemy.exc import IntegrityError

    plate = f"GJ05RC{uuid.uuid4().hex[:4].upper()}"
    session_a = SessionLocal()
    session_b = SessionLocal()
    try:
        # The race window: both sessions independently see nothing, before
        # either one has written anything.
        assert session_a.query(models.Vehicle).filter(models.Vehicle.plate_text == plate).first() is None
        assert session_b.query(models.Vehicle).filter(models.Vehicle.plate_text == plate).first() is None

        session_a.add(models.Vehicle(plate_text=plate, plate_confidence=0.70))
        session_a.commit()  # session A "wins" the race

        # Session B still acts on its EARLIER read (which saw nothing) and
        # tries to insert its own row for the same plate.
        session_b.add(models.Vehicle(plate_text=plate, plate_confidence=0.91))
        try:
            session_b.commit()
        except IntegrityError:
            session_b.rollback()
        else:
            raise AssertionError(
                "a second Vehicle row for the same plate_text was accepted — "
                "Vehicle.plate_text is missing its unique constraint (BUG-1 regressed)."
            )

        # Exactly one row survives.
        verifier = SessionLocal()
        try:
            rows = verifier.query(models.Vehicle).filter(models.Vehicle.plate_text == plate).all()
            assert len(rows) == 1
        finally:
            verifier.close()
    finally:
        session_a.close()
        session_b.close()


def test_concurrent_upsert_calls_for_a_brand_new_plate_never_produce_duplicates():
    """The real recovery path: two concurrent tasks on separate sessions
    (like two workers) racing upsert_vehicle_for_plate for one new plate.
    Exactly one row afterwards, both callers get it, neither raises. Read
    back on a fresh session so the identity map can't hide anything.
    """
    plate = f"GJ05RC{uuid.uuid4().hex[:4].upper()}"
    session_a = SessionLocal()
    session_b = SessionLocal()
    try:
        async def race():
            return await asyncio.gather(
                upsert_vehicle_for_plate(session_a, plate, 0.70),
                upsert_vehicle_for_plate(session_b, plate, 0.91),
            )

        vehicle_a, vehicle_b = asyncio.run(race())
        session_a.commit()
        session_b.commit()

        assert vehicle_a.plate_text == plate
        assert vehicle_b.plate_text == plate

        verifier = SessionLocal()
        try:
            rows = verifier.query(models.Vehicle).filter(models.Vehicle.plate_text == plate).all()
            assert len(rows) == 1, (
                f"expected exactly 1 Vehicle row for {plate} after concurrent first-sighting "
                f"upserts, found {len(rows)} — cross-camera correlation would be silently broken."
            )
            # The surviving row reflects the higher confidence observed
            # across the two racing reads, never silently the lower one.
            assert rows[0].plate_confidence == 0.91
        finally:
            verifier.close()
    finally:
        session_a.close()
        session_b.close()


def test_vehicle_plate_text_has_a_real_database_level_unique_constraint():
    """create_all on a fresh DB (what the suite uses) makes a real UNIQUE
    index on vehicles.plate_text, not just a lookup index."""
    from sqlalchemy import inspect
    from app.db import engine

    insp = inspect(engine)
    unique_constraints = insp.get_unique_constraints("vehicles")
    unique_indexes = [ix for ix in insp.get_indexes("vehicles") if ix.get("unique")]
    covers_plate_text = any("plate_text" in uc["column_names"] for uc in unique_constraints) or any(
        "plate_text" in ix["column_names"] for ix in unique_indexes
    )
    assert covers_plate_text, (
        "vehicles.plate_text has no real UNIQUE constraint/index at the database level — "
        "the race-condition fix in upsert_vehicle_for_plate has nothing to actually rely on."
    )


def test_multiple_null_plate_texts_are_still_allowed():
    """Several plate-less vehicles are still allowed; UNIQUE treats NULLs as
    distinct."""
    a = models.Vehicle(plate_text=None, vehicle_type="car")
    b = models.Vehicle(plate_text=None, vehicle_type="truck")
    session = SessionLocal()
    try:
        session.add(a)
        session.add(b)
        session.commit()  # must not raise
        assert a.id != b.id
    finally:
        session.query(models.Vehicle).filter(models.Vehicle.id.in_([a.id, b.id])).delete(synchronize_session=False)
        session.commit()
        session.close()
