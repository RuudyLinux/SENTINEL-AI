"""Incident creation, assignment and timeline.

`app/routers/incidents.py` was the least-covered module left (43%). Four
defects, each reproduced before it was fixed:

1. `POST /api/incidents` with an unknown `camera_id`, `alert_id` or
   `vehicle_id` raised an unhandled IntegrityError — a 500 carrying a raw
   "FOREIGN KEY constraint failed". Before SQLite foreign keys were enforced
   the same request silently stored a dangling reference, which is worse: an
   incident pointing at no camera still renders as an incident.

2. `POST /{id}/assign` did not check the assignee exists — same 500. Assigning
   to a DISABLED account was accepted too, moving the incident to in_progress
   with nobody able to log in and work it.

3. `GET /{id}/timeline` did `', '.join(alert.reasons)` on a nullable column:
   `TypeError: can only join an iterable`, a 500 on an otherwise valid
   incident. The summary endpoint in the same file already guards that column
   with `or []`; this call site did not.

4. An incident note had no length bound — a 2,000,000-character note was
   accepted (200, measured) and then rendered into the timeline.
"""
import uuid

import pytest

from app import models


@pytest.fixture
def auth(admin_token):
    return {"Authorization": f"Bearer {admin_token}"}


@pytest.fixture
def camera(db_session):
    cam = models.Camera(
        camera_code=f"INC-{uuid.uuid4().hex[:8]}", name="incident test cam",
        source_type="mock_vms", source_uri="",
    )
    db_session.add(cam)
    db_session.commit()
    return cam


def _incident(client, auth, **fields) -> dict:
    resp = client.post("/api/incidents", json={"title": f"inc-{uuid.uuid4().hex[:6]}", **fields}, headers=auth)
    assert resp.status_code == 200, resp.text
    return resp.json()


class TestCreationValidatesReferences:
    @pytest.mark.parametrize("field,label", [
        ("camera_id", "Camera"), ("alert_id", "Alert"), ("vehicle_id", "Vehicle"),
    ])
    def test_an_unknown_reference_is_a_404_not_a_500(self, client, auth, field, label):
        resp = client.post("/api/incidents", json={"title": "ghost", field: f"no-such-{field}"}, headers=auth)
        assert resp.status_code == 404
        assert label in resp.json()["detail"]

    def test_a_valid_reference_is_accepted(self, client, auth, camera):
        assert _incident(client, auth, camera_id=camera.id)["camera_id"] == camera.id

    def test_an_incident_with_no_references_is_still_allowed(self, client, auth):
        """All three columns are nullable — a manually raised incident need
        name none of them."""
        assert _incident(client, auth)["id"]


class TestAssignment:
    def test_an_unknown_assignee_is_a_404_not_a_500(self, client, auth):
        incident = _incident(client, auth)
        resp = client.post(f"/api/incidents/{incident['id']}/assign?assignee_user_id=no-such-user", headers=auth)
        assert resp.status_code == 404

    def test_a_disabled_account_cannot_be_assigned_work(self, client, auth):
        created = client.post(
            "/api/users",
            json={
                "username": f"assignee-{uuid.uuid4().hex[:8]}", "password": "Str0ng-Passw0rd!",
                "full_name": "Disabled Assignee", "department": "Testing",
                "role_name": "Control Room Operator",
            },
            headers=auth,
        ).json()
        client.post(f"/api/users/{created['id']}/disable", headers=auth)

        incident = _incident(client, auth)
        resp = client.post(f"/api/incidents/{incident['id']}/assign?assignee_user_id={created['id']}", headers=auth)
        assert resp.status_code == 400
        assert "disabled" in resp.json()["detail"]

    def test_assigning_an_active_user_works(self, client, auth, db_session, admin_user):
        incident = _incident(client, auth)
        resp = client.post(f"/api/incidents/{incident['id']}/assign?assignee_user_id={admin_user.id}", headers=auth)
        assert resp.status_code == 200

        db_session.expire_all()
        row = db_session.query(models.Incident).filter(models.Incident.id == incident["id"]).one()
        assert row.assigned_to == admin_user.id and row.status == "in_progress"


class TestTimeline:
    def test_an_alert_with_no_reasons_does_not_break_the_timeline(self, client, auth, db_session, camera):
        alert = models.Alert(camera_id=camera.id, severity="HIGH", reasons=None)
        db_session.add(alert)
        db_session.flush()
        incident = models.Incident(title=f"no-reasons-{uuid.uuid4().hex[:6]}", alert_id=alert.id)
        db_session.add(incident)
        db_session.commit()

        resp = client.get(f"/api/incidents/{incident.id}/timeline", headers=auth)
        assert resp.status_code == 200, "joining a null reasons column raised TypeError"
        assert resp.json()["events"][0]["label"] == "Alert fired: "

    def test_alert_reasons_are_still_rendered(self, client, auth, db_session, camera):
        alert = models.Alert(camera_id=camera.id, severity="HIGH", reasons=["zone entry", "watchlist"])
        db_session.add(alert)
        db_session.flush()
        incident = models.Incident(title=f"reasons-{uuid.uuid4().hex[:6]}", alert_id=alert.id)
        db_session.add(incident)
        db_session.commit()

        labels = [e["label"] for e in client.get(f"/api/incidents/{incident.id}/timeline", headers=auth).json()["events"]]
        assert "Alert fired: zone entry, watchlist" in labels

    def test_a_note_appears_on_the_timeline(self, client, auth):
        incident = _incident(client, auth)
        assert client.post(f"/api/incidents/{incident['id']}/notes", json={"text": "checked CCTV"}, headers=auth).status_code == 200

        labels = [e["label"] for e in client.get(f"/api/incidents/{incident['id']}/timeline", headers=auth).json()["events"]]
        assert "Note: checked CCTV" in labels


class TestNoteBounds:
    def test_an_absurdly_long_note_is_refused(self, client, auth):
        incident = _incident(client, auth)
        resp = client.post(f"/api/incidents/{incident['id']}/notes", json={"text": "x" * 2_000_000}, headers=auth)
        assert resp.status_code == 422

    def test_an_empty_note_is_refused(self, client, auth):
        incident = _incident(client, auth)
        assert client.post(f"/api/incidents/{incident['id']}/notes", json={"text": ""}, headers=auth).status_code == 422

    def test_a_normal_note_is_accepted(self, client, auth):
        incident = _incident(client, auth)
        assert client.post(f"/api/incidents/{incident['id']}/notes", json={"text": "x" * 4000}, headers=auth).status_code == 200

    def test_a_note_on_an_unknown_incident_is_a_404(self, client, auth):
        assert client.post("/api/incidents/no-such-incident/notes", json={"text": "hi"}, headers=auth).status_code == 404
