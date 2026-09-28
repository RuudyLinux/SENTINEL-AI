"""Taking a plate off the watchlist actually takes it off.

1. Deactivating didn't stop alerts. DELETE set active = False, but
   rules_engine went on watchlist_flag alone and nothing cleared it, so
   CRITICAL alerts kept coming, each saying the plate "matches an active
   watchlist entry" (an `else "HIGH"` fallback hid the missing entry).
2. valid_until was stored but never compared to the clock.
3. The stale flag kept snapshots being captured (worker.py) and "⚠ WATCHLIST"
   showing on the vehicles/search/tracking pages.
4. GET /api/watchlists listed deactivated and expired entries like live ones,
   so Deactivate looked broken.
"""
import asyncio
import random
import string
import uuid
from datetime import datetime, timedelta

import pytest

from app import models, watchlist
from app.pipeline import correlate, rules_engine


@pytest.fixture(autouse=True)
def _clean_cooldowns():
    rules_engine._alert_claims.clear()
    yield
    rules_engine._alert_claims.clear()


@pytest.fixture
def auth(admin_token):
    return {"Authorization": f"Bearer {admin_token}"}


def _plate() -> str:
    letters = "".join(random.choice(string.ascii_uppercase) for _ in range(2))
    return f"GJ05{letters}{random.randint(1000, 9999)}"


def _camera(db) -> models.Camera:
    camera = models.Camera(
        camera_code=f"WLL-{uuid.uuid4().hex[:8]}", name="watchlist lifecycle cam",
        source_type="mock_vms", source_uri="",
    )
    db.add(camera)
    db.flush()
    return camera


def _detection(db, camera) -> models.Detection:
    detection = models.Detection(
        camera_id=camera.id, cls="car", confidence=0.9, bbox=[10.0, 10.0, 90.0, 90.0],
        track_id=str(random.randint(10000, 99999)),
    )
    db.add(detection)
    db.flush()
    return detection


def _entry(db, plate: str, **kwargs) -> models.WatchlistEntry:
    fields = {"priority": "CRITICAL", "reason": "lifecycle test", "active": True, **kwargs}
    entry = models.WatchlistEntry(entity_type="plate", identifier=plate, **fields)
    db.add(entry)
    db.flush()
    return entry


def _watchlisted_vehicle(db, plate: str) -> models.Vehicle:
    vehicle = models.Vehicle(plate_text=plate, plate_confidence=0.94, watchlist_flag=True)
    db.add(vehicle)
    db.flush()
    return vehicle


def _evaluate(db, camera, vehicle):
    return asyncio.run(rules_engine.evaluate(db, camera, _detection(db, camera), 100, 100, vehicle))


class TestDeactivationTakesEffect:
    def test_an_in_force_entry_still_fires(self, db_session):
        """The behaviour to keep, not the bug."""
        plate = _plate()
        _entry(db_session, plate)
        camera = _camera(db_session)
        assert len(_evaluate(db_session, camera, _watchlisted_vehicle(db_session, plate))) == 1

    def test_deactivating_the_entry_stops_the_alert(self, client, db_session, auth):
        plate = _plate()
        entry = _entry(db_session, plate)
        vehicle = _watchlisted_vehicle(db_session, plate)
        camera = _camera(db_session)
        db_session.commit()

        assert client.delete(f"/api/watchlists/{entry.id}", headers=auth).status_code == 200
        db_session.expire_all()
        vehicle = db_session.query(models.Vehicle).filter(models.Vehicle.id == vehicle.id).one()

        assert _evaluate(db_session, camera, vehicle) == [], (
            "a vehicle kept firing watchlist alerts after its only watchlist "
            "entry was deactivated"
        )

    def test_deactivating_the_entry_clears_the_cached_vehicle_flag(self, client, db_session, auth):
        """The flag drives snapshot capture and the badge on four pages, so
        stopping the alert isn't enough."""
        plate = _plate()
        entry = _entry(db_session, plate)
        vehicle = _watchlisted_vehicle(db_session, plate)
        db_session.commit()

        client.delete(f"/api/watchlists/{entry.id}", headers=auth)
        db_session.expire_all()

        assert db_session.query(models.Vehicle).filter(models.Vehicle.id == vehicle.id).one().watchlist_flag is False

    def test_a_second_in_force_entry_keeps_the_vehicle_flagged(self, client, db_session, auth):
        """Recomputed, not cleared: two agencies can list the same plate."""
        plate = _plate()
        first = _entry(db_session, plate, reason="agency A")
        _entry(db_session, plate, reason="agency B")
        vehicle = _watchlisted_vehicle(db_session, plate)
        db_session.commit()

        client.delete(f"/api/watchlists/{first.id}", headers=auth)
        db_session.expire_all()
        vehicle = db_session.query(models.Vehicle).filter(models.Vehicle.id == vehicle.id).one()

        assert vehicle.watchlist_flag is True
        assert len(_evaluate(db_session, _camera(db_session), vehicle)) == 1


