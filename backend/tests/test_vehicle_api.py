"""V2 Phase 2 — the vehicle investigation API.

Covers the endpoints the plate-first investigation flow depends on:
plate -> vehicle, vehicle -> summary, vehicle -> journey, vehicle -> raw
sightings. The flow starts from a plate an officer types, so resolving that
plate correctly (and refusing to guess when it does not exist) is the contract.
"""
import uuid
from datetime import datetime, timedelta

import pytest

from app import models
from app.db import SessionLocal


@pytest.fixture
def auth(admin_token):
    return {"Authorization": f"Bearer {admin_token}"}


@pytest.fixture
def seeded_vehicle():
    """A vehicle with a real three-camera journey, written directly so the test
    exercises the API rather than the detection pipeline."""
    suffix = uuid.uuid4().hex[:4].upper()
    plate = f"GJ05VA{suffix}"
    db = SessionLocal()
    try:
        base = datetime.utcnow() - timedelta(hours=2)
        cameras = []
        for i in range(3):
            camera = models.Camera(
                camera_code=f"VA-{suffix}-{i}", name=f"VA cam {i}", location=f"Junction {i}",
                lat=23.02 + i * 0.01, lng=72.57 + i * 0.01,
                source_type="video_file", source_uri="x.mp4",
            )
            db.add(camera)
            cameras.append(camera)
        db.flush()

        vehicle = models.Vehicle(
            plate_text=plate, plate_confidence=0.94, vehicle_type="car",
            first_seen=base, last_seen=base + timedelta(minutes=26),
        )
        db.add(vehicle)
        db.flush()

        for i, camera in enumerate(cameras):
            at = base + timedelta(minutes=13 * i)
            db.add(models.Plate(
                vehicle_id=vehicle.id, camera_id=camera.id, plate_text_normalized=plate,
                confidence=0.9, timestamp=at, last_seen=at + timedelta(seconds=20),
                track_id=str(280 + i), reads_count=4, vehicle_class="car",
            ))
        db.commit()
        yield {"plate": plate, "vehicle_id": vehicle.id, "camera_codes": [c.camera_code for c in cameras]}
    finally:
        db.close()


class TestResolveByPlate:
    def test_resolves_a_recognized_plate(self, client, auth, seeded_vehicle):
        resp = client.get(f"/api/vehicles/by-plate/{seeded_vehicle['plate']}", headers=auth)
        assert resp.status_code == 200
        assert resp.json()["id"] == seeded_vehicle["vehicle_id"]

    def test_normalizes_the_input_the_same_way_the_pipeline_does(self, client, auth, seeded_vehicle):
        """An officer types 'GJ 05 AB 1234'; OCR stored 'GJ05AB1234'. Both must
        resolve, or the search silently fails on correct input."""
        spaced = seeded_vehicle["plate"][:2] + " " + seeded_vehicle["plate"][2:4] + " " + seeded_vehicle["plate"][4:]
        resp = client.get(f"/api/vehicles/by-plate/{spaced.lower()}", headers=auth)
        assert resp.status_code == 200
        assert resp.json()["id"] == seeded_vehicle["vehicle_id"]

    def test_an_unknown_plate_is_a_404_not_an_arbitrary_match(self, client, auth):
        """The pre-V2 frontend list-searched and took the first result, which
        quietly returned an unrelated vehicle on a substring hit."""
        resp = client.get("/api/vehicles/by-plate/GJ99ZZ0000", headers=auth)
        assert resp.status_code == 404
        assert "GJ99ZZ0000" in resp.json()["detail"]

    def test_requires_authentication(self, client, seeded_vehicle):
        assert client.get(f"/api/vehicles/by-plate/{seeded_vehicle['plate']}").status_code == 401


class TestVehicleSummary:
    def test_reports_the_real_journey_size_and_current_camera(self, client, auth, seeded_vehicle):
        resp = client.get(f"/api/vehicles/{seeded_vehicle['vehicle_id']}/summary", headers=auth)
        assert resp.status_code == 200
        body = resp.json()

        assert body["total_sightings"] == 3
        assert body["cameras_visited"] == 3
        assert body["current_camera_code"] == seeded_vehicle["camera_codes"][-1]
        assert body["vehicle"]["plate_text"] == seeded_vehicle["plate"]

    def test_a_vehicle_last_seen_hours_ago_is_not_reported_as_live(self, client, auth, seeded_vehicle):
        """The UI must be able to say 'last known position' rather than
        implying the vehicle is on camera right now."""
        body = client.get(f"/api/vehicles/{seeded_vehicle['vehicle_id']}/summary", headers=auth).json()
        assert body["is_live"] is False
        assert body["current_seen_at"] is not None

    def test_carries_an_explainable_risk_score(self, client, auth, seeded_vehicle):
        body = client.get(f"/api/vehicles/{seeded_vehicle['vehicle_id']}/summary", headers=auth).json()
        assert body["risk_score"] == sum(f["points"] for f in body["risk_factors"])
        assert body["risk_severity"] in ("LOW", "MEDIUM", "HIGH", "CRITICAL")

    def test_unknown_vehicle_is_404(self, client, auth):
        assert client.get("/api/vehicles/veh_doesnotexist/summary", headers=auth).status_code == 404


class TestVehicleRoute:
    def test_route_hops_are_ordered_and_carry_map_coordinates(self, client, auth, seeded_vehicle):
        resp = client.get(f"/api/vehicles/{seeded_vehicle['vehicle_id']}/route", headers=auth)
        assert resp.status_code == 200
        sightings = resp.json()["sightings"]

        assert [s["camera_code"] for s in sightings] == seeded_vehicle["camera_codes"]
        assert all(s["lat"] and s["lng"] for s in sightings), "the map must not need a second fetch per hop"
        assert all(s["dwell_seconds"] >= 0 for s in sightings)

    def test_route_still_serves_the_pre_v2_field_shape(self, client, auth, seeded_vehicle):
        """Existing callers read these keys; V2 adds fields, it does not rename."""
        sighting = client.get(
            f"/api/vehicles/{seeded_vehicle['vehicle_id']}/route", headers=auth
        ).json()["sightings"][0]
        for key in ("camera_id", "camera_code", "camera_name", "timestamp", "confidence"):
            assert key in sighting


class TestVehicleSightings:
    def test_returns_the_raw_uncollapsed_records(self, client, auth, seeded_vehicle):
        """`/route` collapses consecutive same-camera hops for readability; an
        investigator still needs the underlying evidence unmodified."""
        resp = client.get(f"/api/vehicles/{seeded_vehicle['vehicle_id']}/sightings", headers=auth)
        assert resp.status_code == 200
        rows = resp.json()

        assert len(rows) == 3
        assert all(row["track_id"] for row in rows)
        assert all(row["reads_count"] == 4 for row in rows)

    def test_unknown_vehicle_is_404(self, client, auth):
        assert client.get("/api/vehicles/veh_nope/sightings", headers=auth).status_code == 404
