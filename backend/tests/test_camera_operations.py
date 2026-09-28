"""Camera lifecycle, diagnostics, system status and person search.

The network/worker-heavy paths (catalogue and grid sync, upload, probing)
are tested elsewhere; this covers the cheap everyday ones: health,
diagnostics, start/stop/restart, delete refusal, person-similarity guards.

mock_vms cameras everywhere: create_camera starts a worker, and a
video_file camera here means a live FFmpeg decode with no teardown, which
used to abort whole runs with a libavcodec assertion (see conftest).
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
        camera_code=f"OPS-{uuid.uuid4().hex[:8]}", name="operations cam",
        source_type="mock_vms", source_uri="", status="online", fps=12.5,
        resolution="1920x1080", error_count=0,
    )
    db_session.add(cam)
    db_session.commit()
    return cam


def _operator_token(client, auth, role_name: str) -> str:
    username = f"ops-{uuid.uuid4().hex[:8]}"
    password = "Str0ng-Passw0rd!"
    created = client.post(
        "/api/users",
        json={
            "username": username, "password": password, "full_name": "Ops Tester",
            "department": "Testing", "role_name": role_name,
        },
        headers=auth,
    )
    assert created.status_code == 200, created.text
    return client.post(
        "/api/auth/login", json={"username": username, "password": password}
    ).json()["access_token"]


class TestCameraHealth:
    def test_health_reports_the_stored_state(self, client, auth, camera):
        body = client.get(f"/api/cameras/{camera.id}/health", headers=auth).json()
        assert body["status"] == "online"
        assert body["fps"] == 12.5
        assert body["resolution"] == "1920x1080"

    def test_health_of_an_unknown_camera_is_a_404(self, client, auth):
        assert client.get("/api/cameras/no-such-camera/health", headers=auth).status_code == 404

    def test_health_requires_authentication(self, client, camera):
        assert client.get(f"/api/cameras/{camera.id}/health").status_code == 401


class TestCameraDiagnostics:
    def test_diagnostics_report_the_worker_state(self, client, auth, camera):
        """A camera with no worker must say so, rather than looking healthy."""
        body = client.get(f"/api/cameras/{camera.id}/diagnostics", headers=auth).json()
        assert body["camera_id"] == camera.id
        assert body["worker_task_state"] == "not_started"
        assert body["worker_task_error"] is None

    def test_diagnostics_of_an_unknown_camera_is_a_404(self, client, auth):
        assert client.get("/api/cameras/no-such-camera/diagnostics", headers=auth).status_code == 404

    def test_diagnostics_are_not_open_to_every_role(self, client, auth, camera):
        token = _operator_token(client, auth, "Investigator")
        resp = client.get(f"/api/cameras/{camera.id}/diagnostics", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 403

    def test_system_diagnostics_are_role_gated(self, client, auth):
        token = _operator_token(client, auth, "Investigator")
        assert client.get(
            "/api/cameras/diagnostics/system", headers={"Authorization": f"Bearer {token}"}
        ).status_code == 403

    def test_system_diagnostics_are_served_to_an_administrator(self, client, auth):
        """Also checks /diagnostics/system isn't swallowed by
        /{camera_id}/diagnostics."""
        assert client.get("/api/cameras/diagnostics/system", headers=auth).status_code == 200

    def test_illegal_state_transitions_are_reported_when_clean(self, client, auth):
        """worker._TRANSITIONS should keep this empty; this is where it'd show."""
        from app.pipeline import worker
        worker.ILLEGAL_TRANSITIONS.clear()
        body = client.get("/api/cameras/diagnostics/system", headers=auth).json()
        assert body["illegal_state_transitions"] == {}

    def test_an_illegal_state_transition_shows_up_here(self, client, auth):
        from app.pipeline import worker
        worker.CAMERA_STATS.clear()
        worker.ILLEGAL_TRANSITIONS.clear()
        worker._set_grid_state("probe-cam", "CONNECTING")
        worker._set_grid_state("probe-cam", "DISCONNECTED")
        worker._set_grid_state("probe-cam", "PROCESSING")  # illegal: DISCONNECTED -> PROCESSING
        body = client.get("/api/cameras/diagnostics/system", headers=auth).json()
        assert body["illegal_state_transitions"] == {"DISCONNECTED->PROCESSING": 1}
        worker.CAMERA_STATS.clear()
        worker.ILLEGAL_TRANSITIONS.clear()


