"""List endpoints must bound what a single request can pull back.

`GET /api/detections` (and `/api/review/queue`) took `limit: int` with no
validation. SQLite reads `LIMIT -1` as NO LIMIT, so `?limit=-1` returned the
whole table — as did any absurdly large value — from any authenticated user,
against the largest table the platform writes (one row per detected object per
frame per camera).

Measured on a 120-row test database before the fix:
    default -> 100    limit=-1 -> 120 (all)    limit=100000000 -> 120 (all)

`self_heal.py` already used `Query(default=100, le=500)`; these two endpoints
were the deviation, not the new rule.
"""
import uuid

import pytest

from app import models


@pytest.fixture
def auth(admin_token):
    return {"Authorization": f"Bearer {admin_token}"}


@pytest.fixture
def many_detections(db_session):
    camera = models.Camera(
        camera_code=f"LIM-{uuid.uuid4().hex[:8]}", name="limit test cam",
        source_type="mock_vms", source_uri="",
    )
    db_session.add(camera)
    db_session.flush()
    for _ in range(120):
        db_session.add(models.Detection(camera_id=camera.id, cls="car", confidence=0.5, bbox=[1, 2, 3, 4]))
    db_session.commit()
    return camera


class TestDetectionsLimit:
    def test_a_negative_limit_is_rejected(self, client, auth, many_detections):
        resp = client.get("/api/detections?limit=-1", headers=auth)
        assert resp.status_code == 422, "limit=-1 reaches SQLite as 'LIMIT -1', which means no limit at all"

    def test_an_oversized_limit_is_rejected(self, client, auth, many_detections):
        assert client.get("/api/detections?limit=100000000", headers=auth).status_code == 422

    def test_zero_is_rejected(self, client, auth):
        assert client.get("/api/detections?limit=0", headers=auth).status_code == 422

    def test_the_ceiling_itself_is_allowed(self, client, auth, many_detections):
        resp = client.get("/api/detections?limit=500", headers=auth)
        assert resp.status_code == 200
        assert len(resp.json()) <= 500

    def test_a_normal_limit_still_works(self, client, auth, many_detections):
        """The clamp must not change ordinary paging behaviour."""
        resp = client.get(f"/api/detections?camera_id={many_detections.id}&limit=25", headers=auth)
        assert resp.status_code == 200
        assert len(resp.json()) == 25

    def test_the_default_is_unchanged(self, client, auth, many_detections):
        assert len(client.get("/api/detections", headers=auth).json()) == 100


class TestReviewQueueLimit:
    def test_a_negative_limit_is_rejected(self, client, auth):
        assert client.get("/api/review/queue?limit=-1", headers=auth).status_code == 422

    def test_a_normal_limit_still_works(self, client, auth):
        assert client.get("/api/review/queue?limit=10", headers=auth).status_code == 200
