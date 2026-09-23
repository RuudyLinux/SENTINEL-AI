"""Optional SSRF egress policy (workstream C3) — pipeline/egress_policy.py.

`docs/THREAT_MODEL.md` recorded camera SSRF as role-gated only, with no
private-IP blocklist. This is that blocklist, opt-in so it cannot break
deployments whose cameras legitimately live on private ranges.

The tests cover both directions deliberately: that the policy blocks what it
claims to when enabled, and — just as important — that it changes NOTHING
while disabled, which is the default every existing deployment runs.
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
        # Python's ipaddress puts 0.0.0.0/8 in the PRIVATE set, so this is
        # caught by the private check before reaching the unspecified one.
        # Blocked either way, which is what matters.
        ("rtsp://0.0.0.0:554/stream", "private"),
        ("rtsp://224.0.0.1:554/stream", "reserved, multicast or unspecified"),
    ])
    def test_internal_targets_are_refused_with_a_reason(self, policy_on, uri, fragment):
        reason = blocked_reason("rtsp", uri)
        assert reason is not None, f"{uri} was allowed"
        assert fragment in reason


class TestAllowedTargets:
    def test_a_public_address_is_allowed(self, policy_on):
        """A literal global address, so no DNS lookup happens during the test.

        NOT a documentation range (203.0.113.0/24 etc): Python's `ipaddress`
        classifies those as PRIVATE, so they are correctly blocked by this
        policy and cannot stand in for a public camera.
        """
        assert blocked_reason("rtsp", "rtsp://8.8.8.8:554/stream") is None

    @pytest.mark.parametrize("source_type,uri", [
        ("video_file", "/var/data/clip.mp4"),
        ("webcam", "0"),
        ("mock_vms", ""),
    ])
    def test_non_network_sources_are_not_policed(self, policy_on, source_type, uri):
        """A file on disk or a local capture device reaches no network, so
        there is nothing for an egress policy to decide."""
        assert blocked_reason(source_type, uri) is None

    def test_a_uri_with_no_extractable_host_is_not_blocked(self, policy_on):
        """It names no target to connect to; the existing open-timeout path
        already handles it, and blocking here would be a confusing error."""
        assert blocked_reason("rtsp", "not a url at all !!!") is None
        assert blocked_reason("rtsp", "") is None

    def test_an_unresolvable_hostname_is_not_blocked(self, policy_on):
        """Resolution failure yields nothing to judge. The connection attempt
        will fail on its own, bounded by the open timeout."""
        assert blocked_reason("rtsp", "rtsp://nonexistent.invalid:554/s") is None


class TestEnforcedAtTheEndpoints:
    """The policy function is only useful if it is actually consulted. Both
    call sites are covered: the probe endpoint AND camera registration —
    gating only the probe would leave the same reach available by simply
    skipping the probe and registering the camera, whose worker opens the
    stream moments later.

    Only the BLOCKED paths are exercised over HTTP. They return 400 before
    any connection is attempted or any worker is started, so these stay fast
    and cannot leave a background RTSP task running in the suite.
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
        """A camera that reaches no network must register normally even with
        the policy at its strictest.

        Uses `mock_vms`, NOT `video_file`. The first version of this test
        registered a real video_file camera pointing at the bundled clip, and
        `create_camera` starts a worker for it — a live FFmpeg decode with no
        teardown. That reproducibly aborted the whole test process a moment
        later with

            Assertion fctx->async_lock failed at libavcodec/pthread_frame.c:178

        (pytest exit 3, no traceback, ~1 run in 3), which is exactly the
        hazard tests/conftest.py's `client` fixture comment documents: API
        route tests have no business starting real camera AI workers.
        `mock_vms` is equally non-network, so it proves the same property
        without decoding anything.
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
