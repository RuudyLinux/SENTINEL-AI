"""Optional SSRF egress policy (pipeline/egress_policy.py).

Opt-in so deployments with cameras on private ranges keep working. Tests
both that it blocks what it should when on, and that it changes nothing when
off, the default.
"""
import pytest

from app.config import settings
from app.pipeline.egress_policy import blocked_reason


@pytest.fixture
def policy_on(monkeypatch):
    monkeypatch.setattr(settings, "camera_source_block_private_networks", True)


@pytest.fixture
def policy_off(monkeypatch):
    monkeypatch.setattr(settings, "camera_source_block_private_networks", False)


class TestDisabledByDefault:
    def test_the_policy_is_off_unless_a_deployment_turns_it_on(self):
        assert settings.camera_source_block_private_networks is False

    @pytest.mark.parametrize("uri", [
        "rtsp://127.0.0.1:554/stream",
        "rtsp://10.0.0.5:554/stream",
        "rtsp://169.254.169.254/latest/meta-data/",
    ])
    def test_nothing_is_blocked_while_disabled(self, policy_off, uri):
        """Existing installations must be completely unaffected."""
        assert blocked_reason("rtsp", uri) is None


class TestBlockedTargets:
    @pytest.mark.parametrize("uri,fragment", [
        ("rtsp://127.0.0.1:554/stream", "loopback"),
        ("rtsp://localhost:554/stream", "loopback"),
        ("rtsp://[::1]:554/stream", "loopback"),
        ("rtsp://10.0.0.5:554/stream", "private"),
        ("rtsp://192.168.1.20:554/stream", "private"),
        ("rtsp://172.16.4.4:554/stream", "private"),
        ("rtsp://169.254.169.254/latest/meta-data/", "link-local"),
        # ipaddress counts 0.0.0.0/8 as private, so the private check catches
        # it first. blocked either way
        ("rtsp://0.0.0.0:554/stream", "private"),
        ("rtsp://224.0.0.1:554/stream", "reserved, multicast or unspecified"),
    ])
    def test_internal_targets_are_refused_with_a_reason(self, policy_on, uri, fragment):
        reason = blocked_reason("rtsp", uri)
        assert reason is not None, f"{uri} was allowed"
        assert fragment in reason


class TestAllowedTargets:
    def test_a_public_address_is_allowed(self, policy_on):
        """Literal global address, no DNS during the test. Not a documentation
        range like 203.0.113.0/24: ipaddress calls those private, so they'd be
        blocked."""
        assert blocked_reason("rtsp", "rtsp://8.8.8.8:554/stream") is None

    @pytest.mark.parametrize("source_type,uri", [
        ("video_file", "/var/data/clip.mp4"),
        ("webcam", "0"),
        ("mock_vms", ""),
    ])
    def test_non_network_sources_are_not_policed(self, policy_on, source_type, uri):
        """A file or local device reaches no network, nothing to decide."""
        assert blocked_reason(source_type, uri) is None

    def test_a_uri_with_no_extractable_host_is_not_blocked(self, policy_on):
        """No target to connect to; the open timeout handles it."""
        assert blocked_reason("rtsp", "not a url at all !!!") is None
        assert blocked_reason("rtsp", "") is None

    def test_an_unresolvable_hostname_is_not_blocked(self, policy_on):
        """Resolution failed, nothing to judge; the connect fails on its own."""
        assert blocked_reason("rtsp", "rtsp://nonexistent.invalid:554/s") is None


class TestEnforcedAtTheEndpoints:
    """Both call sites consult it: the probe and camera registration.
    Gating only the probe would let you skip it and register the camera,
    whose worker opens the stream right after.

    Only blocked paths go over HTTP; they 400 before any connection or
    worker, so they're fast and leave nothing running.
    """

    def _auth(self, admin_token):
        return {"Authorization": f"Bearer {admin_token}"}

    def test_test_connection_refuses_an_internal_target(self, client, admin_token, policy_on):
        resp = client.post(
            "/api/cameras/test-connection",
            data={"source_type": "rtsp", "source_uri": "rtsp://127.0.0.1:554/stream"},
            headers=self._auth(admin_token),
        )
        assert resp.status_code == 400
        assert "loopback" in resp.json()["detail"]

    def test_camera_registration_refuses_an_internal_target(self, client, admin_token, db_session, policy_on):
        import uuid

        from app import models

        code = f"EGR-{uuid.uuid4().hex[:8]}"
        resp = client.post(
            "/api/cameras",
            json={
                "camera_code": code, "name": "egress probe", "source_type": "rtsp",
                "source_uri": "rtsp://192.168.1.50:554/stream",
            },
            headers=self._auth(admin_token),
        )
        assert resp.status_code == 400
        assert "private" in resp.json()["detail"]
        # Refused BEFORE the row was written, so no camera and no worker.
        assert db_session.query(models.Camera).filter(models.Camera.camera_code == code).count() == 0

    def test_a_non_network_camera_is_unaffected_by_the_policy(self, client, admin_token, db_session, policy_on):
        """A no-network camera registers fine with the policy at its strictest.

        mock_vms, not video_file. With video_file create_camera starts a live
        FFmpeg decode with no teardown, and about 1 run in 3 died with

            Assertion fctx->async_lock failed at libavcodec/pthread_frame.c:178

        (exit 3, no traceback), the hazard conftest's client fixture is about.
        """
        import uuid

        from app import models

        code = f"EGR-{uuid.uuid4().hex[:8]}"
        resp = client.post(
            "/api/cameras",
            json={
                "camera_code": code, "name": "local source", "source_type": "mock_vms",
                "source_uri": "", "ai_person": False, "ai_vehicle": False, "ai_anpr": False,
            },
            headers=self._auth(admin_token),
        )
        assert resp.status_code == 200
        assert db_session.query(models.Camera).filter(models.Camera.camera_code == code).count() == 1
