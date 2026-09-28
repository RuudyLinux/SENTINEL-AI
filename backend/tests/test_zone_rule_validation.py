"""Zones and rules that can never fire are refused.

1. Unknown camera_id on a zone or zone_id on a rule became a 500 with a raw
   DB error once FKs were enforced (before that, a silent orphan row).
2. Zone coordinates weren't checked. They're frame fractions and
   _bbox_center_in_zone does x1 <= cx <= x2, so an inverted or out-of-range
   box matches nothing and looks configured.
3. rule_type was free text; only three types are evaluated, anything else
   sits in the list doing nothing. A loitering rule without a zone likewise.
4. Zone delete is soft but the list returned every row, so deleted zones
   stayed on the map and in the rules dropdown.
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
        camera_code=f"ZON-{uuid.uuid4().hex[:8]}", name="zone test cam",
        source_type="mock_vms", source_uri="",
    )
    db_session.add(cam)
    db_session.commit()
    return cam


def _zone_payload(camera_id: str, **overrides):
    return {"name": f"zone-{uuid.uuid4().hex[:6]}", "camera_id": camera_id, **overrides}


class TestZoneCreation:
    def test_an_unknown_camera_is_a_404_not_a_500(self, client, auth):
        resp = client.post("/api/zones", json=_zone_payload("no-such-camera"), headers=auth)
        assert resp.status_code == 404

    def test_a_valid_zone_is_created(self, client, auth, camera):
        resp = client.post("/api/zones", json=_zone_payload(camera.id), headers=auth)
        assert resp.status_code == 200
        assert resp.json()["camera_id"] == camera.id

    @pytest.mark.parametrize("box", [
        {"x1": 0.9, "x2": 0.1},           # inverted horizontally
        {"y1": 0.8, "y2": 0.2},           # inverted vertically
        {"x1": 0.5, "x2": 0.5},           # zero width
    ])
    def test_an_empty_or_inverted_box_is_refused(self, client, auth, camera, box):
        resp = client.post("/api/zones", json=_zone_payload(camera.id, **box), headers=auth)
        assert resp.status_code == 400
        assert "never match" in resp.json()["detail"]

    @pytest.mark.parametrize("box", [{"x1": -0.1}, {"x2": 1.5}, {"y2": 42.0}])
    def test_coordinates_outside_the_frame_are_refused(self, client, auth, camera, box):
        assert client.post("/api/zones", json=_zone_payload(camera.id, **box), headers=auth).status_code == 400


class TestZoneListing:
    def test_a_deleted_zone_disappears_from_the_list(self, client, auth, camera):
        zone_id = client.post("/api/zones", json=_zone_payload(camera.id), headers=auth).json()["id"]
        assert client.delete(f"/api/zones/{zone_id}", headers=auth).status_code == 200

        listed = client.get(f"/api/zones?camera_id={camera.id}", headers=auth).json()
        assert zone_id not in [z["id"] for z in listed], (
            "a deleted zone was still offered, including in the rules page's zone picker"
        )

    def test_history_is_still_retrievable(self, client, auth, camera):
        zone_id = client.post("/api/zones", json=_zone_payload(camera.id), headers=auth).json()["id"]
        client.delete(f"/api/zones/{zone_id}", headers=auth)

        listed = client.get(f"/api/zones?camera_id={camera.id}&include_inactive=true", headers=auth).json()
        found = next((z for z in listed if z["id"] == zone_id), None)
        assert found is not None and found["active"] is False

    def test_an_active_zone_is_listed(self, client, auth, camera):
        zone_id = client.post("/api/zones", json=_zone_payload(camera.id), headers=auth).json()["id"]
        listed = client.get(f"/api/zones?camera_id={camera.id}", headers=auth).json()
        assert zone_id in [z["id"] for z in listed]


class TestRuleCreation:
    def test_an_unknown_rule_type_is_refused(self, client, auth):
        resp = client.post(
            "/api/rules", json={"name": "typo rule", "rule_type": "zone_entryy"}, headers=auth
        )
        assert resp.status_code == 400
        assert "rule_type" in resp.json()["detail"]

    def test_an_unknown_zone_is_a_404_not_a_500(self, client, auth):
        resp = client.post(
            "/api/rules",
            json={"name": "ghost rule", "rule_type": "zone_entry", "zone_id": "no-such-zone"},
            headers=auth,
        )
        assert resp.status_code == 404

    def test_a_loitering_rule_without_a_zone_is_refused(self, client, auth):
        resp = client.post("/api/rules", json={"name": "watches nothing", "rule_type": "loitering"}, headers=auth)
        assert resp.status_code == 400
        assert "zone" in resp.json()["detail"].lower()

    def test_a_valid_loitering_rule_is_created(self, client, auth, camera):
        zone_id = client.post("/api/zones", json=_zone_payload(camera.id), headers=auth).json()["id"]
        resp = client.post(
            "/api/rules",
            json={"name": f"loiter-{uuid.uuid4().hex[:6]}", "rule_type": "loitering", "zone_id": zone_id},
            headers=auth,
        )
        assert resp.status_code == 200

    def test_a_watchlist_rule_needs_no_zone(self, client, auth):
        resp = client.post(
            "/api/rules",
            json={"name": f"wl-{uuid.uuid4().hex[:6]}", "rule_type": "watchlist_plate"},
            headers=auth,
        )
        assert resp.status_code == 200
