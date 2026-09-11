"""BUG investigation D2: can concurrent cross-camera alerts for ONE vehicle
open TWO incidents?

`rules_engine.evaluate` does read-then-create for incidents
(`_find_correlatable_incident` -> `db.add(models.Incident(...))`), the same
shape as the vehicle race that WAS real (see test_vehicle_upsert_race.py).
Two cameras seeing a watchlisted vehicle seconds apart is routine, and a
duplicate incident is exactly the operator-flooding that correlation exists
to prevent.

It was attacked 8 times during the debugging pass and never reproduced. The
structural reason: the entire read-decide-create block contains NO await, so
two tasks on one event loop cannot interleave inside it — whichever runs
second necessarily runs after the first has finished creating.

A partial unique index was deliberately NOT added. An incident legitimately
recurs for the same vehicle over time, so a constraint could reject a valid
write from inside the camera loop — a worse failure mode than a rare
duplicate. Instead the INVARIANT is stress-tested here, so if the window
ever opens (a future `await` added inside that block would open it), this
fails loudly and the fix can be designed against a real reproduction.
"""
import asyncio
import uuid

import pytest

from app import models
from app.db import SessionLocal
from app.pipeline import rules_engine

ROUNDS = 12


@pytest.fixture(autouse=True)
def _clean_cooldowns():
    rules_engine._last_alert_at.clear()
    rules_engine._zone_presence.clear()
    yield
    rules_engine._last_alert_at.clear()
    rules_engine._zone_presence.clear()


def _camera(session, code: str) -> models.Camera:
    camera = models.Camera(
        camera_code=code, name=code, source_type="video_file", source_uri="x.mp4",
    )
    session.add(camera)
    session.commit()
    session.refresh(camera)
    return camera


def _detection(session, camera) -> models.Detection:
    detection = models.Detection(
        camera_id=camera.id, cls="car", confidence=0.9, bbox=[1, 1, 50, 50], track_id="1",
    )
    session.add(detection)
    session.commit()
    session.refresh(detection)
    return detection


def test_concurrent_cross_camera_alerts_never_open_two_incidents_for_one_vehicle(db_session):
    """Each round: one watchlisted vehicle, two cameras on two separate
    sessions (exactly how two camera workers behave), both evaluating
    concurrently. Invariant: exactly ONE incident for that vehicle."""
    failures = []

    for round_index in range(ROUNDS):
        rules_engine._last_alert_at.clear()
        plate = f"GJ05DS{uuid.uuid4().hex[:4].upper()}"
        db_session.add(models.WatchlistEntry(
            entity_type="plate", identifier=plate, priority="CRITICAL", active=True, reason="stress",
        ))
        vehicle = models.Vehicle(plate_text=plate, plate_confidence=0.95, watchlist_flag=True)
        db_session.add(vehicle)
        db_session.commit()
        vehicle_id = vehicle.id

        session_a, session_b = SessionLocal(), SessionLocal()
        try:
            camera_a = _camera(session_a, f"DS-A-{uuid.uuid4().hex[:6]}")
            camera_b = _camera(session_b, f"DS-B-{uuid.uuid4().hex[:6]}")
            detection_a = _detection(session_a, camera_a)
            detection_b = _detection(session_b, camera_b)
            vehicle_a = session_a.query(models.Vehicle).filter(models.Vehicle.id == vehicle_id).first()
            vehicle_b = session_b.query(models.Vehicle).filter(models.Vehicle.id == vehicle_id).first()

            async def race():
                return await asyncio.gather(
                    rules_engine.evaluate(session_a, camera_a, detection_a, 100, 100, vehicle_a),
                    rules_engine.evaluate(session_b, camera_b, detection_b, 100, 100, vehicle_b),
                    return_exceptions=True,
                )

            results = asyncio.run(race())
            session_a.commit()
            session_b.commit()

            raised = [r for r in results if isinstance(r, BaseException)]
            if raised:
                failures.append(f"round {round_index}: evaluate raised {raised[0]!r}")
                continue

            verifier = SessionLocal()
            try:
                incidents = verifier.query(models.Incident).filter(
                    models.Incident.vehicle_id == vehicle_id
                ).all()
                if len(incidents) != 1:
                    failures.append(
                        f"round {round_index}: {len(incidents)} incidents for one vehicle "
                        f"({[i.id for i in incidents]})"
                    )
            finally:
                verifier.close()
        finally:
            session_a.close()
            session_b.close()

    assert not failures, (
        f"the incident-duplication window opened in {len(failures)}/{ROUNDS} rounds:\n  "
        + "\n  ".join(failures)
        + "\n\nIf this fires, an `await` was likely introduced between "
          "_find_correlatable_incident and the incident flush in rules_engine.evaluate, "
          "which lets two camera workers interleave inside the read-decide-create block."
    )


def test_the_second_alert_correlates_rather_than_opening_its_own_incident(db_session):
    """The positive half of the invariant: the losing task must ATTACH to the
    winner's incident (recorded with a correlation reason), not be silently
    dropped. A test that only counted incidents would pass if the second
    alert vanished entirely."""
    rules_engine._last_alert_at.clear()
    plate = f"GJ05DC{uuid.uuid4().hex[:4].upper()}"
    db_session.add(models.WatchlistEntry(
        entity_type="plate", identifier=plate, priority="CRITICAL", active=True, reason="stress",
    ))
    vehicle = models.Vehicle(plate_text=plate, plate_confidence=0.95, watchlist_flag=True)
    db_session.add(vehicle)
    db_session.commit()
    vehicle_id = vehicle.id

    session_a, session_b = SessionLocal(), SessionLocal()
    try:
        camera_a = _camera(session_a, f"DC-A-{uuid.uuid4().hex[:6]}")
        camera_b = _camera(session_b, f"DC-B-{uuid.uuid4().hex[:6]}")
        detection_a = _detection(session_a, camera_a)
        detection_b = _detection(session_b, camera_b)
        vehicle_a = session_a.query(models.Vehicle).filter(models.Vehicle.id == vehicle_id).first()
        vehicle_b = session_b.query(models.Vehicle).filter(models.Vehicle.id == vehicle_id).first()

        async def race():
            return await asyncio.gather(
                rules_engine.evaluate(session_a, camera_a, detection_a, 100, 100, vehicle_a),
                rules_engine.evaluate(session_b, camera_b, detection_b, 100, 100, vehicle_b),
            )

        alerts_a, alerts_b = asyncio.run(race())
        session_a.commit()
        session_b.commit()

        verifier = SessionLocal()
        try:
            incidents = verifier.query(models.Incident).filter(
                models.Incident.vehicle_id == vehicle_id
            ).all()
            assert len(incidents) == 1

            alert_ids = {a.id for a in (alerts_a + alerts_b)}
            links = verifier.query(models.IncidentAlert).filter(
                models.IncidentAlert.alert_id.in_(alert_ids)
            ).all()
            # Both alerts belong to the one incident, and the second says why.
            assert {link.incident_id for link in links} == {incidents[0].id}
            assert any("within the correlation window" in (link.correlation_reason or "") for link in links)
        finally:
            verifier.close()
    finally:
        session_a.close()
        session_b.close()
