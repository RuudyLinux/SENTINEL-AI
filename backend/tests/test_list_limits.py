"""List endpoints bound what one request can pull back.

/api/detections and /api/review/queue took a bare `limit: int`. SQLite reads
LIMIT -1 as no limit, so ?limit=-1 (or any huge value) returned the whole
table, the biggest one we have, to any logged-in user.

On a 120-row test DB before the fix:
    default -> 100    limit=-1 -> 120 (all)    limit=100000000 -> 120 (all)

self_heal.py already had Query(default=100, le=500).
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


@pytest.fixture
def many_alerts(db_session):
    """More alerts than the default page, on one camera, so the asserts
    don't depend on whatever else is in the shared DB.
    """
    camera = models.Camera(
        camera_code=f"ALIM-{uuid.uuid4().hex[:8]}", name="alert limit test cam",
        source_type="mock_vms", source_uri="",
    )
    db_session.add(camera)
    db_session.flush()
    for _ in range(210):
        db_session.add(models.Alert(camera_id=camera.id, severity="LOW", status="new"))
    db_session.commit()
    return camera


class TestAlertsLimit:
    """/api/alerts was hard-capped at 200 with no limit param. Not a hole
    like an unbounded .all(), but the Alert Center couldn't page. Now the
    same everywhere: default 200, ge=1, le=500.
    """

    def test_a_negative_limit_is_rejected(self, client, auth, many_alerts):
        resp = client.get("/api/alerts?limit=-1", headers=auth)
        assert resp.status_code == 422, "limit=-1 reaches SQLite as 'LIMIT -1', which means no limit at all"

    def test_an_oversized_limit_is_rejected(self, client, auth):
        assert client.get("/api/alerts?limit=100000000", headers=auth).status_code == 422

    def test_zero_is_rejected(self, client, auth):
        assert client.get("/api/alerts?limit=0", headers=auth).status_code == 422

    def test_the_ceiling_itself_is_allowed(self, client, auth, many_alerts):
        resp = client.get("/api/alerts?limit=500", headers=auth)
        assert resp.status_code == 200
        assert len(resp.json()) <= 500

    def test_a_smaller_page_is_honoured(self, client, auth, many_alerts):
        resp = client.get(f"/api/alerts?camera_id={many_alerts.id}&limit=25", headers=auth)
        assert resp.status_code == 200
        assert len(resp.json()) == 25

    def test_the_default_is_still_200(self, client, auth, many_alerts):
        """The pre-existing page size is the default, not a behaviour change."""
        resp = client.get(f"/api/alerts?camera_id={many_alerts.id}", headers=auth)
        assert resp.status_code == 200
        assert len(resp.json()) == 200

    def test_the_limit_applies_after_the_filters(self, client, auth, many_alerts):
        """Ordering of filter-then-limit is what makes the per-camera view correct."""
        resp = client.get(f"/api/alerts?camera_id={many_alerts.id}&severity=LOW&limit=10", headers=auth)
        assert resp.status_code == 200
        body = resp.json()
        assert len(body) == 10
        assert all(a["camera_id"] == many_alerts.id for a in body)
