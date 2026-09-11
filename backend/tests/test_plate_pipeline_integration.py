"""End-to-end coherence of the V2 plate pipeline.

The unit tests cover each stage in isolation. This drives the REAL chain in
`worker._run_anpr` and asserts the stages actually compose:

    vehicle detection -> ByteTrack id -> plate localization -> OCR
      -> vote/aggregate -> vehicle identity -> sighting row -> cross-camera route

Only the two genuinely external things are faked (the OCR engine and the
localizer), because a real EasyOCR pass on a synthetic frame measures nothing.
Everything between them — association, voting, persistence, dedup, route
reconstruction — is the real code.
"""
import itertools
import asyncio
from datetime import datetime

import numpy as np
import pytest

from app import models
from app.pipeline import correlate, plate_tracker, worker


@pytest.fixture(autouse=True)
def _clean_tracker():
    plate_tracker.reset()
    yield
    plate_tracker.reset()


@pytest.fixture
def frame():
    return np.zeros((240, 320, 3), dtype=np.uint8)


def _camera(db_session, code: str, lat: float = 23.0, lng: float = 72.0) -> models.Camera:
    camera = models.Camera(
        camera_code=code, name=f"Camera {code}", location=f"Junction {code}",
        lat=lat, lng=lng, source_type="video_file", source_uri="x.mp4",
    )
    db_session.add(camera)
    db_session.commit()
    db_session.refresh(camera)
    return camera