class TestExpiry:
    def test_an_expired_entry_does_not_fire(self, db_session):
        plate = _plate()
        _entry(db_session, plate, valid_until=datetime.utcnow() - timedelta(hours=1))
        vehicle = _watchlisted_vehicle(db_session, plate)

        assert _evaluate(db_session, _camera(db_session), vehicle) == [], (
            "an entry past its valid_until still produced a watchlist alert"
        )

    def test_an_unexpired_entry_still_fires(self, db_session):
        plate = _plate()
        _entry(db_session, plate, valid_until=datetime.utcnow() + timedelta(hours=1))
        vehicle = _watchlisted_vehicle(db_session, plate)

        assert len(_evaluate(db_session, _camera(db_session), vehicle)) == 1

    def test_creating_an_in_force_entry_flags_an_existing_vehicle(self, client, db_session, auth):
        """The other direction. The create path's recompute first ran before
        the new entry was flushed (autoflush=False), found nothing and left
        an already-seen vehicle unflagged."""
        plate = _plate()
        vehicle = models.Vehicle(plate_text=plate, plate_confidence=0.9, watchlist_flag=False)
        db_session.add(vehicle)
        db_session.commit()

        resp = client.post(
            "/api/watchlists",
            json={"entity_type": "plate", "identifier": plate, "reason": "stolen", "priority": "CRITICAL"},
            headers=auth,
        )
        assert resp.status_code == 200
        db_session.expire_all()
        assert db_session.query(models.Vehicle).filter(models.Vehicle.id == vehicle.id).one().watchlist_flag is True

    def test_creating_an_already_expired_entry_does_not_flag_the_vehicle(self, client, db_session, auth):
        plate = _plate()
        vehicle = models.Vehicle(plate_text=plate, plate_confidence=0.9, watchlist_flag=False)
        db_session.add(vehicle)
        db_session.commit()

        resp = client.post(
            "/api/watchlists",
            json={
                "entity_type": "plate", "identifier": plate, "reason": "expired on arrival",
                "priority": "CRITICAL", "valid_until": (datetime.utcnow() - timedelta(days=1)).isoformat(),
            },
            headers=auth,
        )
        assert resp.status_code == 200
        db_session.expire_all()
        assert db_session.query(models.Vehicle).filter(models.Vehicle.id == vehicle.id).one().watchlist_flag is False

    def test_a_later_sighting_refreshes_a_stale_flag(self, db_session):
        """Nothing runs at expiry time, so the flag is fixed at the next
        sighting, when it's next used."""
        plate = _plate()
        _entry(db_session, plate, valid_until=datetime.utcnow() - timedelta(minutes=5))
        vehicle = _watchlisted_vehicle(db_session, plate)
        db_session.commit()

        asyncio.run(correlate.upsert_vehicle_for_plate(db_session, plate, 0.91))

        assert vehicle.watchlist_flag is False

    def test_a_sighting_of_an_in_force_plate_leaves_the_flag_set(self, db_session):
        plate = _plate()
        _entry(db_session, plate)
        vehicle = _watchlisted_vehicle(db_session, plate)
        db_session.commit()

        asyncio.run(correlate.upsert_vehicle_for_plate(db_session, plate, 0.91))

        assert vehicle.watchlist_flag is True


