"""Watchlist alert severity follows how sure the plate read is.

rules_engine used to make every watchlist_flag hit CRITICAL, so a 48% read
alerted the same as a 94% one. The match still fires; only severity and
reason text change.
"""
import asyncio
import uuid

import pytest

from app import models
from app.config import settings
from app.pipeline import rules_engine


@pytest.fixture(autouse=True)
def _one_frame_zone_entry(monkeypatch):
    # these tests fire a zone alert from one tracked frame; the multi-frame
    # confirmation has its own tests in test_zone_alert_accuracy.py
    from app.config import settings as _settings
    monkeypatch.setattr(_settings, "zone_entry_min_frames", 1)


@pytest.fixture(autouse=True)
def _clean_cooldowns():
    rules_engine._alert_claims.clear()
    rules_engine._zone_presence.clear()
    yield
    rules_engine._alert_claims.clear()
    rules_engine._zone_presence.clear()


def _camera(db) -> models.Camera:
    camera = models.Camera(
        camera_code=f"WLC-{uuid.uuid4().hex[:8]}", name="watchlist confidence cam",
        source_type="video_file", source_uri="x.mp4",
    )
    db.add(camera)
    db.flush()
    return camera


def _watchlisted_vehicle(
    db, plate: str, plate_confidence: float, corroborated: bool = True,
) -> models.Vehicle:
    """corroborated defaults True so these isolate confidence; corroboration
    has its own tests in TestCorroborationGate."""
    db.add(models.WatchlistEntry(
        entity_type="plate", identifier=plate, priority="CRITICAL", active=True, reason="test",
    ))
    vehicle = models.Vehicle(
        plate_text=plate, plate_confidence=plate_confidence, watchlist_flag=True,
        plate_corroborated=corroborated,
    )
    db.add(vehicle)
    db.flush()
    return vehicle


def _detection(db, camera) -> models.Detection:
    detection = models.Detection(
        camera_id=camera.id, cls="car", confidence=0.88, bbox=[10.0, 10.0, 90.0, 90.0],
        track_id="9001", snapshot_path="/evidence/test.jpg",
    )
    db.add(detection)
    db.flush()
    return detection


def test_high_confidence_watchlist_match_is_critical(db_session):
    camera = _camera(db_session)
    vehicle = _watchlisted_vehicle(db_session, f"GJ05HC{uuid.uuid4().hex[:4].upper()}", plate_confidence=0.94)
    alerts = asyncio.run(rules_engine.evaluate(db_session, camera, _detection(db_session, camera), 100, 100, vehicle))

    assert len(alerts) == 1
    assert alerts[0].severity == "CRITICAL"
    assert "LOW CONFIDENCE" not in alerts[0].reasons[0]


def test_low_confidence_watchlist_match_is_capped_at_high_but_still_fires(db_session):
    camera = _camera(db_session)
    low_confidence = settings.watchlist_high_confidence_floor - 0.10
    assert low_confidence > settings.plate_min_confidence  # still a real passing read
    vehicle = _watchlisted_vehicle(db_session, f"GJ05LC{uuid.uuid4().hex[:4].upper()}", plate_confidence=low_confidence)
    alerts = asyncio.run(rules_engine.evaluate(db_session, camera, _detection(db_session, camera), 100, 100, vehicle))

    # still alerts
    assert len(alerts) == 1
    assert alerts[0].severity == "HIGH"
    assert "LOW CONFIDENCE" in alerts[0].reasons[0]
    assert "requires confirmation" in alerts[0].reasons[0]


def test_low_confidence_match_still_escalates_to_critical_via_other_signals(db_session):
    """The cap only limits the watchlist rule. A CRITICAL earned elsewhere in
    the same evaluation (say a restricted zone) still counts."""
    camera = _camera(db_session)
    zone = models.Zone(
        name="Secure Yard", camera_id=camera.id, x1=0.0, y1=0.0, x2=1.0, y2=1.0,
        active=True, severity="CRITICAL",
    )
    db_session.add(zone)
    db_session.flush()

    low_confidence = settings.watchlist_high_confidence_floor - 0.10
    vehicle = _watchlisted_vehicle(db_session, f"GJ05ZC{uuid.uuid4().hex[:4].upper()}", plate_confidence=low_confidence)
    alerts = asyncio.run(rules_engine.evaluate(db_session, camera, _detection(db_session, camera), 100, 100, vehicle))

    assert len(alerts) == 1
    # zone_entry's own CRITICAL severity still wins, independent of the watchlist cap.
    assert alerts[0].severity == "CRITICAL"
    assert any("LOW CONFIDENCE" in r for r in alerts[0].reasons)


