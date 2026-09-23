"""Account administration must not be able to lock everyone out.

`POST /api/users/{id}/disable` had no self-disable guard, and nothing in the
API ever set `active` back to True. Disabling takes effect immediately —
`auth.py` rejects a login from an inactive user and `get_current_user` rejects
their existing token — so an administrator who disabled their own account got
200 and then 401 on the very next request, with no route back.

Measured before the fix: disable returned 200 and the caller's next
`GET /api/users` returned 401.

Administrator gates user management, camera registration, rule deletion and
the governance purge, so recovery meant editing the database by hand. Since
this endpoint is Administrator-gated, self-disable is the ONLY route to zero
administrators: any other caller disabling an administrator is an active
administrator who remains.
"""
import uuid

import pytest

from app import models


@pytest.fixture
def auth(admin_token):
    return {"Authorization": f"Bearer {admin_token}"}


def _make_user(client, auth, role_name: str = "Control Room Operator"):
    username = f"user-{uuid.uuid4().hex[:8]}"
    password = "Str0ng-Passw0rd!"
    resp = client.post(
        "/api/users",
        json={
            "username": username, "password": password, "full_name": "Test Account",
            "department": "Testing", "role_name": role_name,
        },
        headers=auth,
    )
    assert resp.status_code == 200, resp.text
    return resp.json(), password


class TestSelfDisableIsRefused:
    def test_an_administrator_cannot_disable_their_own_account(self, client, auth, admin_user):
        resp = client.post(f"/api/users/{admin_user.id}/disable", headers=auth)
        assert resp.status_code == 400
        assert "own account" in resp.json()["detail"]

    def test_the_caller_still_works_afterwards(self, client, auth, admin_user):
        """The failure mode being prevented: 200, then 401 on everything."""
        client.post(f"/api/users/{admin_user.id}/disable", headers=auth)
        assert client.get("/api/users", headers=auth).status_code == 200

    def test_disabling_someone_else_still_works(self, client, auth, db_session):
        """The guard must be exactly self-disable, not a blanket refusal."""
        created, _ = _make_user(client, auth)
        assert client.post(f"/api/users/{created['id']}/disable", headers=auth).status_code == 200
        db_session.expire_all()
        assert db_session.query(models.User).filter(models.User.id == created["id"]).one().active is False


class TestDisableIsReversible:
    def test_a_disabled_account_can_be_enabled_again(self, client, auth, db_session):
        created, _ = _make_user(client, auth)
        client.post(f"/api/users/{created['id']}/disable", headers=auth)

        assert client.post(f"/api/users/{created['id']}/enable", headers=auth).status_code == 200
        db_session.expire_all()
        assert db_session.query(models.User).filter(models.User.id == created["id"]).one().active is True

    def test_a_re_enabled_account_can_log_in_again(self, client, auth):
        created, password = _make_user(client, auth)
        assert client.post("/api/auth/login", json={"username": created["username"], "password": password}).status_code == 200

        client.post(f"/api/users/{created['id']}/disable", headers=auth)
        # 403, not 401: the credentials were correct, the account is closed.
        assert client.post("/api/auth/login", json={"username": created["username"], "password": password}).status_code == 403

        client.post(f"/api/users/{created['id']}/enable", headers=auth)
        assert client.post("/api/auth/login", json={"username": created["username"], "password": password}).status_code == 200

    def test_enabling_an_unknown_user_is_a_404(self, client, auth):
        assert client.post("/api/users/nope-not-a-user/enable", headers=auth).status_code == 404

    def test_enable_requires_administrator(self, client, auth):
        created, password = _make_user(client, auth)
        token = client.post(
            "/api/auth/login", json={"username": created["username"], "password": password}
        ).json()["access_token"]
        resp = client.post(f"/api/users/{created['id']}/enable", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 403

    def test_both_account_changes_are_audited(self, client, auth, db_session):
        created, _ = _make_user(client, auth)
        client.post(f"/api/users/{created['id']}/disable", headers=auth)
        client.post(f"/api/users/{created['id']}/enable", headers=auth)

        actions = {
            row.action for row in db_session.query(models.AuditLog)
            .filter(models.AuditLog.resource == created["id"]).all()
        }
        assert {"disable_user", "enable_user"} <= actions


class TestDisabledLoginIsAudited:
    def test_using_a_revoked_account_leaves_a_trace(self, client, auth, db_session):
        """Correct credentials against a disabled account returned 403 and
        recorded nothing — a revoked operator still holding a working password
        is exactly the event an audit log exists for."""
        created, password = _make_user(client, auth)
        client.post(f"/api/users/{created['id']}/disable", headers=auth)

        client.post("/api/auth/login", json={"username": created["username"], "password": password})

        logged = (
            db_session.query(models.AuditLog)
            .filter(
                models.AuditLog.action == "login_disabled_account",
                models.AuditLog.resource == created["username"],
            )
            .all()
        )
        assert logged, "a login attempt on a disabled account was not audited"
        assert logged[0].result == "FAILURE"
