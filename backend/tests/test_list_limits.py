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


@pytest.fixture
def many_alerts(db_session):
    """More alerts than the endpoint's default page, on one camera.

    Filtering by this camera keeps the assertions independent of whatever
    else the session's database happens to hold.
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
    """`GET /api/alerts` truncated to a hard-coded 200 with no `limit` at all.

    That is not a security hole the way an unbounded `.all()` is — the ceiling
    was always there — but it made the one screen an operator lives in
    un-pageable, and it was the last transactional list not following the
    repo's own bounded-limit rule. The bound is now the same everywhere:
    default 200, `ge=1`, `le=500`.
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
