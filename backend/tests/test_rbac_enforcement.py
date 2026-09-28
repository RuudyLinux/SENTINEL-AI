"""RBAC rejects lower roles at the API, not just by hiding buttons. Every
earlier test used Administrator, which passes every require_roles() and
would never catch a wrong role list."""
import pytest

from app import models
from app.security import hash_password, create_access_token


@pytest.fixture
def auditor_user(db_session):
    """Auditor: audit and compliance visibility only (seed.py), the lowest
    role for every write below."""
    role = db_session.query(models.Role).filter(models.Role.name == "Auditor").first()
    if role is None:
        role = models.Role(name="Auditor", description="test")
        db_session.add(role)
        db_session.flush()
    user = db_session.query(models.User).filter(models.User.username == "test_auditor").first()
    if user is None:
        user = models.User(
            username="test_auditor", password_hash=hash_password("testpass123"),
            full_name="Test Auditor", role_id=role.id,
        )
        db_session.add(user)
        db_session.commit()
        db_session.refresh(user)
    return user


@pytest.fixture
def auditor_token(auditor_user):
    return create_access_token(auditor_user)


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_auditor_cannot_create_a_camera(client, auditor_token):
    resp = client.post(
        "/api/cameras",
        json={"camera_code": "C-RBAC-1", "name": "x", "source_type": "mock_vms", "source_uri": ""},
        headers=_auth(auditor_token),
    )
    assert resp.status_code == 403


def test_admin_can_create_a_camera(client, admin_token):
    resp = client.post(
        "/api/cameras",
        json={"camera_code": "C-RBAC-ADMIN-OK", "name": "x", "source_type": "mock_vms", "source_uri": ""},
        headers=_auth(admin_token),
    )
    assert resp.status_code == 200, resp.text


def test_auditor_cannot_create_a_watchlist_entry(client, auditor_token):
    resp = client.post(
        "/api/watchlists",
        json={"entity_type": "plate", "identifier": "GJ01ZZ0000", "reason": "x"},
        headers=_auth(auditor_token),
    )
    assert resp.status_code == 403


def test_auditor_cannot_create_a_zone(client, auditor_token):
    resp = client.post(
        "/api/zones",
        json={"name": "x", "camera_id": "cam_does_not_matter"},
        headers=_auth(auditor_token),
    )
    assert resp.status_code == 403


def test_only_administrator_can_list_users(client, auditor_token, admin_token):
    denied = client.get("/api/users", headers=_auth(auditor_token))
    assert denied.status_code == 403
    allowed = client.get("/api/users", headers=_auth(admin_token))
    assert allowed.status_code == 200


def test_only_administrator_can_create_a_user(client, auditor_token):
    resp = client.post(
        "/api/users",
        json={"username": "should_not_be_created", "password": "x", "role_name": "Auditor"},
        headers=_auth(auditor_token),
    )
    assert resp.status_code == 403


def test_unauthenticated_request_is_rejected_not_treated_as_empty_data(client):
    # no token must be a 401, never an empty list a client could read as
    # "no cameras"
    resp = client.get("/api/cameras")
    assert resp.status_code == 401


def test_auditor_can_still_read_cameras(client, auditor_token):
    # RBAC limits writes, not reads; every role can list cameras
    resp = client.get("/api/cameras", headers=_auth(auditor_token))
    assert resp.status_code == 200
