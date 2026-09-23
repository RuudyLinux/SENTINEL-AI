"""V2 Phase 1/2 — vehicle sightings and cross-camera route reconstruction.

Locks down the fix for the biggest data-quality problem in the pre-V2 pipeline:
a Plate row was inserted on EVERY OCR frame, and `get_route` builds a vehicle's
journey from those rows — so a vehicle stopped at one junction produced dozens
of identical route hops between a camera and itself.
"""
import asyncio
from datetime import datetime, timedelta

import pytest

from app import models
from app.pipeline import correlate


def _camera(db, code: str, lat: float = 23.0, lng: float = 72.0) -> models.Camera:
    camera = models.Camera(
        camera_code=code, name=f"Camera {code}", location=f"Junction {code}",
        lat=lat, lng=lng, source_type="video_file", source_uri="test.mp4",
    )
    db.add(camera)
    db.flush()
    return camera


def _vehicle(db, plate: str) -> models.Vehicle:
    vehicle = models.Vehicle(plate_text=plate, plate_confidence=0.9)
    db.add(vehicle)
    db.flush()
    return vehicle


def _sighting(db, vehicle, camera, at, *, last_seen=None, confidence=0.9, track_id="284", reads=3):
    plate = models.Plate(
        vehicle_id=vehicle.id, camera_id=camera.id, plate_text_normalized=vehicle.plate_text,
        confidence=confidence, timestamp=at, last_seen=last_seen or at,
        track_id=track_id, reads_count=reads, vehicle_class="car",
    )
    db.add(plate)
    db.flush()
    return plate


class TestSightingUpsert:
    def test_first_read_creates_a_sighting_row(self, db_session):
        camera = _camera(db_session, "V2-C1")
        # Not "GJ05AB1234": that literal is also used by the real pipeline in
        # test_plate_pipeline_integration.py, which runs earlier in the full
        # suite and persists a real Vehicle row for it — colliding here since
        # Vehicle.plate_text became unique (BUG-1 fix, 10/10 debugging pass).
        # Nothing in this test asserts on the specific plate text value.
        vehicle = _vehicle(db_session, "GJ05AB1230")
        detection = models.Detection(camera_id=camera.id, cls="car", confidence=0.8, bbox=[0, 0, 10, 10])
        db_session.add(detection)
        db_session.flush()

        plate = asyncio.run(correlate.upsert_plate_sighting(
            db_session, vehicle=vehicle, camera_id=camera.id, track_id="284",
            detection_id=detection.id, raw_text="GJ 05 AB 1234", normalized_text="GJ05AB1234",
            confidence=0.72, reads_count=1, vehicle_class="car", detection_confidence=0.81,
            vehicle_bbox=[10.0, 20.0, 110.0, 90.0], plate_bbox=[40.0, 70.0, 95.0, 85.0],
            snapshot_path="/tmp/snap.jpg", source_timestamp=None, existing_plate_id=None,
        ))

        assert plate.track_id == "284"
        assert plate.confidence == pytest.approx(0.72)
        assert plate.reads_count == 1
        assert plate.vehicle_class == "car"
        assert plate.plate_bbox == [40.0, 70.0, 95.0, 85.0]
        assert plate.last_seen is not None

    def test_same_track_updates_the_row_instead_of_inserting_another(self, db_session):
        """The core fix: one sighting per (camera, track), not one per frame."""
        camera = _camera(db_session, "V2-C2")
        vehicle = _vehicle(db_session, "GJ05AB9999")
        detection = models.Detection(camera_id=camera.id, cls="car", confidence=0.8, bbox=[0, 0, 10, 10])
        db_session.add(detection)
        db_session.flush()

        first = asyncio.run(correlate.upsert_plate_sighting(
            db_session, vehicle=vehicle, camera_id=camera.id, track_id="284",
            detection_id=detection.id, raw_text="GJ05AB9999", normalized_text="GJ05AB9999",
            confidence=0.72, reads_count=1, vehicle_class="car", detection_confidence=0.8,
            vehicle_bbox=None, plate_bbox=None, snapshot_path="/tmp/a.jpg",
            source_timestamp=None, existing_plate_id=None,
        ))
        second = asyncio.run(correlate.upsert_plate_sighting(
            db_session, vehicle=vehicle, camera_id=camera.id, track_id="284",
            detection_id=detection.id, raw_text="GJ05AB9999", normalized_text="GJ05AB9999",
            confidence=0.94, reads_count=4, vehicle_class="car", detection_confidence=0.8,
            vehicle_bbox=None, plate_bbox=[1.0, 2.0, 3.0, 4.0], snapshot_path=None,
            source_timestamp=None, existing_plate_id=first.id,
        ))
        db_session.commit()

        assert second.id == first.id
        rows = db_session.query(models.Plate).filter(models.Plate.vehicle_id == vehicle.id).all()
        assert len(rows) == 1, "a vehicle sitting in frame must not accumulate duplicate sightings"
        assert second.confidence == pytest.approx(0.94)
        assert second.reads_count == 4
        assert second.plate_bbox == [1.0, 2.0, 3.0, 4.0]

    def test_confidence_only_ever_moves_up(self, db_session):
        """A later, worse read of an already well-read plate must not degrade
        the recorded quality of the sighting."""
        camera = _camera(db_session, "V2-C3")
        vehicle = _vehicle(db_session, "GJ05AB7777")
        first = asyncio.run(correlate.upsert_plate_sighting(
            db_session, vehicle=vehicle, camera_id=camera.id, track_id="7",
            detection_id=None, raw_text="", normalized_text="GJ05AB7777",
            confidence=0.94, reads_count=4, vehicle_class="car", detection_confidence=0.8,
            vehicle_bbox=None, plate_bbox=None, snapshot_path=None,
            source_timestamp=None, existing_plate_id=None,
        ))
        updated = asyncio.run(correlate.upsert_plate_sighting(
            db_session, vehicle=vehicle, camera_id=camera.id, track_id="7",
            detection_id=None, raw_text="", normalized_text="GJ05AB7777",
            confidence=0.41, reads_count=5, vehicle_class="car", detection_confidence=0.8,
            vehicle_bbox=None, plate_bbox=None, snapshot_path=None,
            source_timestamp=None, existing_plate_id=first.id,
        ))
        assert updated.confidence == pytest.approx(0.94)

    def test_an_existing_snapshot_is_never_dropped(self, db_session):
        """Evidence already captured for this sighting must survive an update
        that had no new snapshot to offer."""
        camera = _camera(db_session, "V2-C4")
        vehicle = _vehicle(db_session, "GJ05AB5555")
        first = asyncio.run(correlate.upsert_plate_sighting(
            db_session, vehicle=vehicle, camera_id=camera.id, track_id="9",
            detection_id=None, raw_text="", normalized_text="GJ05AB5555",
            confidence=0.8, reads_count=1, vehicle_class="car", detection_confidence=0.8,
            vehicle_bbox=None, plate_bbox=None, snapshot_path="/evidence/real.jpg",
            source_timestamp=None, existing_plate_id=None,
        ))
        updated = asyncio.run(correlate.upsert_plate_sighting(
            db_session, vehicle=vehicle, camera_id=camera.id, track_id="9",
            detection_id=None, raw_text="", normalized_text="GJ05AB5555",
            confidence=0.9, reads_count=2, vehicle_class="car", detection_confidence=0.8,
            vehicle_bbox=None, plate_bbox=None, snapshot_path=None,
            source_timestamp=None, existing_plate_id=first.id,
        ))
        assert updated.snapshot_path == "/evidence/real.jpg"


