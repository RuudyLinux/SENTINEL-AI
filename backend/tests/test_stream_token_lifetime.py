"""A live stream must not outlive the token that authorized it.

`GET /api/streams/{id}/mjpeg` validated its resource token once, at connect,
and then held the response open forever. `settings.stream_token_ttl_seconds`
(default 3600) therefore bounded nothing for the one endpoint whose access
lasts long enough for a bound to matter: a tab left open kept receiving live
video days later, and deactivating the operator's account did not interrupt
the feed — the account check also runs only at connect.

The stream now stops at the token's `exp`. Re-authorizing means asking for a
new token, which re-runs the full RBAC and active-account check.
"""
import asyncio
import uuid
from datetime import datetime, timedelta

import pytest

from app import models
from app.config import settings
from app.routers import streams
from app.security import create_resource_token, resource_token_expiry


@pytest.fixture
def auth(admin_token):
    return {"Authorization": f"Bearer {admin_token}"}


@pytest.fixture
def camera(db_session):
    cam = models.Camera(
        camera_code=f"STR-{uuid.uuid4().hex[:8]}", name="stream lifetime cam",
        source_type="mock_vms", source_uri="", status="online",
    )
    db_session.add(cam)
    db_session.commit()
    return cam


def _drain(camera_id, deadline, max_chunks=2):
    """Pull at most `max_chunks` frames from the generator, then close it."""
    async def run():
        chunks = []
        agen = streams._mjpeg_generator(camera_id, deadline)
        try:
            async for chunk in agen:
                chunks.append(chunk)
                if len(chunks) >= max_chunks:
                    break
        finally:
            await agen.aclose()
        return chunks
    return asyncio.run(run())


class TestStreamDeadline:
    def test_a_stream_ends_at_its_deadline(self, camera):
        streams.LATEST_FRAMES[camera.id] = b"fake-jpeg-bytes"
        try:
            past = datetime.utcnow() - timedelta(seconds=5)
            assert _drain(camera.id, past) == [], (
                "frames were still delivered after the authorizing token expired"
            )
        finally:
            streams.LATEST_FRAMES.pop(camera.id, None)

    def test_a_stream_with_a_live_token_still_delivers_frames(self, camera, admin_user):
        """The bound must not break streaming itself."""
        streams.LATEST_FRAMES[camera.id] = b"fake-jpeg-bytes"
        try:
            live = streams._stream_deadline(
                create_resource_token("camera_stream", camera.id, admin_user, ttl_seconds=3600)
            )
            chunks = _drain(camera.id, live, max_chunks=1)
            assert len(chunks) == 1 and b"fake-jpeg-bytes" in chunks[0]
        finally:
            streams.LATEST_FRAMES.pop(camera.id, None)

    def test_the_deadline_comes_from_the_token(self, camera, admin_user):
        token = create_resource_token("camera_stream", camera.id, admin_user, ttl_seconds=120)
        deadline = streams._stream_deadline(token)
        assert timedelta(seconds=60) < (deadline - datetime.utcnow()) <= timedelta(seconds=120)

    def test_an_unreadable_token_does_not_become_an_unlimited_stream(self):
        """The first version of this fix failed open here. `jwt.decode`
        verifies `exp`, so an EXPIRED token cannot be decoded at all and
        `resource_token_expiry` returns None for it — exactly the input the
        bound exists for. None must never mean "stream forever"."""
        assert resource_token_expiry("not-a-jwt") is None
        fallback = streams._stream_deadline("not-a-jwt")
        assert fallback <= datetime.utcnow() + timedelta(seconds=settings.stream_token_ttl_seconds)


class TestStreamAuthorizationStillEnforced:
    def test_an_expired_token_cannot_open_a_stream(self, client, camera, admin_user):
        token = create_resource_token("camera_stream", camera.id, admin_user, ttl_seconds=-5)
        assert client.get(f"/api/streams/{camera.id}/mjpeg?token={token}").status_code == 401

    def test_a_token_for_another_camera_is_refused(self, client, camera, admin_user):
        token = create_resource_token("camera_stream", "some-other-camera", admin_user, ttl_seconds=3600)
        assert client.get(f"/api/streams/{camera.id}/mjpeg?token={token}").status_code == 401

    def test_no_token_at_all_is_refused(self, client, camera):
        assert client.get(f"/api/streams/{camera.id}/mjpeg").status_code == 422

    def test_a_stream_token_still_requires_authentication(self, client, camera):
        assert client.get(f"/api/streams/{camera.id}/stream-token").status_code == 401

    def test_an_authenticated_user_can_obtain_a_token(self, client, camera, auth):
        resp = client.get(f"/api/streams/{camera.id}/stream-token", headers=auth)
        assert resp.status_code == 200
        assert resource_token_expiry(resp.json()["token"]) is not None

    def test_a_token_for_a_missing_camera_is_not_issued(self, client, auth):
        assert client.get("/api/streams/nope-does-not-exist/stream-token", headers=auth).status_code == 404

    def test_the_snapshot_endpoint_enforces_the_same_token(self, client, camera):
        assert client.get(f"/api/streams/{camera.id}/snapshot.jpg?token=garbage").status_code == 401
