"""Incident Investigator Summary (10/10 roadmap P10):
GET /api/incidents/{id}/summary answers what/why/where/evidence/confidence
in one call, from data the platform already computed elsewhere."""
import asyncio
import uuid

import pytest

from app import models
from app.pipeline import rules_engine


@pytest.fixture
def auth(admin_token):
    return {"Authorization": f"Bearer {admin_token}"}


@pytest.fixture(autouse=True)
def _clean_cooldowns():
    rules_engine._last_alert_at.clear()
    rules_engine._zone_presence.clear()
    yield
    rules_engine._last_alert_at.clear()
    rules_engine._zone_presence.clear()


def _watchlisted_incident(db):
    camera = models.Camera(camera_code=f"SUM-{uuid.uuid4().hex[:8]}", name="summary cam", source_type="video_file", source_uri="x.mp4")
    db.add(camera)
    db.flush()
    plate = f"GJ05SU{uuid.uuid4().hex[:4].upper()}"
    db.add(models.WatchlistEntry(entity_type="plate", identifier=plate, priority="CRITICAL", active=True, reason="stolen vehicle"))
    vehicle = models.Vehicle(
        plate_text=plate, plate_confidence=0.93, watchlist_flag=True,
        plate_corroborated=True,  # CRITICAL now needs corroboration as well as confidence
    )
    db.add(vehicle)
    db.flush()
    detection = models.Detection(
        camera_id=camera.id, cls="car", confidence=0.9, bbox=[1, 1, 50, 50],
        track_id="7001", snapshot_path="/evidence/summary-test.jpg",
    )
    db.add(detection)
    db.flush()
    alerts = asyncio.run(rules_engine.evaluate(db, camera, detection, 100, 100, vehicle))
    db.commit()
    incident = rules_engine.find_incident_for_alert(db, alerts[0].id)
    return incident, vehicle, alerts[0]


class TestIncidentSummary:
    def test_summary_reports_risk_vehicle_and_reasons(self, client, db_session, auth):
        incident, vehicle, alert = _watchlisted_incident(db_session)
        resp = client.get(f"/api/incidents/{incident.id}/summary", headers=auth)
        assert resp.status_code == 200
        body = resp.json()

        assert body["incident_id"] == incident.id
        assert body["risk"]["score"] == alert.risk_score
        assert body["risk"]["factors"] == alert.risk_factors
        assert any("Watchlist" in r for r in body["why"])
        assert body["vehicle"]["plate_text"] == vehicle.plate_text
        assert body["vehicle"]["watchlist_match"]["priority"] == "CRITICAL"

    def test_summary_lists_related_alerts_with_feedback_field(self, client, db_session, auth):
        incident, vehicle, alert = _watchlisted_incident(db_session)
        resp = client.get(f"/api/incidents/{incident.id}/summary", headers=auth)
        related = resp.json()["related_alerts"]
        assert len(related) == 1
        assert related[0]["id"] == alert.id
        assert related[0]["feedback"] is None  # not yet reviewed

    def test_summary_reports_evidence_and_verification_state(self, client, db_session, auth):
        incident, vehicle, alert = _watchlisted_incident(db_session)
        resp = client.get(f"/api/incidents/{incident.id}/summary", headers=auth)
        body = resp.json()
        assert len(body["evidence"]) >= 1
        # Freshly captured, never verified yet — must not be claimed VERIFIED.
        assert body["evidence_fully_verified"] is False

    def test_unknown_incident_is_404(self, client, auth):
        resp = client.get("/api/incidents/inc_doesnotexist/summary", headers=auth)
        assert resp.status_code == 404

    def test_unauthenticated_request_is_rejected(self, client, db_session):
        incident, _, _ = _watchlisted_incident(db_session)
        assert client.get(f"/api/incidents/{incident.id}/summary").status_code == 401