class TestCameraLifecycle:
    @pytest.mark.parametrize("action", ["start", "stop", "restart"])
    def test_acting_on_an_unknown_camera_is_a_404(self, client, auth, action):
        assert client.post(f"/api/cameras/no-such-camera/{action}", headers=auth).status_code == 404

    def test_stop_marks_the_camera_offline(self, client, auth, db_session, camera):
        assert client.post(f"/api/cameras/{camera.id}/stop", headers=auth).status_code == 200
        db_session.expire_all()
        assert db_session.query(models.Camera).filter(models.Camera.id == camera.id).one().status == "offline"

    def test_stop_is_audited(self, client, auth, db_session, camera):
        client.post(f"/api/cameras/{camera.id}/stop", headers=auth)
        actions = {
            row.action for row in db_session.query(models.AuditLog)
            .filter(models.AuditLog.resource == camera.camera_code).all()
        }
        assert "stop_camera" in actions

    def test_lifecycle_actions_are_role_gated(self, client, auth, camera):
        token = _operator_token(client, auth, "Investigator")
        assert client.post(
            f"/api/cameras/{camera.id}/stop", headers={"Authorization": f"Bearer {token}"}
        ).status_code == 403


class TestCameraDeletion:
    def test_a_camera_with_history_is_refused_not_orphaned(self, client, auth, db_session, camera):
        """Deleting a camera used to silently orphan its evidence."""
        detection = models.Detection(camera_id=camera.id, cls="car", confidence=0.9, bbox=[1, 2, 3, 4])
        db_session.add(detection)
        db_session.commit()

        resp = client.delete(f"/api/cameras/{camera.id}", headers=auth)
        assert resp.status_code == 409
        assert db_session.query(models.Camera).filter(models.Camera.id == camera.id).count() == 1

    def test_a_camera_with_no_history_deletes_cleanly(self, client, auth, db_session, camera):
        # grab the id before the delete, reading attrs off a deleted instance
        # after expire_all() raises ObjectDeletedError
        camera_id = camera.id
        assert client.delete(f"/api/cameras/{camera_id}", headers=auth).status_code == 200
        db_session.expire_all()
        assert db_session.query(models.Camera).filter(models.Camera.id == camera_id).count() == 0

    def test_deletion_requires_administrator(self, client, auth, camera):
        token = _operator_token(client, auth, "Control Room Operator")
        assert client.delete(
            f"/api/cameras/{camera.id}", headers={"Authorization": f"Bearer {token}"}
        ).status_code == 403


class TestSystemStatus:
    def test_every_subsystem_is_reported(self, client, auth):
        body = client.get("/api/system/status", headers=auth).json()
        names = {s["name"] for s in body["subsystems"]}
        assert {"API", "DATABASE", "WEBSOCKET", "STORAGE"} <= names

    def test_the_database_check_actually_runs(self, client, auth):
        """DATABASE comes from a real SELECT 1, so it's OPERATIONAL while the
        test DB is reachable."""
        body = client.get("/api/system/status", headers=auth).json()
        database = next(s for s in body["subsystems"] if s["name"] == "DATABASE")
        assert database["status"] == "OPERATIONAL"

    def test_status_requires_authentication(self, client):
        assert client.get("/api/system/status").status_code == 401


class TestPersonSimilarity:
    def test_an_unknown_detection_is_a_404(self, client, auth):
        assert client.get("/api/persons/no-such-detection/similar", headers=auth).status_code == 404

    def test_a_non_person_detection_is_refused(self, client, auth, db_session, camera):
        """It ranks person signatures; a car would get a confident-looking
        empty answer to a question that doesn't apply."""
        detection = models.Detection(camera_id=camera.id, cls="car", confidence=0.9, bbox=[1, 2, 3, 4])
        db_session.add(detection)
        db_session.commit()

        resp = client.get(f"/api/persons/{detection.id}/similar", headers=auth)
        assert resp.status_code == 400
        assert "person" in resp.json()["detail"].lower()

    def test_a_person_with_no_signature_returns_no_candidates_not_an_error(
        self, client, auth, db_session, camera
    ):
        """No stored signature means no candidates, not a made-up match."""
        detection = models.Detection(camera_id=camera.id, cls="person", confidence=0.9, bbox=[1, 2, 3, 4])
        db_session.add(detection)
        db_session.commit()

        body = client.get(f"/api/persons/{detection.id}/similar", headers=auth).json()
        assert body["reference_detection_id"] == detection.id
        assert body["candidates"] == []

    def test_min_similarity_is_bounded(self, client, auth, db_session, camera):
        detection = models.Detection(camera_id=camera.id, cls="person", confidence=0.9, bbox=[1, 2, 3, 4])
        db_session.add(detection)
        db_session.commit()

        assert client.get(
            f"/api/persons/{detection.id}/similar?min_similarity=1.5", headers=auth
        ).status_code == 422

    def test_person_detections_are_listed(self, client, auth, db_session, camera):
        detection = models.Detection(camera_id=camera.id, cls="person", confidence=0.9, bbox=[1, 2, 3, 4])
        db_session.add(detection)
        db_session.commit()

        listed = client.get("/api/persons/detections", headers=auth).json()
        assert detection.id in [d["id"] for d in listed]
