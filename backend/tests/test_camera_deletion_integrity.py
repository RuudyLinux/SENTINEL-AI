"""DELETE /api/cameras/{id} used to return 200 and orphan everything that
referenced the camera. On one probe camera:

    orphan detections: 1   orphan alerts:   1   orphan incidents: 1
    orphan evidence:   1   orphan plates:   1   orphan zones:     1

The evidence row had its capture digest and belonged to an OPEN incident.
docs/PRIVACY_GOVERNANCE.md says evidence is never silently destroyed.

And the backends disagreed: SQLite without foreign_keys=ON let it through,
PostgreSQL would raise. The guard gives both the same 409.
"""
import uuid

import pytest

from app import models
from app.db import SessionLocal


@pytest.fixture
def auth(admin_token):
    return {"Authorization": f"Bearer {admin_token}"}


def _camera(db) -> models.Camera:
    cam = models.Camera(
        camera_code=f"DEL-{uuid.uuid4().hex[:8]}", name="deletion integrity probe",
        source_type="video_file", source_uri="x.mp4",
    )
    db.add(cam)
    db.commit()
    db.refresh(cam)
    return cam


class TestDeletionRefusedWhileHistoryExists:
    def test_a_camera_with_evidence_and_an_incident_cannot_be_deleted(self, client, db_session, auth):
        cam = _camera(db_session)
        det = models.Detection(camera_id=cam.id, cls="car", confidence=0.9, bbox=[1, 2, 3, 4])
        db_session.add(det)
        db_session.flush()
        alert = models.Alert(camera_id=cam.id, severity="CRITICAL", detection_id=det.id, reasons=["probe"])
        db_session.add(alert)
        db_session.flush()
        incident = models.Incident(title="probe incident", camera_id=cam.id, alert_id=alert.id, status="open")
        db_session.add(incident)
        db_session.flush()
        evidence = models.Evidence(
            incident_id=incident.id, camera_id=cam.id, alert_id=alert.id, detection_id=det.id,
            evidence_type="snapshot", sha256="deadbeef" * 8,
        )
        db_session.add(evidence)
        db_session.commit()

        resp = client.delete(f"/api/cameras/{cam.id}", headers=auth)
        assert resp.status_code == 409, "a camera holding evidence/incident history must not be deletable"
        detail = resp.json()["detail"]
        # The operator must be told what is actually in the way.
        assert "evidence" in detail and "incidents" in detail

        verifier = SessionLocal()
        try:
            # Nothing was orphaned, and the camera itself survives.
            assert verifier.query(models.Camera).filter(models.Camera.id == cam.id).count() == 1
            assert verifier.query(models.Evidence).filter(models.Evidence.id == evidence.id).count() == 1
            assert verifier.query(models.Incident).filter(models.Incident.id == incident.id).count() == 1
        finally:
            verifier.close()

    @pytest.mark.parametrize("dependent", ["detection", "plate", "zone"])
    def test_any_dependent_record_blocks_deletion(self, client, db_session, auth, dependent):
        """Every referencing table blocks, or the orphan just moves to the
        one that was forgotten."""
        cam = _camera(db_session)
        if dependent == "detection":
            db_session.add(models.Detection(camera_id=cam.id, cls="car", confidence=0.5, bbox=[0, 0, 1, 1]))
        elif dependent == "plate":
            db_session.add(models.Plate(camera_id=cam.id, plate_text_normalized="GJ01DEL001", confidence=0.8))
        else:
            db_session.add(models.Zone(name="probe zone", camera_id=cam.id))
        db_session.commit()

        resp = client.delete(f"/api/cameras/{cam.id}", headers=auth)
        assert resp.status_code == 409
        assert db_session.query(models.Camera).filter(models.Camera.id == cam.id).count() == 1


class TestDeletionStillWorksWhenClean:
    def test_a_camera_with_no_history_is_still_deletable(self, client, db_session, auth):
        """A new camera with no history still deletes."""
        cam = _camera(db_session)
        cam_id = cam.id

        resp = client.delete(f"/api/cameras/{cam_id}", headers=auth)
        assert resp.status_code == 200

        verifier = SessionLocal()
        try:
            assert verifier.query(models.Camera).filter(models.Camera.id == cam_id).count() == 0
        finally:
            verifier.close()

    def test_deleting_an_unknown_camera_is_still_404(self, client, auth):
        assert client.delete("/api/cameras/cam_doesnotexist", headers=auth).status_code == 404

    def test_deletion_requires_administrator(self, client, db_session):
        cam = _camera(db_session)
        assert client.delete(f"/api/cameras/{cam.id}").status_code == 401