class TestCorroborationGate:
    """CRITICAL needs the read corroborated across frames, not just confident.

    On the benchmark (docs/ANPR_ACCURACY.md, "A1") confidence doesn't
    separate right from wrong: correct 0.262-0.990, wrong plate-shaped
    0.260-0.956, 6 of 7 wrong reads at or above the lowest correct one, no
    threshold above 0.5 precision.

    UP84AE9889 read as UP81AE9889 at 0.956 would clear the 0.60 floor, raise
    CRITICAL and open an incident about a car that was never there.
    """

    def test_a_confident_but_uncorroborated_match_is_capped_at_high(self, db_session):
        """0.956 is the real confidence of a real misread in the corpus."""
        camera = _camera(db_session)
        vehicle = _watchlisted_vehicle(
            db_session, f"UP81AE{uuid.uuid4().hex[:4].upper()}",
            plate_confidence=0.956, corroborated=False,
        )
        detection = _detection(db_session, camera)

        alerts = asyncio.run(rules_engine.evaluate(db_session, camera, detection, 640, 480, vehicle))

        assert alerts, "an uncorroborated match must still FIRE — never silenced"
        alert = alerts[0]
        assert alert.severity == "HIGH", (
            "a single unconfirmed frame must not auto-escalate to CRITICAL, however "
            "confident that one read was"
        )
        assert any("UNCORROBORATED" in reason for reason in alert.reasons), (
            "the operator must be told WHY it was capped"
        )
        assert any("ONE frame" in reason for reason in alert.reasons)

    def test_a_confident_and_corroborated_match_is_critical(self, db_session):
        camera = _camera(db_session)
        vehicle = _watchlisted_vehicle(
            db_session, f"GJ05CR{uuid.uuid4().hex[:4].upper()}",
            plate_confidence=0.94, corroborated=True,
        )
        detection = _detection(db_session, camera)

        alerts = asyncio.run(rules_engine.evaluate(db_session, camera, detection, 640, 480, vehicle))
        assert alerts[0].severity == "CRITICAL"

    def test_corroboration_does_not_rescue_a_low_confidence_read(self, db_session):
        """Both gates apply; corroborating a barely-made read doesn't make it
        confident."""
        camera = _camera(db_session)
        vehicle = _watchlisted_vehicle(
            db_session, f"GJ05LC{uuid.uuid4().hex[:4].upper()}",
            plate_confidence=0.40, corroborated=True,
        )
        detection = _detection(db_session, camera)

        alerts = asyncio.run(rules_engine.evaluate(db_session, camera, detection, 640, 480, vehicle))
        assert alerts[0].severity == "HIGH"
        assert any("LOW CONFIDENCE" in reason for reason in alert_reasons(alerts[0]))

    def test_a_null_corroboration_flag_is_treated_as_not_corroborated(self, db_session):
        """Old rows and the legacy single-frame path have unknown provenance;
        unknown doesn't get CRITICAL."""
        camera = _camera(db_session)
        vehicle = _watchlisted_vehicle(
            db_session, f"GJ05NU{uuid.uuid4().hex[:4].upper()}", plate_confidence=0.99,
        )
        vehicle.plate_corroborated = None
        db_session.flush()
        detection = _detection(db_session, camera)

        alerts = asyncio.run(rules_engine.evaluate(db_session, camera, detection, 640, 480, vehicle))
        assert alerts[0].severity == "HIGH"

    def test_the_escape_hatch_restores_the_previous_behaviour(self, db_session, monkeypatch):
        """WATCHLIST_REQUIRE_CORROBORATION=false really goes back to
        confidence-only escalation."""
        monkeypatch.setattr(settings, "watchlist_require_corroboration", False)
        camera = _camera(db_session)
        vehicle = _watchlisted_vehicle(
            db_session, f"GJ05EH{uuid.uuid4().hex[:4].upper()}",
            plate_confidence=0.94, corroborated=False,
        )
        detection = _detection(db_session, camera)

        alerts = asyncio.run(rules_engine.evaluate(db_session, camera, detection, 640, 480, vehicle))
        assert alerts[0].severity == "CRITICAL"

    def test_an_uncorroborated_match_does_not_auto_open_an_incident(self, db_session):
        """One unconfirmed frame mustn't open an incident."""
        camera = _camera(db_session)
        vehicle = _watchlisted_vehicle(
            db_session, f"GJ05NI{uuid.uuid4().hex[:4].upper()}",
            plate_confidence=0.97, corroborated=False,
        )
        detection = _detection(db_session, camera)

        before = db_session.query(models.Incident).count()
        asyncio.run(rules_engine.evaluate(db_session, camera, detection, 640, 480, vehicle))
        db_session.flush()
        assert db_session.query(models.Incident).count() == before


def alert_reasons(alert) -> list:
    return list(alert.reasons or [])
