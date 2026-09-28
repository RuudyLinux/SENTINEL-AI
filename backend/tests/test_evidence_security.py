"""Evidence file serving stays inside the evidence dir, and an expired
resource token is actually rejected (not just assumed from jose)."""
import pytest
from fastapi import HTTPException

from app import models
from app.security import create_resource_token, get_user_from_resource_token
from app.config import settings


def test_expired_resource_token_is_rejected(db_session, admin_user):
    # A negative TTL mints a token whose `exp` claim is already in the past.
    token = create_resource_token("evidence_file", "evd_expired", admin_user, ttl_seconds=-1)
    with pytest.raises(HTTPException) as exc:
        get_user_from_resource_token("evidence_file", "evd_expired", token, db_session)
    assert exc.value.status_code == 401


def _make_evidence(db_session, file_path: str) -> models.Evidence:
    e = models.Evidence(evidence_type="snapshot", file_path=file_path, verification_status="unverified")
    db_session.add(e)
    db_session.commit()
    db_session.refresh(e)
    return e


def test_download_evidence_rejects_a_path_outside_the_evidence_directory(client, admin_token, db_session, tmp_path):
    # a corrupted row; real writes only go under evidence_dir, but serving
    # mustn't trust the column
    outside_file = tmp_path / "not_evidence.txt"
    outside_file.write_text("should never be served")
    evidence = _make_evidence(db_session, str(outside_file))

    token_resp = client.get(f"/api/evidence/{evidence.id}/file-token", headers={"Authorization": f"Bearer {admin_token}"})
    assert token_resp.status_code == 200, token_resp.text
    file_token = token_resp.json()["token"]

    resp = client.get(f"/api/evidence/{evidence.id}/file?token={file_token}")
    assert resp.status_code == 404
    assert "not evidence" not in resp.text  # never leaks the real file's content


def test_download_evidence_rejects_a_traversal_style_path(client, admin_token, db_session):
    traversal_path = str(settings.evidence_dir / ".." / ".." / "backend" / "app" / "config.py")
    evidence = _make_evidence(db_session, traversal_path)

    token_resp = client.get(f"/api/evidence/{evidence.id}/file-token", headers={"Authorization": f"Bearer {admin_token}"})
    file_token = token_resp.json()["token"]

    resp = client.get(f"/api/evidence/{evidence.id}/file?token={file_token}")
    assert resp.status_code == 404


def test_download_evidence_serves_a_real_file_inside_the_evidence_directory(client, admin_token, db_session):
    # Confirms the containment check doesn't break the legitimate case.
    real_file = settings.evidence_dir / "test_security_real_evidence.jpg"
    real_file.write_bytes(b"fake-jpeg-bytes")
    try:
        evidence = _make_evidence(db_session, str(real_file))
        token_resp = client.get(f"/api/evidence/{evidence.id}/file-token", headers={"Authorization": f"Bearer {admin_token}"})
        file_token = token_resp.json()["token"]
        resp = client.get(f"/api/evidence/{evidence.id}/file?token={file_token}")
        assert resp.status_code == 200
        assert resp.content == b"fake-jpeg-bytes"
    finally:
        real_file.unlink(missing_ok=True)
