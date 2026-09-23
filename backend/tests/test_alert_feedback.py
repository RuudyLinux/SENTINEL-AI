"""False-positive feedback + precision metrics (10/10 roadmap P6)."""
import uuid

import pytest

from app import models
from app.routers.analytics import MIN_FEEDBACK_SAMPLE_SIZE


@pytest.fixture
def auth(admin_token):
    return {"Authorization": f"Bearer {admin_token}"}


def _make_alert(db) -> models.Alert:
    camera = models.Camera(camera_code=f"FB-{uuid.uuid4().hex[:8]}", name="feedback cam", source_type="video_file", source_uri="x.mp4")
    db.add(camera)
    db.flush()
    alert = models.Alert(camera_id=camera.id, severity="HIGH", reasons=["test"])
    db.add(alert)
    db.commit()
    db.refresh(alert)
    return alert


class TestSubmitFeedback:
    def test_confirmed_feedback_is_recorded_and_audited(self, client, db_session, auth):
        alert = _make_alert(db_session)
        resp = client.post(f"/api/alerts/{alert.id}/feedback", json={"feedback": "confirmed"}, headers=auth)
        assert resp.status_code == 200
        body = resp.json()
        assert body["feedback"] == "confirmed"
        assert body["feedback_at"] is not None

    def test_false_positive_feedback_accepts_a_reason(self, client, db_session, auth):
        alert = _make_alert(db_session)
        resp = client.post(
            f"/api/alerts/{alert.id}/feedback",
            json={"feedback": "false_positive", "reason": "misread plate — actually a dealer badge"},
            headers=auth,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["feedback"] == "false_positive"
        assert body["feedback_reason"] == "misread plate — actually a dealer badge"

    def test_invalid_feedback_value_is_rejected(self, client, db_session, auth):
        alert = _make_alert(db_session)
        resp = client.post(f"/api/alerts/{alert.id}/feedback", json={"feedback": "definitely_maybe"}, headers=auth)
        assert resp.status_code == 400

    def test_unauthenticated_request_is_rejected(self, client, db_session):
        alert = _make_alert(db_session)
        resp = client.post(f"/api/alerts/{alert.id}/feedback", json={"feedback": "confirmed"})
        assert resp.status_code == 401

    def test_unknown_alert_is_404(self, client, auth):
        resp = client.post("/api/alerts/alt_doesnotexist/feedback", json={"feedback": "confirmed"}, headers=auth)
        assert resp.status_code == 404


class TestAlertPrecisionAnalytics:
    def test_insufficient_sample_reports_no_rate(self, client, db_session, auth):
        alert = _make_alert(db_session)
        client.post(f"/api/alerts/{alert.id}/feedback", json={"feedback": "confirmed"}, headers=auth)
        body = client.get("/api/analytics/alert-precision", headers=auth).json()
        assert body["sample_sufficient"] is False
        assert body["precision"] is None
        assert "insufficient_sample" in body["note"]

    def test_sufficient_sample_computes_a_real_rate(self, client, db_session, auth):
        for _ in range(MIN_FEEDBACK_SAMPLE_SIZE - 4):
            a = _make_alert(db_session)
            client.post(f"/api/alerts/{a.id}/feedback", json={"feedback": "confirmed"}, headers=auth)
        for _ in range(4):
            a = _make_alert(db_session)
            client.post(f"/api/alerts/{a.id}/feedback", json={"feedback": "false_positive"}, headers=auth)

        body = client.get("/api/analytics/alert-precision", headers=auth).json()
        assert body["sample_sufficient"] is True
        assert body["reviewed_alerts"] >= MIN_FEEDBACK_SAMPLE_SIZE
        assert 0.0 <= body["precision"] <= 1.0
        assert 0.0 <= body["false_positive_rate"] <= 1.0
        assert round(body["precision"] + body["false_positive_rate"], 4) == 1.0
