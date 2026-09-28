"""Foreign keys enforced on SQLite, and demo reset working with them on.

SQLite ignores foreign keys unless PRAGMA foreign_keys=ON per connection,
while PostgreSQL always enforced them, so invalid references passed in dev
and failed in production where no SQLite test could see it.

Turning it on found a real bug straight away: seed.reset_demo_data (POST
/api/system/demo/reset) deleted incidents and alerts before IncidentAlert,
which points at both. On PostgreSQL that's a ForeignKeyViolation, so demo
reset was broken in production.

Pinned: the PRAGMA stays on, the constraints actually bite, demo reset works.
"""
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app import models
from app.config import settings
from app.db import SessionLocal


class TestForeignKeysAreEnforced:
    def test_the_pragma_is_on_for_every_connection(self):
        """Per connection: a pooled connection without the PRAGMA would quietly
        stop enforcing."""
        for _ in range(3):
            session = SessionLocal()
            try:
                assert session.execute(text("PRAGMA foreign_keys")).scalar() == 1
            finally:
                session.close()

    def test_inserting_a_row_referencing_a_nonexistent_camera_is_rejected(self):
        """It actually bites, not just reports 1."""
        session = SessionLocal()
        try:
            session.add(models.Detection(
                camera_id="cam_does_not_exist", cls="car", confidence=0.9, bbox=[0, 0, 1, 1],
            ))
            with pytest.raises(IntegrityError):
                session.commit()
        finally:
            session.rollback()
            session.close()

    def test_deleting_a_referenced_row_is_rejected(self):
        """A parent with children can't vanish and leave them dangling."""
        session = SessionLocal()
        try:
            camera = models.Camera(
                camera_code=f"FK-{uuid.uuid4().hex[:8]}", name="fk probe",
                source_type="video_file", source_uri="x.mp4",
            )
            session.add(camera)
            session.flush()
            session.add(models.Detection(camera_id=camera.id, cls="car", confidence=0.9, bbox=[0, 0, 1, 1]))
            session.commit()

            session.delete(camera)
            with pytest.raises(IntegrityError):
                session.commit()
        finally:
            session.rollback()
            session.close()


class TestDemoResetIsReferentiallyValid:
    def test_reset_demo_data_succeeds_with_a_fully_linked_incident(self, db_session):
        """Incident with an IncidentAlert link, evidence and a note, then
        reset. Used to fail "FOREIGN KEY constraint failed" on DELETE FROM
        incidents, a 500 on PostgreSQL."""
        from app.seed import reset_demo_data

        assert settings.demo_mode, "this test requires DEMO_MODE (conftest sets it)"

        camera = models.Camera(
            camera_code=f"RST-{uuid.uuid4().hex[:8]}", name="reset probe",
            source_type="video_file", source_uri="x.mp4",
        )
        db_session.add(camera)
        db_session.flush()
        detection = models.Detection(camera_id=camera.id, cls="car", confidence=0.9, bbox=[0, 0, 1, 1])
        db_session.add(detection)
        db_session.flush()
        vehicle = models.Vehicle(plate_text=f"GJ05RS{uuid.uuid4().hex[:4].upper()}", plate_confidence=0.9)
        db_session.add(vehicle)
        db_session.flush()
        alert = models.Alert(
            camera_id=camera.id, severity="CRITICAL", detection_id=detection.id,
            vehicle_id=vehicle.id, reasons=["reset probe"],
        )
        db_session.add(alert)
        db_session.flush()
        incident = models.Incident(
            title="reset probe incident", camera_id=camera.id, alert_id=alert.id, vehicle_id=vehicle.id,
        )
        db_session.add(incident)
        db_session.flush()
        # The row that was missing from the wipe list.
        db_session.add(models.IncidentAlert(
            incident_id=incident.id, alert_id=alert.id, correlation_reason="reset probe",
        ))
        db_session.add(models.IncidentNote(incident_id=incident.id, text="probe note"))
        db_session.add(models.Evidence(
            incident_id=incident.id, camera_id=camera.id, alert_id=alert.id,
            detection_id=detection.id, evidence_type="snapshot", sha256="ab" * 32,
        ))
        db_session.commit()

        summary = reset_demo_data(db_session)  # must not raise
        assert "cameras" in summary

        # The transactional data really is gone, links included.
        assert db_session.query(models.IncidentAlert).count() == 0
        assert db_session.query(models.Incident).count() == 0
        assert db_session.query(models.Alert).count() == 0
        assert db_session.query(models.Evidence).count() == 0
