"""V2 Phase 3 — event correlation and risk-scored alerts.

Pre-V2, every CRITICAL alert opened its own incident, so one real event — a
watchlisted vehicle entering a restricted zone and then crossing three more
cameras — became four incidents the operator had to reassemble by eye. These
tests lock down the correlation rules AND, just as importantly, the limits on
them: correlation must never merge two genuinely different events.
"""
import asyncio
import uuid
from datetime import datetime, timedelta

import pytest

from app import models
from app.config import settings
from app.pipeline import rules_engine


@pytest.fixture(autouse=True)
def _clean_cooldowns():
    # Module-level dicts shared across the whole test session otherwise.
    rules_engine._alert_claims.clear()
    rules_engine._zone_presence.clear()
    yield
    rules_engine._alert_claims.clear()
    rules_engine._zone_presence.clear()


def _camera(db, code: str | None = None) -> models.Camera:
    camera = models.Camera(
        camera_code=code or f"COR-{uuid.uuid4().hex[:8]}", name="correlation cam",
        location="Test Junction", source_type="video_file", source_uri="x.mp4",
    )
    db.add(camera)
    db.flush()
    return camera


def _watchlisted_vehicle(db, plate: str, priority: str = "CRITICAL") -> models.Vehicle:
    db.add(models.WatchlistEntry(
        entity_type="plate", identifier=plate, priority=priority, active=True, reason="test",
    ))
    vehicle = models.Vehicle(
        plate_text=plate, plate_confidence=0.94, watchlist_flag=True,
        # A CRITICAL watchlist escalation now requires BOTH a confident read and
        # corroboration across frames. This fixture models a well-identified vehicle,
        # so it sets both; see rules_engine._plate_is_corroborated.
        plate_corroborated=True,
    )
    db.add(vehicle)
    db.flush()
    return vehicle


def _detection(db, camera, cls: str = "car") -> models.Detection:
    detection = models.Detection(
        camera_id=camera.id, cls=cls, confidence=0.88, bbox=[10.0, 10.0, 90.0, 90.0],
        track_id="284", snapshot_path="/evidence/test.jpg",
    )
    db.add(detection)
    db.flush()
    return detection


def _evaluate(db, camera, detection, vehicle):
    return asyncio.run(rules_engine.evaluate(db, camera, detection, 100, 100, vehicle))


class TestRiskScoredAlerts:
    def test_a_watchlist_alert_carries_a_score_and_its_factors(self, db_session):
        camera = _camera(db_session)
        vehicle = _watchlisted_vehicle(db_session, f"GJ05CR{uuid.uuid4().hex[:4].upper()}")
        alerts = _evaluate(db_session, camera, _detection(db_session, camera), vehicle)

        assert len(alerts) == 1
        alert = alerts[0]
        assert alert.risk_score > 0
        assert alert.risk_factors, "an alert with a score must say what produced it"
        assert alert.risk_score == sum(f["points"] for f in alert.risk_factors)
        assert "watchlist_match" in [f["factor"] for f in alert.risk_factors]

    def test_the_rule_severity_is_a_floor_the_score_cannot_lower(self, db_session):
        """A scoring change must never make an explicit rule matter less than
        it did before. A watchlist match stays CRITICAL even though the score
        alone would band lower."""
        camera = _camera(db_session)
        vehicle = _watchlisted_vehicle(db_session, f"GJ05FL{uuid.uuid4().hex[:4].upper()}", priority="LOW")
        alert = _evaluate(db_session, camera, _detection(db_session, camera), vehicle)[0]

        assert alert.severity == "CRITICAL"
        assert alert.risk_score < 75, "precondition: the score alone would not band CRITICAL"

    def test_the_reasons_narrative_is_preserved_alongside_the_score(self, db_session):
        """`reasons` says WHAT matched, `risk_factors` says how much each
        mattered. V2 adds the second; it must not replace the first."""
        camera = _camera(db_session)
        vehicle = _watchlisted_vehicle(db_session, f"GJ05RN{uuid.uuid4().hex[:4].upper()}")
        alert = _evaluate(db_session, camera, _detection(db_session, camera), vehicle)[0]

        assert alert.reasons
        assert any("Watchlist signal" in r for r in alert.reasons)


