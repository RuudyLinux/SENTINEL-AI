"""PATCH /api/cameras/{id} — in-place edit (Model 2/4: camera groups + editable
analytics config). Only fields present in the payload should change."""


def _create_camera(client, admin_token, **overrides):
    payload = {
        "camera_code": "C-PATCH-TEST", "name": "Original Name", "location": "Original Loc",
        "source_type": "mock_vms", "source_uri": "",
        "ai_person": True, "ai_vehicle": True, "ai_anpr": True, "camera_group": "",
    }
    payload.update(overrides)
    resp = client.post("/api/cameras", json=payload, headers={"Authorization": f"Bearer {admin_token}"})
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_patch_updates_only_provided_fields(client, admin_token):
    camera = _create_camera(client, admin_token)
    resp = client.patch(
        f"/api/cameras/{camera['id']}",
        json={"camera_group": "North Zone", "ai_anpr": False},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200, resp.text
    updated = resp.json()
    assert updated["camera_group"] == "North Zone"
    assert updated["ai_anpr"] is False
    # untouched fields survive unchanged
    assert updated["name"] == "Original Name"
    assert updated["location"] == "Original Loc"
    assert updated["ai_person"] is True
    assert updated["ai_vehicle"] is True


def test_patch_requires_auth(client, admin_token):
    camera = _create_camera(client, admin_token, camera_code="C-PATCH-NOAUTH")
    resp = client.patch(f"/api/cameras/{camera['id']}", json={"camera_group": "X"})
    assert resp.status_code == 401


def test_patch_unknown_camera_404s(client, admin_token):
    resp = client.patch(
        "/api/cameras/does-not-exist",
        json={"camera_group": "X"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 404
