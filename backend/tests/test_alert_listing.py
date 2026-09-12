"""The Alert Center's list endpoint.

Two defects in `app/routers/alerts.py` (52% covered):

1. One alert row with a NULL `reasons` column made the WHOLE list fail
   response validation — `ResponseValidationError: Input should be a valid
   list, input None` — so a single bad row blanked the Alert Center for every
   camera, not just its own line. The same nullable column already crashed the
   incident timeline; here it took the entire page with it.

2. There was no `camera_id` filter, yet the UI offered a per-camera view
   ("OPEN ALERTS" from the single-camera page). It filtered in the browser
   over whatever this endpoint had already truncated to its 200 most recent
   rows, so a camera whose alerts were not among the newest 200 system-wide
   showed an empty list — indistinguishable from a camera with no alerts.
"""
import uuid
from datetime import datetime, timedelta

import pytest

from app import models


@pytest.fixture
def auth(admin_token):
    return {"Authorization": f"Bearer {admin_token}"}


def _camera(db, prefix="ALR"):
    cam = models.Camera(
        camera_code=f"{prefix}-{uuid.uuid4().hex[:8]}", name="alert list cam",
        source_type="mock_vms", source_uri="",
    )
    db.add(cam)
    db.flush()
    return cam


class TestNullReasonsDoesNotBlankThePage:
    def test_one_row_with_null_reasons_does_not_break_the_list(self, client, auth, db_session):
        camera = _camera(db_session)
        db_session.add(models.Alert(camera_id=camera.id, severity="HIGH", reasons=None))
        db_session.commit()

        resp = client.get("/api/alerts", headers=auth)
        assert resp.status_code == 200, "a single NULL reasons row failed validation for the whole list"

    def test_null_reasons_is_reported_as_no_reasons(self, client, auth, db_session):
        camera = _camera(db_session)
        db_session.add(models.Alert(camera_id=camera.id, severity="HIGH", reasons=None))
        db_session.commit()

        rows = client.get(f"/api/alerts?camera_id={camera.id}", headers=auth).json()
        assert rows[0]["reasons"] == []

    def test_real_reasons_are_unchanged(self, client, auth, db_session):
        camera = _camera(db_session)
        db_session.add(models.Alert(camera_id=camera.id, severity="HIGH", reasons=["zone entry"]))
        db_session.commit()

        rows = client.get(f"/api/alerts?camera_id={camera.id}", headers=auth).json()
        assert rows[0]["reasons"] == ["zone entry"]


class TestCameraFilter:
    def test_alerts_are_scoped_to_the_camera(self, client, auth, db_session):
        mine = _camera(db_session)
        other = _camera(db_session)
        db_session.add(models.Alert(camera_id=mine.id, severity="HIGH", reasons=["mine"]))
        db_session.add(models.Alert(camera_id=other.id, severity="HIGH", reasons=["not mine"]))
        db_session.commit()

        rows = client.get(f"/api/alerts?camera_id={mine.id}", headers=auth).json()
        assert rows and all(r["camera_id"] == mine.id for r in rows)

    def test_an_older_alert_survives_the_200_row_limit(self, client, auth, db_session):
        """The exact failure: the camera's alert is not among the 200 most
        recent system-wide, so client-side filtering could never find it."""
        mine = _camera(db_session)
        noisy = _camera(db_session, prefix="NOISY")
        old = datetime.utcnow() - timedelta(days=30)
        db_session.add(models.Alert(camera_id=mine.id, severity="HIGH", reasons=["old but mine"], timestamp=old))
        for _ in range(220):
            db_session.add(models.Alert(camera_id=noisy.id, severity="LOW", reasons=["noise"]))
        db_session.commit()

        unscoped = client.get("/api/alerts", headers=auth).json()
        assert mine.id not in {r["camera_id"] for r in unscoped}, "precondition: the alert is past the 200-row cut"

        scoped = client.get(f"/api/alerts?camera_id={mine.id}", headers=auth).json()
        assert [r["reasons"] for r in scoped] == [["old but mine"]]

    def test_filters_combine(self, client, auth, db_session):
        camera = _camera(db_session)
        db_session.add(models.Alert(camera_id=camera.id, severity="CRITICAL", reasons=["c"]))
        db_session.add(models.Alert(camera_id=camera.id, severity="LOW", reasons=["l"]))
        db_session.commit()

        rows = client.get(f"/api/alerts?camera_id={camera.id}&severity=critical", headers=auth).json()
        assert [r["severity"] for r in rows] == ["CRITICAL"]

    def test_an_unknown_camera_returns_an_empty_list_not_an_error(self, client, auth):
        resp = client.get("/api/alerts?camera_id=no-such-camera", headers=auth)
        assert resp.status_code == 200 and resp.json() == []