def _detection_row(db_session, camera, track_id: str = "284") -> models.Detection:
    row = models.Detection(
        camera_id=camera.id, cls="car", confidence=0.88,
        bbox=[10.0, 10.0, 200.0, 180.0], track_id=track_id,
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


def _detection(track_id: int = 284) -> dict:
    return {"cls": "car", "confidence": 0.88, "bbox": [10.0, 10.0, 200.0, 180.0], "track_id": track_id}


def _drive(db_session, monkeypatch, camera, frame, reads, track_id: int = 284):
    """Feed a sequence of (text, confidence) OCR results through the real
    pipeline, one per inference cycle, as if the vehicle stayed in frame."""
    # Localizer returns a plausible plate box so the localized path is exercised.
    monkeypatch.setattr(
        worker.plate_detect, "locate_plate",
        lambda crop: (np.zeros((64, 200), dtype=np.uint8), [40.0, 120.0, 150.0, 150.0]),
    )
    # Always re-OCR, so every scripted read is actually consumed rather than
    # being skipped by the stability throttle.
    monkeypatch.setattr(worker.settings, "plate_reverify_seconds", 0.0)
    monkeypatch.setattr(worker.settings, "plate_sighting_refresh_seconds", 0.0)
    monkeypatch.setattr(worker, "_save_snapshot", lambda f, prefix: f"/evidence/{prefix}.jpg")

    results = []
    for text, confidence in reads:
        monkeypatch.setattr(worker, "read_plate", lambda crop, _t=text, _c=confidence: (_t, _t, _c))
        det_row = _detection_row(db_session, camera, track_id=str(track_id))
        results.append(asyncio.run(worker._run_anpr(
            db_session, _detection(track_id), det_row, frame,
            str(camera.id), str(camera.camera_code), None,
        )))
    db_session.commit()
    return results


# Unique-per-test plate texts. These tests assert on COUNTS of Vehicle/Plate
# rows for a given plate ("exactly one vehicle", "no phantom vehicle"), and the
# suite shares one on-disk database — so a hardcoded literal makes the result
# depend on whether some other test happened to create that plate first. Caught
# by the --random-order gate: under alphabetical ordering these passed purely
# by luck of scheduling. Numbered in the 3xxx range to stay clear of the
# GJ05AB1234 literal other modules use, and shaped to satisfy PLATE_RE.
_plate_counter = itertools.count(3000)


def _fresh_plate() -> str:
    return f"GJ05AB{next(_plate_counter):04d}"


class TestPipelineCoherence:
    def test_the_full_chain_produces_one_vehicle_and_one_sighting(self, db_session, monkeypatch, frame):
        """Four OCR frames of the SAME tracked vehicle are one sighting of one
        vehicle — not four vehicles, and not four route hops."""
        camera = _camera(db_session, "INT-C1")
        plate = _fresh_plate()
        results = _drive(db_session, monkeypatch, camera, frame, [
            (plate, 0.72), (plate, 0.91),
            (plate, 0.94), (plate, 0.89),
        ])

        vehicles = db_session.query(models.Vehicle).filter(models.Vehicle.plate_text == plate).all()
        assert len(vehicles) == 1, "repeated OCR of one vehicle must not create duplicate vehicles"

        plates = db_session.query(models.Plate).filter(models.Plate.vehicle_id == vehicles[0].id).all()
        assert len(plates) == 1, "one sighting per (camera, track), not one per OCR frame"

        sighting = plates[0]
        assert sighting.confidence == pytest.approx(0.94), "the sighting records the PEAK read"
        assert sighting.reads_count == 4, "all four corroborating reads are counted"
        assert sighting.track_id == "284", "the ByteTrack id is carried into the sighting"
        # The localizer works in the VEHICLE CROP's coordinate space; the stored
        # bbox is full-frame, offset by the vehicle box origin (10, 10). Asserted
        # explicitly because a bbox in the wrong space draws a plate marker in
        # the wrong place on an evidence image.
        assert sighting.plate_bbox == [50.0, 130.0, 160.0, 160.0]
        assert sighting.vehicle_class == "car"
        assert all(r[0] is not None for r in results)

    def test_a_single_bad_frame_cannot_replace_an_established_plate(self, db_session, monkeypatch, frame):
        """The headline guarantee, asserted against the PERSISTED row rather
        than the in-memory tally."""
        camera = _camera(db_session, "INT-C2")
        plate = _fresh_plate()
        misread = _fresh_plate()  # a DIFFERENT valid plate: the outlier read
        _drive(db_session, monkeypatch, camera, frame, [
            (plate, 0.72), (plate, 0.91), (plate, 0.94),
            (misread, 0.95),  # one high-confidence misread
        ])

        # Scoped by camera: ByteTrack ids are only unique per camera, so a
        # query on track_id alone would also match another camera's track 284 —
        # exactly the ambiguity the pipeline's (camera, track) keying avoids.
        plates = db_session.query(models.Plate).filter(
            models.Plate.camera_id == camera.id, models.Plate.track_id == "284",
        ).all()
        assert len(plates) == 1
        assert plates[0].plate_text_normalized == plate
        assert db_session.query(models.Vehicle).filter(
            models.Vehicle.plate_text == misread
        ).first() is None, "a single outlier read must not create a phantom vehicle"

    def test_tracking_continues_while_the_vehicle_stays_visible(self, db_session, monkeypatch, frame):
        """The sighting is extended, not duplicated, as the vehicle remains in
        frame — last_seen advances so dwell time stays truthful."""
        camera = _camera(db_session, "INT-C3")
        _drive(db_session, monkeypatch, camera, frame, [("GJ05CD1111", 0.80)])
        first = db_session.query(models.Plate).filter(models.Plate.plate_text_normalized == "GJ05CD1111").one()
        first_seen, first_last = first.timestamp, first.last_seen

        _drive(db_session, monkeypatch, camera, frame, [("GJ05CD1111", 0.93), ("GJ05CD1111", 0.90)])
        db_session.refresh(first)

        assert db_session.query(models.Plate).filter(
            models.Plate.plate_text_normalized == "GJ05CD1111"
        ).count() == 1
        assert first.timestamp == first_seen, "first-seen must not move"
        assert first.last_seen >= first_last, "last-seen must advance while still visible"
        assert first.reads_count == 3

    def test_a_track_row_links_the_bytetrack_id_to_the_vehicle(self, db_session, monkeypatch, frame):
        """models.Track was dead schema before V2. Track 284 IS GJ05AB1234 must
        be a stored, queryable fact."""
        camera = _camera(db_session, "INT-C4")
        _drive(db_session, monkeypatch, camera, frame, [("GJ05EF2222", 0.9), ("GJ05EF2222", 0.92)])
        vehicle = db_session.query(models.Vehicle).filter(models.Vehicle.plate_text == "GJ05EF2222").one()

        asyncio.run(correlate.upsert_track(
            db_session, camera.id, 284, "car", datetime.utcnow(), vehicle_id=vehicle.id, plate_reads=2,
        ))
        db_session.commit()

        track = db_session.query(models.Track).filter(
            models.Track.camera_id == camera.id, models.Track.yolo_track_id == 284,
        ).one()
        assert track.vehicle_id == vehicle.id


class TestCrossCameraRoute:
    def test_the_same_plate_on_two_cameras_is_one_vehicle_with_a_two_hop_journey(
        self, db_session, monkeypatch, frame,
    ):
        """Cross-camera identity: the plate is the join key, so the second
        camera extends the SAME vehicle's journey rather than creating a new
        vehicle."""
        cam_a = _camera(db_session, "INT-R1", lat=23.02, lng=72.57)
        cam_b = _camera(db_session, "INT-R2", lat=23.07, lng=72.65)

        _drive(db_session, monkeypatch, cam_a, frame, [("GJ05GH3333", 0.88)], track_id=11)
        _drive(db_session, monkeypatch, cam_b, frame, [("GJ05GH3333", 0.92)], track_id=77)

        vehicles = db_session.query(models.Vehicle).filter(models.Vehicle.plate_text == "GJ05GH3333").all()
        assert len(vehicles) == 1, "the same plate on two cameras is ONE vehicle"

        route = correlate.get_route(db_session, vehicles[0].id)
        assert [hop["camera_code"] for hop in route] == ["INT-R1", "INT-R2"], "chronological, one hop per camera"
        assert route[0]["lat"] == pytest.approx(23.02)
        assert route[1]["lat"] == pytest.approx(23.07)
        # Distinct track ids: ByteTrack ids are only unique per camera, and the
        # route must preserve which local track each sighting came from.
        assert route[0]["track_id"] == "11"
        assert route[1]["track_id"] == "77"

    def test_the_summary_reports_the_latest_camera_as_current(self, db_session, monkeypatch, frame):
        cam_a = _camera(db_session, "INT-S1")
        cam_b = _camera(db_session, "INT-S2")
        _drive(db_session, monkeypatch, cam_a, frame, [("GJ05IJ4444", 0.9)], track_id=21)
        _drive(db_session, monkeypatch, cam_b, frame, [("GJ05IJ4444", 0.9)], track_id=22)
        vehicle = db_session.query(models.Vehicle).filter(models.Vehicle.plate_text == "GJ05IJ4444").one()

        summary = correlate.get_vehicle_summary(db_session, vehicle.id)

        assert summary["current_camera_code"] == "INT-S2"
        assert summary["cameras_visited"] == 2
        assert summary["total_sightings"] == 2
        assert summary["first_seen"] is not None and summary["last_seen"] is not None
        assert summary["last_seen"] >= summary["first_seen"]
        # Just written, so it is genuinely live — and the score must add up.
        assert summary["is_live"] is True
        assert summary["risk_score"] == sum(f["points"] for f in summary["risk_factors"])

    def test_a_route_carries_no_position_between_cameras(self, db_session, monkeypatch, frame):
        """Honesty check: the route is a reconstructed camera-to-camera journey.
        Every hop must be an OBSERVATION at a camera — there must be no
        interpolated position, heading or speed field implying the system knows
        where the vehicle went in between."""
        camera = _camera(db_session, "INT-H1")
        _drive(db_session, monkeypatch, camera, frame, [("GJ05KL5555", 0.9)], track_id=31)
        vehicle = db_session.query(models.Vehicle).filter(models.Vehicle.plate_text == "GJ05KL5555").one()

        hop = correlate.get_route(db_session, vehicle.id)[0]

        for invented in ("heading", "speed", "bearing", "path", "interpolated", "gps"):
            assert invented not in hop, f"route hop claims '{invented}', which is not observed"
        assert hop["camera_id"] == camera.id
        assert hop["lat"] == camera.lat and hop["lng"] == camera.lng, (
            "a hop's coordinates are the CAMERA's location, not a vehicle position"
        )


class TestLegacyPathPreserved:
    def test_a_detection_with_no_track_id_still_records_a_sighting(self, db_session, monkeypatch, frame):
        """ByteTrack does not assign an id on an object's first frames. That
        read must still be persisted, via the pre-V2 whole-crop path, rather
        than being dropped for lack of a track."""
        camera = _camera(db_session, "INT-L1")
        monkeypatch.setattr(worker, "read_plate", lambda crop: ("GJ05MN6666", "GJ05MN6666", 0.9))
        monkeypatch.setattr(worker, "_save_snapshot", lambda f, prefix: "/evidence/x.jpg")
        det_row = _detection_row(db_session, camera, track_id="")
        detection = {"cls": "car", "confidence": 0.8, "bbox": [10.0, 10.0, 200.0, 180.0], "track_id": None}

        vehicle, plate_row, _ = asyncio.run(worker._run_anpr(
            db_session, detection, det_row, frame, str(camera.id), str(camera.camera_code), None,
        ))
        db_session.commit()

        assert vehicle is not None and plate_row is not None
        assert plate_row.plate_text_normalized == "GJ05MN6666"
        assert plate_row.track_id is None, "no track id is recorded as null, never invented"

    def test_the_v2_switch_restores_per_frame_behaviour(self, db_session, monkeypatch, frame):
        """PLATE_PIPELINE_V2=false is a real escape hatch: back to one Plate row
        per passing OCR frame."""
        camera = _camera(db_session, "INT-L2")
        monkeypatch.setattr(worker.settings, "plate_pipeline_v2", False)
        monkeypatch.setattr(worker, "read_plate", lambda crop: ("GJ05OP7777", "GJ05OP7777", 0.9))
        monkeypatch.setattr(worker, "_save_snapshot", lambda f, prefix: "/evidence/x.jpg")

        for _ in range(3):
            det_row = _detection_row(db_session, camera, track_id="99")
            asyncio.run(worker._run_anpr(
                db_session, _detection(99), det_row, frame, str(camera.id), str(camera.camera_code), None,
            ))
        db_session.commit()

        assert db_session.query(models.Plate).filter(
            models.Plate.plate_text_normalized == "GJ05OP7777"
        ).count() == 3