class TestIncidentCorrelation:
    def test_a_second_alert_for_the_same_vehicle_joins_the_same_incident(self, db_session):
        """The headline behavior: one event, one incident, many alerts."""
        camera_a = _camera(db_session)
        camera_b = _camera(db_session)
        vehicle = _watchlisted_vehicle(db_session, f"GJ05CC{uuid.uuid4().hex[:4].upper()}")

        first = _evaluate(db_session, camera_a, _detection(db_session, camera_a), vehicle)[0]
        # Cleared so the cooldown (a separate concern) does not suppress the
        # second alert — this test is about correlation, not dedup.
        rules_engine._alert_claims.clear()
        second = _evaluate(db_session, camera_b, _detection(db_session, camera_b), vehicle)[0]

        incidents = db_session.query(models.Incident).filter(models.Incident.vehicle_id == vehicle.id).all()
        assert len(incidents) == 1, "a vehicle crossing two cameras is one event, not two incidents"
        assert rules_engine.find_incident_for_alert(db_session, first.id).id == incidents[0].id
        assert rules_engine.find_incident_for_alert(db_session, second.id).id == incidents[0].id

    def test_every_alert_is_linked_including_the_one_that_opened_the_incident(self, db_session):
        """A caller must never have to check two relationships to enumerate an
        incident's alerts."""
        camera = _camera(db_session)
        vehicle = _watchlisted_vehicle(db_session, f"GJ05LK{uuid.uuid4().hex[:4].upper()}")
        first = _evaluate(db_session, camera, _detection(db_session, camera), vehicle)[0]
        rules_engine._alert_claims.clear()
        second = _evaluate(db_session, camera, _detection(db_session, camera), vehicle)[0]

        incident = rules_engine.find_incident_for_alert(db_session, first.id)
        linked = db_session.query(models.IncidentAlert).filter(
            models.IncidentAlert.incident_id == incident.id
        ).all()
        assert {row.alert_id for row in linked} == {first.id, second.id}
        assert all(row.correlation_reason for row in linked), "a link must justify itself"

    def test_the_correlated_incident_describes_the_whole_event(self, db_session):
        camera_a = _camera(db_session)
        camera_b = _camera(db_session)
        plate = f"GJ05DS{uuid.uuid4().hex[:4].upper()}"
        vehicle = _watchlisted_vehicle(db_session, plate)

        _evaluate(db_session, camera_a, _detection(db_session, camera_a), vehicle)
        rules_engine._alert_claims.clear()
        second = _evaluate(db_session, camera_b, _detection(db_session, camera_b), vehicle)[0]

        incident = rules_engine.find_incident_for_alert(db_session, second.id)
        assert plate in incident.title
        assert "2 correlated alerts" in incident.title
        assert "2 camera(s)" in incident.title

    def test_a_different_vehicle_gets_its_own_incident(self, db_session):
        """Correlation must not merge on proximity or similar behavior — two
        different vehicles doing similar things are two events."""
        camera = _camera(db_session)
        vehicle_a = _watchlisted_vehicle(db_session, f"GJ05AA{uuid.uuid4().hex[:4].upper()}")
        vehicle_b = _watchlisted_vehicle(db_session, f"GJ05BB{uuid.uuid4().hex[:4].upper()}")

        first = _evaluate(db_session, camera, _detection(db_session, camera), vehicle_a)[0]
        rules_engine._alert_claims.clear()
        second = _evaluate(db_session, camera, _detection(db_session, camera), vehicle_b)[0]

        assert rules_engine.find_incident_for_alert(db_session, first.id).id != \
            rules_engine.find_incident_for_alert(db_session, second.id).id

    def test_an_alert_outside_the_window_opens_a_new_incident(self, db_session, monkeypatch):
        """A vehicle returning hours later is a genuinely separate event."""
        monkeypatch.setattr(settings, "incident_correlation_window_seconds", 0.0)
        camera = _camera(db_session)
        vehicle = _watchlisted_vehicle(db_session, f"GJ05WN{uuid.uuid4().hex[:4].upper()}")

        first = _evaluate(db_session, camera, _detection(db_session, camera), vehicle)[0]
        # Age the existing incident well past the (now zero-length) window.
        incident = rules_engine.find_incident_for_alert(db_session, first.id)
        incident.created_at = datetime.utcnow() - timedelta(hours=3)
        db_session.commit()
        rules_engine._alert_claims.clear()
        second = _evaluate(db_session, camera, _detection(db_session, camera), vehicle)[0]

        assert rules_engine.find_incident_for_alert(db_session, second.id).id != incident.id

    def test_a_closed_incident_is_never_reopened_by_correlation(self, db_session):
        """An operator who closed an incident has made a judgement. A new alert
        must raise a new incident, not silently resurrect the closed one."""
        camera = _camera(db_session)
        vehicle = _watchlisted_vehicle(db_session, f"GJ05CL{uuid.uuid4().hex[:4].upper()}")

        first = _evaluate(db_session, camera, _detection(db_session, camera), vehicle)[0]
        incident = rules_engine.find_incident_for_alert(db_session, first.id)
        incident.status = "closed"
        db_session.commit()
        rules_engine._alert_claims.clear()
        second = _evaluate(db_session, camera, _detection(db_session, camera), vehicle)[0]

        second_incident = rules_engine.find_incident_for_alert(db_session, second.id)
        assert second_incident.id != incident.id
        assert incident.status == "closed"


class TestFindIncidentForAlert:
    def test_resolves_an_incident_linked_only_by_the_original_fk(self, db_session):
        """Incidents created before V2 (and any created directly through the
        incidents API) have no IncidentAlert row — the lookup must still find
        them via Incident.alert_id."""
        camera = _camera(db_session)
        alert = models.Alert(camera_id=camera.id, severity="HIGH", reasons=["legacy"])
        db_session.add(alert)
        db_session.flush()
        incident = models.Incident(title="legacy incident", alert_id=alert.id, camera_id=camera.id)
        db_session.add(incident)
        db_session.commit()

        assert rules_engine.find_incident_for_alert(db_session, alert.id).id == incident.id

    def test_returns_none_for_an_alert_with_no_incident(self, db_session):
        camera = _camera(db_session)
        alert = models.Alert(camera_id=camera.id, severity="LOW", reasons=["nothing"])
        db_session.add(alert)
        db_session.commit()

        assert rules_engine.find_incident_for_alert(db_session, alert.id) is None