class TestRouteReconstruction:
    def test_route_is_ordered_across_cameras(self, db_session):
        base = datetime(2026, 3, 1, 10, 21, 4)
        vehicle = _vehicle(db_session, "GJ05RT0001")
        cameras = [_camera(db_session, f"RT-C{i}", lat=23.0 + i * 0.01) for i in range(3)]
        # Inserted out of chronological order on purpose — ordering must come
        # from the timestamps, not insertion order.
        _sighting(db_session, vehicle, cameras[2], base + timedelta(minutes=14))
        _sighting(db_session, vehicle, cameras[0], base)
        _sighting(db_session, vehicle, cameras[1], base + timedelta(minutes=7))
        db_session.commit()

        route = correlate.get_route(db_session, vehicle.id)
        assert [hop["camera_code"] for hop in route] == ["RT-C0", "RT-C1", "RT-C2"]

    def test_consecutive_sightings_on_one_camera_collapse_into_one_hop(self, db_session):
        """A vehicle recognized repeatedly at one junction is ONE hop with a
        dwell time — not N hops between a camera and itself."""
        base = datetime(2026, 3, 1, 10, 21, 4)
        vehicle = _vehicle(db_session, "GJ05RT0002")
        camera_a = _camera(db_session, "RT-D1")
        camera_b = _camera(db_session, "RT-D2")
        _sighting(db_session, vehicle, camera_a, base, last_seen=base + timedelta(seconds=15), confidence=0.7)
        _sighting(db_session, vehicle, camera_a, base + timedelta(seconds=20),
                  last_seen=base + timedelta(seconds=40), confidence=0.93)
        _sighting(db_session, vehicle, camera_b, base + timedelta(minutes=6))
        db_session.commit()

        route = correlate.get_route(db_session, vehicle.id)
        assert [hop["camera_code"] for hop in route] == ["RT-D1", "RT-D2"]
        collapsed = route[0]
        assert collapsed["dwell_seconds"] == pytest.approx(40.0)
        assert collapsed["confidence"] == pytest.approx(0.93), "the best read of the hop is kept"
        assert collapsed["reads_count"] == 6

    def test_a_genuine_return_to_a_camera_stays_a_separate_hop(self, db_session):
        """Collapsing must only merge CONSECUTIVE rows. A vehicle that left and
        came back really did pass that camera twice, and erasing that would
        destroy real movement information."""
        base = datetime(2026, 3, 1, 10, 0, 0)
        vehicle = _vehicle(db_session, "GJ05RT0003")
        camera_a = _camera(db_session, "RT-E1")
        camera_b = _camera(db_session, "RT-E2")
        _sighting(db_session, vehicle, camera_a, base)
        _sighting(db_session, vehicle, camera_b, base + timedelta(minutes=5))
        _sighting(db_session, vehicle, camera_a, base + timedelta(minutes=12))
        db_session.commit()

        route = correlate.get_route(db_session, vehicle.id)
        assert [hop["camera_code"] for hop in route] == ["RT-E1", "RT-E2", "RT-E1"]

    def test_hops_carry_the_coordinates_the_map_needs(self, db_session):
        """The journey/map UI must not have to cross-reference /api/cameras per
        hop to draw the route line."""
        vehicle = _vehicle(db_session, "GJ05RT0004")
        camera = _camera(db_session, "RT-F1", lat=23.0225, lng=72.5714)
        _sighting(db_session, vehicle, camera, datetime(2026, 3, 1, 10, 0, 0))
        db_session.commit()

        hop = correlate.get_route(db_session, vehicle.id)[0]
        assert hop["lat"] == pytest.approx(23.0225)
        assert hop["lng"] == pytest.approx(72.5714)
        assert hop["location"] == "Junction RT-F1"
        assert hop["track_id"] == "284"

    def test_preserves_the_pre_v2_sighting_contract(self, db_session):
        """schemas.SightingOut and every existing caller read these exact keys —
        V2 adds fields, it must not rename the ones already relied on."""
        vehicle = _vehicle(db_session, "GJ05RT0005")
        camera = _camera(db_session, "RT-G1")
        _sighting(db_session, vehicle, camera, datetime(2026, 3, 1, 10, 0, 0))
        db_session.commit()

        hop = correlate.get_route(db_session, vehicle.id)[0]
        for key in ("camera_id", "camera_code", "camera_name", "timestamp", "confidence", "snapshot_path"):
            assert key in hop

    def test_a_vehicle_with_no_sightings_has_an_empty_route(self, db_session):
        vehicle = _vehicle(db_session, "GJ05RT0006")
        db_session.commit()
        assert correlate.get_route(db_session, vehicle.id) == []


