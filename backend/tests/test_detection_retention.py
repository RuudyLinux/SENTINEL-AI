"""Detection retention (opt-in): old raw detections can be purged, but never one
that an alert, a plate read or an evidence item still points at."""
import uuid
from datetime import datetime, timedelta

import pytest

from app import models
from app.config import settings


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def setup(db_session):
    cam = models.Camera(camera_code=f"C-RETN-{uuid.uuid4().hex[:6]}", name="r", source_type="mock_vms", source_uri="")
    db_session.add(cam)
    db_session.flush()
    old = datetime.utcnow() - timedelta(days=40)

    def det(ts):
        d = models.Detection(camera_id=cam.id, cls="car", confidence=0.9, bbox=[1, 1, 2, 2], timestamp=ts)
        db_session.add(d)
        db_session.flush()
        return d

    rows = {
        "old_plain": det(old),
        "old_alerted": det(old),
        "old_plated": det(old),
        "old_evidenced": det(old),
        "recent": det(datetime.utcnow()),
    }
    db_session.add(models.Alert(camera_id=cam.id, severity="HIGH", detection_id=rows["old_alerted"].id, reasons=["x"]))
    db_session.add(models.Plate(camera_id=cam.id, detection_id=rows["old_plated"].id, plate_text_raw="x",
                                plate_text_normalized=f"QA{uuid.uuid4().hex[:6]}", confidence=0.9))
    db_session.add(models.Evidence(camera_id=cam.id, detection_id=rows["old_evidenced"].id,
                                   evidence_type="snapshot", file_path=""))
    db_session.commit()
    return {k: v.id for k, v in rows.items()}


def test_disabled_by_default(client, admin_token, monkeypatch):
    monkeypatch.setattr(settings, "detection_retention_days", None)
    resp = client.post("/api/governance/purge-detections", json={}, headers=_auth(admin_token))
    assert resp.status_code == 400


def test_dry_run_deletes_nothing(client, admin_token, db_session, setup, monkeypatch):
    monkeypatch.setattr(settings, "detection_retention_days", 30)
    body = client.post("/api/governance/purge-detections", json={"dry_run": True}, headers=_auth(admin_token)).json()
    assert body["dry_run"] is True and body["deleted_count"] == 0 and body["eligible_count"] >= 1
    db_session.expire_all()
    assert db_session.get(models.Detection, setup["old_plain"]) is not None


def test_purge_keeps_every_referenced_and_recent_detection(client, admin_token, db_session, setup, monkeypatch):
    monkeypatch.setattr(settings, "detection_retention_days", 30)
    body = client.post(
        "/api/governance/purge-detections", json={"dry_run": False, "confirm": True}, headers=_auth(admin_token),
    ).json()
    assert body["deleted_count"] >= 1
    db_session.expire_all()
    assert db_session.get(models.Detection, setup["old_plain"]) is None
    for kept in ("old_alerted", "old_plated", "old_evidenced", "recent"):
        assert db_session.get(models.Detection, setup[kept]) is not None, kept


def test_only_an_administrator_can_purge(client, db_session, monkeypatch):
    from app.security import create_access_token, hash_password
    monkeypatch.setattr(settings, "detection_retention_days", 30)
    role = db_session.query(models.Role).filter_by(name="Auditor").first()
    u = models.User(username=f"aud_{uuid.uuid4().hex[:6]}", password_hash=hash_password("x" * 10), full_name="a", role_id=role.id)
    db_session.add(u)
    db_session.commit()
    resp = client.post("/api/governance/purge-detections", json={"dry_run": False, "confirm": True},
                       headers=_auth(create_access_token(u)))
    assert resp.status_code == 403
