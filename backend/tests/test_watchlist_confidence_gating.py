"""Confidence-aware intelligence (10/10 roadmap P5): a watchlist match must not
carry the same alert severity when the underlying plate read is uncertain.
Before this, `rules_engine.evaluate` set severity=CRITICAL for ANY
`vehicle.watchlist_flag`, regardless of `vehicle.plate_confidence` — a 48%
guess produced an identical alert to a 94% confident read. The match itself
must still fire (never silence a real watchlist hit for being uncertain);
only its severity and reason string change.
"""
import asyncio
import uuid

import pytest

from app import models
from app.config import settings
from app.pipeline import rules_engine


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
    """`corroborated` defaults True so the tests below isolate the CONFIDENCE
    dimension, which is what this module is about. Corroboration is a second,
    independent gate with its own tests in `TestCorroborationGate`."""
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
    assert low_confidence > settings.plate_min_confidence  # still a real, gate-passing read — not garbage
    vehicle = _watchlisted_vehicle(db_session, f"GJ05LC{uuid.uuid4().hex[:4].upper()}", plate_confidence=low_confidence)
    alerts = asyncio.run(rules_engine.evaluate(db_session, camera, _detection(db_session, camera), 100, 100, vehicle))

    # Never silenced — a real watchlist hit still produces an alert.
    assert len(alerts) == 1
    assert alerts[0].severity == "HIGH"
    assert "LOW CONFIDENCE" in alerts[0].reasons[0]
    assert "requires confirmation" in alerts[0].reasons[0]


def test_low_confidence_match_still_escalates_to_critical_via_other_signals(db_session):
    """The confidence cap only limits the WATCHLIST rule's own contribution.
    A separately-earned CRITICAL (e.g. a restricted zone entered in the same
    evaluation) must still be able to raise the final severity — the risk
    score is a floor-raising mechanism, never suppressed by this cap."""
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
    """A1 precision hardening (2026-09-12): a watchlist match may only reach
    CRITICAL if the plate read was corroborated ACROSS FRAMES, not merely
    confident.

    Why this gate exists, measured rather than assumed (docs/ANPR_ACCURACY.md,
    "A1"): on the labelled benchmark, OCR confidence does NOT separate correct
    reads from wrong ones. Correct reads span 0.262-0.990; wrong plate-shaped
    reads span 0.260-0.956, and SIX OF SEVEN wrong reads sit at or above the
    lowest correct read's confidence. No confidence threshold on that corpus
    reaches precision above 0.5.

    The concrete failure: `UP84AE9889` was misread as `UP81AE9889` at 0.956.
    Under a confidence-only gate that single frame clears the 0.60 floor, raises
    a CRITICAL alert and auto-opens an incident naming a vehicle that was never
    there.
    """

    def test_a_confident_but_uncorroborated_match_is_capped_at_high(self, db_session):
        """The regression test for the defect. 0.956 is the real confidence of a
        real misread in the benchmark corpus."""
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
        """The two gates are independent and BOTH must pass. Corroborating a
        read the engine barely made does not make it confident."""
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
        """Rows predating the column, and the legacy single-frame ANPR path, have
        genuinely unknown provenance. The safe default for a missing safety
        signal is 'not satisfied' — unknown must not buy CRITICAL."""
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
        """WATCHLIST_REQUIRE_CORROBORATION=false is a real revert, not
        decoration: one env var returns confidence-only escalation."""
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
        """The operational point of the whole change. An incident is a real
        investigative artefact; one unconfirmed frame must not create one."""
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