class TestTrackUpsert:
    def test_creates_then_updates_one_row_per_camera_track(self, db_session):
        camera = _camera(db_session, "TRK-1")
        at = datetime(2026, 3, 1, 10, 0, 0)

        first = asyncio.run(correlate.upsert_track(db_session, camera.id, 284, "car", at))
        second = asyncio.run(correlate.upsert_track(
            db_session, camera.id, 284, "car", at + timedelta(seconds=5),
        ))
        db_session.commit()

        assert second.id == first.id
        assert second.detection_count == 2
        assert db_session.query(models.Track).filter(models.Track.camera_id == camera.id).count() == 1

    def test_the_same_track_id_on_another_camera_is_another_vehicle(self, db_session):
        """ByteTrack ids are only unique per predictor and detector.py keeps one
        per camera — keying on the id alone would merge two real vehicles."""
        camera_a = _camera(db_session, "TRK-2")
        camera_b = _camera(db_session, "TRK-3")
        at = datetime(2026, 3, 1, 10, 0, 0)
        a = asyncio.run(correlate.upsert_track(db_session, camera_a.id, 284, "car", at))
        b = asyncio.run(correlate.upsert_track(db_session, camera_b.id, 284, "car", at))
        db_session.commit()
        assert a.id != b.id

    def test_identity_is_added_but_never_cleared(self, db_session):
        """A frame where the plate happened not to read must not un-identify a
        vehicle already recognized on this track."""
        camera = _camera(db_session, "TRK-4")
        vehicle = _vehicle(db_session, "GJ05TR0001")
        at = datetime(2026, 3, 1, 10, 0, 0)
        asyncio.run(correlate.upsert_track(db_session, camera.id, 55, "car", at, vehicle_id=vehicle.id, plate_reads=3))
        track = asyncio.run(correlate.upsert_track(db_session, camera.id, 55, "car", at, vehicle_id=None))
        db_session.commit()

        assert track.vehicle_id == vehicle.id
        assert track.plate_reads == 3
