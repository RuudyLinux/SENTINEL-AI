"""BUG-1 (found in the 10/10 debugging pass, 2026-09-11): concurrent first
sighting of the same never-before-seen plate on two DIFFERENT cameras — each
camera worker holds its own `SessionLocal()` — could create two separate
Vehicle rows for one real vehicle, because `upsert_vehicle_for_plate`'s
read-then-insert was a classic TOCTOU race with no DB constraint behind it
(`Vehicle.plate_text` was `index=True` but not `unique=True`).

This is silent, not a crash: `pipeline/correlate.py::get_route()` builds a
vehicle's cross-camera journey from `Plate.vehicle_id` — a real vehicle split
across two Vehicle rows loses half its route, half its sighting count, and
half its risk-scoring signal with no error anywhere.
"""
import asyncio
import uuid

from app import models
from app.db import SessionLocal
from app.pipeline.correlate import upsert_vehicle_for_plate


def test_the_database_rejects_a_second_vehicle_row_for_the_same_plate():
    """Deterministic reproduction of the ROOT CAUSE, at the exact layer the
    fix lives: two sessions, both confirming "nothing exists yet" for a
    brand-new plate BEFORE either one writes — precisely the shape of the
    race (session A's read cannot see session B's not-yet-committed insert,
    and vice versa) — then both proceeding to insert as if they'd each
    legitimately discovered a new vehicle. Before the fix (no unique
    constraint) the second `commit()` below succeeded silently, leaving two
    rows. After the fix it raises IntegrityError — loud and immediate,
    exactly what `upsert_vehicle_for_plate`'s recovery path now catches.
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
    """The full, real recovery path: two REAL concurrent asyncio tasks (each
    on its own session, `asyncio.to_thread`-offloaded flush — exactly how
    two camera workers behave) racing `upsert_vehicle_for_plate` for the
    SAME never-before-seen plate. Regardless of which one's write actually
    lands first, exactly one Vehicle row must exist afterward, both callers
    must get back a Vehicle for the SAME row, and — critically — neither call
    may raise. A fresh third session is used for the final read specifically
    to avoid SQLAlchemy's per-session identity map masking a stale in-memory
    value with an already-committed fresher one.
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
    """Schema-level proof the fix is a real DB constraint, not just
    application-layer luck: on a fresh database (exactly what this test
    suite runs against — see conftest.py), `Base.metadata.create_all()`
    must have created a genuine UNIQUE index/constraint on
    `vehicles.plate_text`, not merely a non-unique lookup index."""
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
    """The unique constraint must not accidentally forbid more than one
    Vehicle row with no plate at all — SQL UNIQUE constraints treat NULL as
    distinct from every other NULL, which is exactly the behavior needed
    and this test locks down."""
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
