"""Privacy / governance controls (10/10 roadmap P13): configurable retention,
dry-run-by-default purge, and the double-confirmation required for a real,
irreversible deletion."""
import uuid
from datetime import datetime, timedelta

import pytest

from app import models
from app.config import settings


@pytest.fixture
def auth(admin_token):
    return {"Authorization": f"Bearer {admin_token}"}


def _old_evidence(db, tmp_path, days_old: int) -> models.Evidence:
    camera = models.Camera(camera_code=f"GOV-{uuid.uuid4().hex[:8]}", name="gov cam", source_type="video_file", source_uri="x.mp4")
    db.add(camera)
    db.flush()
    f = tmp_path / f"{uuid.uuid4().hex}.jpg"
    f.write_bytes(b"fake evidence bytes")
    evidence = models.Evidence(
        camera_id=camera.id, evidence_type="snapshot", file_path=str(f),
        verification_status="unverified", created_at=datetime.utcnow() - timedelta(days=days_old),
    )
    db.add(evidence)
    db.commit()
    db.refresh(evidence)
    return evidence


@pytest.fixture(autouse=True)
def _restore_retention_setting():
    original = settings.evidence_retention_days
    yield
    settings.evidence_retention_days = original


class TestRetentionPolicy:
    def test_unconfigured_by_default(self, client, auth):
        settings.evidence_retention_days = None
        body = client.get("/api/governance/retention-policy", headers=auth).json()
        assert body["configured"] is False
        assert body["eligible_for_purge"] == 0

    def test_counts_eligible_evidence_once_configured(self, client, db_session, auth, tmp_path):
        settings.evidence_retention_days = 30
        _old_evidence(db_session, tmp_path, days_old=45)
        _old_evidence(db_session, tmp_path, days_old=5)  # too recent to be eligible
        body = client.get("/api/governance/retention-policy", headers=auth).json()
        assert body["configured"] is True
        assert body["eligible_for_purge"] >= 1


class TestPurgeExpired:
    def test_purge_without_retention_configured_is_rejected(self, client, auth):
        settings.evidence_retention_days = None
        resp = client.post("/api/governance/purge-expired", json={"dry_run": True}, headers=auth)
        assert resp.status_code == 400

    def test_default_is_dry_run_and_deletes_nothing(self, client, db_session, auth, tmp_path):
        settings.evidence_retention_days = 30
        old = _old_evidence(db_session, tmp_path, days_old=90)
        resp = client.post("/api/governance/purge-expired", json={}, headers=auth)
        assert resp.status_code == 200
        body = resp.json()
        assert body["dry_run"] is True
        assert body["deleted_count"] == 0
        assert old.id in body["eligible_ids"]
        # Row genuinely still exists.
        assert db_session.query(models.Evidence).filter(models.Evidence.id == old.id).first() is not None

    def test_dry_run_false_without_confirm_still_does_not_delete(self, client, db_session, auth, tmp_path):
        settings.evidence_retention_days = 30
        old = _old_evidence(db_session, tmp_path, days_old=90)
        resp = client.post("/api/governance/purge-expired", json={"dry_run": False, "confirm": False}, headers=auth)
        assert resp.json()["deleted_count"] == 0
        assert db_session.query(models.Evidence).filter(models.Evidence.id == old.id).first() is not None

    def test_dry_run_false_and_confirm_true_actually_deletes(self, client, db_session, auth, tmp_path):
        settings.evidence_retention_days = 30
        old = _old_evidence(db_session, tmp_path, days_old=90)
        file_path = old.file_path
        resp = client.post("/api/governance/purge-expired", json={"dry_run": False, "confirm": True}, headers=auth)
        assert resp.status_code == 200
        body = resp.json()
        assert body["deleted_count"] >= 1
        assert old.id in body["eligible_ids"]
        assert db_session.query(models.Evidence).filter(models.Evidence.id == old.id).first() is None
        import os
        assert not os.path.exists(file_path)

    def test_recent_evidence_is_never_purged(self, client, db_session, auth, tmp_path):
        settings.evidence_retention_days = 30
        recent = _old_evidence(db_session, tmp_path, days_old=1)
        resp = client.post("/api/governance/purge-expired", json={"dry_run": False, "confirm": True}, headers=auth)
        assert recent.id not in resp.json()["eligible_ids"]
        assert db_session.query(models.Evidence).filter(models.Evidence.id == recent.id).first() is not None