class TestListing:
    def test_a_deactivated_entry_is_not_listed_as_in_force(self, client, db_session, auth):
        plate = _plate()
        entry = _entry(db_session, plate)
        db_session.commit()
        client.delete(f"/api/watchlists/{entry.id}", headers=auth)

        listed = client.get("/api/watchlists?entity_type=plate", headers=auth).json()
        assert entry.id not in [e["id"] for e in listed], (
            "a deactivated entry was still presented on the watchlist page, "
            "indistinguishable from a live one"
        )

    def test_an_expired_entry_is_not_listed_as_in_force(self, client, db_session, auth):
        plate = _plate()
        entry = _entry(db_session, plate, valid_until=datetime.utcnow() - timedelta(days=2))
        db_session.commit()

        listed = client.get("/api/watchlists?entity_type=plate", headers=auth).json()
        assert entry.id not in [e["id"] for e in listed]

    def test_history_is_still_retrievable(self, client, db_session, auth):
        """Hidden from the in-force view, never deleted; the history stays."""
        plate = _plate()
        entry = _entry(db_session, plate)
        db_session.commit()
        client.delete(f"/api/watchlists/{entry.id}", headers=auth)

        listed = client.get("/api/watchlists?entity_type=plate&include_inactive=true", headers=auth).json()
        found = next((e for e in listed if e["id"] == entry.id), None)
        assert found is not None and found["active"] is False

    def test_an_in_force_entry_is_listed(self, client, db_session, auth):
        plate = _plate()
        entry = _entry(db_session, plate)
        db_session.commit()

        listed = client.get("/api/watchlists?entity_type=plate", headers=auth).json()
        assert entry.id in [e["id"] for e in listed]


class TestInvestigatorSummary:
    def test_the_summary_drops_the_match_once_deactivated(self, client, db_session, auth):
        """The summary's "watchlist match (CRITICAL)" has to be true when read."""
        plate = _plate()
        entry = _entry(db_session, plate)
        vehicle = _watchlisted_vehicle(db_session, plate)
        incident = models.Incident(title=f"lifecycle {plate}", status="open", priority="HIGH", vehicle_id=vehicle.id)
        db_session.add(incident)
        db_session.commit()

        before = client.get(f"/api/incidents/{incident.id}/summary", headers=auth).json()
        assert before["vehicle"]["watchlist_match"] is not None

        client.delete(f"/api/watchlists/{entry.id}", headers=auth)
        after = client.get(f"/api/incidents/{incident.id}/summary", headers=auth).json()

        assert after["vehicle"]["watchlist_match"] is None


class TestInForcePredicate:
    """The shared predicate, so a bare `active == True` somewhere new has
    something to be compared with."""

    def test_no_expiry_means_never_expires(self, db_session):
        plate = _plate()
        _entry(db_session, plate, valid_until=None)
        assert watchlist.plate_entry_in_force(db_session, plate) is not None

    def test_an_inactive_entry_is_not_in_force(self, db_session):
        plate = _plate()
        _entry(db_session, plate, active=False)
        assert watchlist.plate_entry_in_force(db_session, plate) is None

    def test_an_unknown_plate_has_no_entry(self, db_session):
        assert watchlist.plate_entry_in_force(db_session, _plate()) is None

    def test_no_plate_text_is_not_a_match(self, db_session):
        """A vehicle whose plate was never read must not match an entry."""
        assert watchlist.plate_entry_in_force(db_session, None) is None
        assert watchlist.plate_entry_in_force(db_session, "") is None
