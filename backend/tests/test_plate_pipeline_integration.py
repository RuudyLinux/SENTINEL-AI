"""The V2 plate pipeline end to end.

Drives the real worker._run_anpr chain and checks the stages compose:

    vehicle detection -> ByteTrack id -> plate localization -> OCR
      -> vote/aggregate -> vehicle identity -> sighting row -> cross-camera route

Only the plate detector and the OCR engine are faked (real EasyOCR on a
synthetic frame measures nothing). Cropping, perspective correction,
variants, full-frame offsets, whole-crop fallback, track association,
voting, the persistence gate, sighting dedup and routes are real.

Faking _read_plate_for_track instead would fake production code and let the
original bug (OCR handed the whole vehicle crop) pass.
TestOcrReceivesThePlateNotTheVehicle checks the image OCR actually got.
"""
import itertools
import asyncio
from datetime import datetime

import numpy as np
import pytest

from app import models
from app.pipeline import anpr, correlate, plate_tracker, worker


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


# plate position inside the vehicle crop, and the vehicle box origin in the
# frame. shared by the stub detector and the expected full-frame plate_bbox
_PLATE_BOX_IN_CROP = (40, 120, 150, 150)
_VEHICLE_ORIGIN = (10, 10)


def _stub_detection_and_ocr(monkeypatch, text: str, confidence: float, seen=None):
    """Stub only the plate detector and the OCR engine.

    Not _read_plate_for_track: that's production code (cropping, warping,
    variants, offsets, fallback), and stubbing it would let OCR get the
    whole vehicle crop unnoticed.

    `seen` collects the images OCR received, so tests can check what was read.
    """
    x1, y1, x2, y2 = _PLATE_BOX_IN_CROP
    box = worker.plate_detector.PlateBox(
        x1=x1, y1=y1, x2=x2, y2=y2, confidence=0.7, source="heuristic",
    )
    monkeypatch.setattr(worker.plate_detector, "detect_plates", lambda crop: [box])

    def _read_structured(variants):
        if seen is not None:
            seen.append(variants)
        return anpr.OcrRead(
            raw=text, normalized=text, confidence=confidence,
            variant=variants[0][0] if variants else "",
        )

    monkeypatch.setattr(worker, "read_plate_structured", _read_structured)


def _drive(db_session, monkeypatch, camera, frame, reads, track_id: int = 284):
    """Run (text, confidence) OCR results through the real pipeline, one per
    cycle, as if the vehicle stayed in frame. Only detector and OCR are
    stubbed (_stub_detection_and_ocr).
    """
    # Always re-OCR, so every scripted read is actually consumed rather than
    # being skipped by the stability throttle.
    monkeypatch.setattr(worker.settings, "plate_reverify_seconds", 0.0)
    monkeypatch.setattr(worker.settings, "plate_sighting_refresh_seconds", 0.0)
    monkeypatch.setattr(worker, "_save_snapshot", lambda f, prefix: f"/evidence/{prefix}.jpg")

    results = []
    for text, confidence in reads:
        _stub_detection_and_ocr(monkeypatch, text, confidence)
        det_row = _detection_row(db_session, camera, track_id=str(track_id))
        results.append(asyncio.run(worker._run_anpr(
            db_session, _detection(track_id), det_row, frame,
            str(camera.id), str(camera.camera_code), None,
        )))
    db_session.commit()
    return results


# Unique plate per test. These assert on row counts per plate and the suite
# shares one DB, so a fixed literal depends on whether another test made that
# plate first; --random-order caught it. 3xxx range to stay clear of
# GJ05AB1234, shaped to match PLATE_RE.
_plate_counter = itertools.count(3000)


def _fresh_plate() -> str:
    return f"GJ05AB{next(_plate_counter):04d}"


class TestPipelineCoherence:
    def test_the_full_chain_produces_one_vehicle_and_one_sighting(self, db_session, monkeypatch, frame):
        """Four frames of one tracked vehicle are one sighting of one vehicle,
        not four vehicles or four hops."""
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
        # localizer works in crop coords; stored bbox is full frame, offset by
        # the vehicle origin (10, 10)
        assert sighting.plate_bbox == [50.0, 130.0, 160.0, 160.0]
        assert sighting.vehicle_class == "car"
        assert all(r[0] is not None for r in results)

    def test_a_single_bad_frame_cannot_replace_an_established_plate(self, db_session, monkeypatch, frame):
        """Checked on the persisted row, not the in-memory tally."""
        camera = _camera(db_session, "INT-C2")
        plate = _fresh_plate()
        misread = _fresh_plate()  # a DIFFERENT valid plate: the outlier read
        _drive(db_session, monkeypatch, camera, frame, [
            (plate, 0.72), (plate, 0.91), (plate, 0.94),
            (misread, 0.95),  # one high-confidence misread
        ])

        # by camera too, track ids are only unique per camera
        plates = db_session.query(models.Plate).filter(
            models.Plate.camera_id == camera.id, models.Plate.track_id == "284",
        ).all()
        assert len(plates) == 1
        assert plates[0].plate_text_normalized == plate
        assert db_session.query(models.Vehicle).filter(
            models.Vehicle.plate_text == misread
        ).first() is None, "a single outlier read must not create a phantom vehicle"

    def test_tracking_continues_while_the_vehicle_stays_visible(self, db_session, monkeypatch, frame):
        """Extended, not duplicated, while the car stays; last_seen advances."""
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
        """"Track 284 is GJ05AB1234" is a stored, queryable fact."""
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
        """The plate is the join key: the second camera extends the same
        vehicle's journey."""
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
        # just written, so live, and the score adds up
        assert summary["is_live"] is True
        assert summary["risk_score"] == sum(f["points"] for f in summary["risk_factors"])

    def test_a_route_carries_no_position_between_cameras(self, db_session, monkeypatch, frame):
        """Every hop is an observation at a camera. No interpolated position,
        heading or speed implying we know what happened in between."""
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
        """No track id yet on an object's first frames; the read still goes
        through the old whole-crop path instead of being dropped."""
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
        """PLATE_PIPELINE_V2=false: one Plate row per passing frame again."""
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


class TestOcrReceivesThePlateNotTheVehicle:
    """OCR must get a plate region, not the whole vehicle crop (bumper
    stickers, dealer badges and all). That lives in _read_plate_for_track,
    which is why the stubs sit on either side of it.
    """

    def test_ocr_is_given_the_localized_plate_region(self, db_session, monkeypatch, frame):
        camera = _camera(db_session, "INT-W1")
        seen: list = []
        _stub_detection_and_ocr(monkeypatch, _fresh_plate(), 0.9, seen=seen)
        monkeypatch.setattr(worker, "_save_snapshot", lambda f, prefix: "/evidence/x.jpg")

        det_row = _detection_row(db_session, camera)
        asyncio.run(worker._run_anpr(
            db_session, _detection(), det_row, frame, str(camera.id), str(camera.camera_code), None,
        ))

        assert seen, "OCR was never called"
        variants = seen[0]
        assert variants, "OCR was handed no image at all"
        _name, image = variants[0]
        # Shape, not size: the plate crop gets upscaled and can end up wider
        # than the vehicle crop. It can't be vehicle-shaped though: vehicle
        # 190x170 (~1.1), plate 110x30 (~3.7), upscaling keeps the ratio.
        vehicle_height, vehicle_width = frame[10:180, 10:200].shape[:2]
        vehicle_aspect = vehicle_width / vehicle_height
        image_aspect = image.shape[1] / image.shape[0]
        assert image_aspect > 2.0, (
            f"OCR received an image of aspect {image_aspect:.2f} (vehicle crop is "
            f"{vehicle_aspect:.2f}) — that is the whole vehicle, not a plate region"
        )

    def test_a_localization_miss_falls_back_to_the_whole_vehicle_crop(
        self, db_session, monkeypatch, frame,
    ):
        """A miss degrades the read, doesn't drop it, and stores plate_bbox =
        NULL rather than a box the read didn't come from."""
        camera = _camera(db_session, "INT-W2")
        plate = _fresh_plate()
        seen: list = []
        monkeypatch.setattr(worker.plate_detector, "detect_plates", lambda crop: [])

        def _read_structured(variants):
            seen.append(variants)
            return anpr.OcrRead(raw=plate, normalized=plate, confidence=0.9, variant="clahe")

        monkeypatch.setattr(worker, "read_plate_structured", _read_structured)
        monkeypatch.setattr(worker, "_save_snapshot", lambda f, prefix: "/evidence/x.jpg")
        monkeypatch.setattr(worker.settings, "plate_min_observations", 1)

        det_row = _detection_row(db_session, camera)
        _vehicle, plate_row, _snap = asyncio.run(worker._run_anpr(
            db_session, _detection(), det_row, frame, str(camera.id), str(camera.camera_code), None,
        ))
        db_session.commit()

        vehicle_height, vehicle_width = frame[10:180, 10:200].shape[:2]
        _name, image = seen[0][0]
        assert (image.shape[0], image.shape[1]) == (vehicle_height, vehicle_width), (
            "with no plate region found, OCR must read the whole vehicle crop"
        )
        assert plate_row is not None and plate_row.plate_bbox is None, (
            "a non-localized read records a null plate box, never a fabricated one"
        )

    def test_the_stored_plate_box_is_in_full_frame_coordinates(
        self, db_session, monkeypatch, frame,
    ):
        """Box offset to the frame, or evidence draws the marker in the wrong place."""
        camera = _camera(db_session, "INT-W3")
        plate = _fresh_plate()
        _stub_detection_and_ocr(monkeypatch, plate, 0.9)
        monkeypatch.setattr(worker, "_save_snapshot", lambda f, prefix: "/evidence/x.jpg")
        monkeypatch.setattr(worker.settings, "plate_min_observations", 1)

        det_row = _detection_row(db_session, camera)
        _vehicle, plate_row, _snap = asyncio.run(worker._run_anpr(
            db_session, _detection(), det_row, frame, str(camera.id), str(camera.camera_code), None,
        ))
        db_session.commit()

        x1, y1, x2, y2 = _PLATE_BOX_IN_CROP
        origin_x, origin_y = _VEHICLE_ORIGIN
        assert plate_row.plate_bbox == [
            x1 + origin_x, y1 + origin_y, x2 + origin_x, y2 + origin_y,
        ]


class TestPersistenceScenarios:
    """The persistence gate, checked on persisted rows.

    A plate is trusted once enough frames agree; the first passing read used
    to create a vehicle outright. A car seen in one cycle still gets its one
    read kept.
    """

    def _sighting(self, db_session, plate_text):
        return (
            db_session.query(models.Plate)
            .filter(models.Plate.plate_text_normalized == plate_text)
            .one_or_none()
        )

    def test_one_strong_read_is_recorded_but_not_trusted(self, db_session, monkeypatch, frame):
        """One 0.95 read is kept but not corroborated, so it goes to review
        instead of becoming a settled identity."""
        monkeypatch.setattr(worker.settings, "plate_min_observations", 2)
        camera = _camera(db_session, "INT-P1")
        plate = _fresh_plate()
        _drive(db_session, monkeypatch, camera, frame, [(plate, 0.95)])

        sighting = self._sighting(db_session, plate)
        assert sighting is not None, "a real observation must never be silently dropped"
        assert sighting.corroborated is False
        assert sighting.review_status == "pending_review", (
            "an uncorroborated read is flagged for a human however confident it was"
        )

    def test_two_agreeing_reads_are_trusted(self, db_session, monkeypatch, frame):
        monkeypatch.setattr(worker.settings, "plate_min_observations", 2)
        camera = _camera(db_session, "INT-P2")
        plate = _fresh_plate()
        _drive(db_session, monkeypatch, camera, frame, [(plate, 0.82), (plate, 0.88)])

        sighting = self._sighting(db_session, plate)
        assert sighting.corroborated is True
        assert sighting.review_status == "auto_accepted"
        assert sighting.reads_count == 2

    def test_two_conflicting_reads_reach_no_consensus(self, db_session, monkeypatch, frame):
        """Two different plates on one track isn't two observations; neither
        gets promoted."""
        monkeypatch.setattr(worker.settings, "plate_min_observations", 2)
        camera = _camera(db_session, "INT-P3")
        first, second = _fresh_plate(), _fresh_plate()
        _drive(db_session, monkeypatch, camera, frame, [(first, 0.90), (second, 0.90)])

        rows = (
            db_session.query(models.Plate)
            .filter(models.Plate.plate_text_normalized.in_([first, second]))
            .all()
        )
        assert len(rows) == 1, "one track is still one sighting row, not two"
        assert rows[0].corroborated is False
        assert rows[0].review_status == "pending_review"

    def test_three_reads_with_a_clear_winner_are_trusted(self, db_session, monkeypatch, frame):
        monkeypatch.setattr(worker.settings, "plate_min_observations", 2)
        camera = _camera(db_session, "INT-P4")
        winner, outlier = _fresh_plate(), _fresh_plate()
        _drive(db_session, monkeypatch, camera, frame, [
            (winner, 0.88), (outlier, 0.55), (winner, 0.91),
        ])

        sighting = self._sighting(db_session, winner)
        assert sighting is not None and sighting.corroborated is True
        assert sighting.confidence == pytest.approx(0.91), "the sighting records the PEAK read"
        assert self._sighting(db_session, outlier) is None, (
            "the out-voted read must not get a sighting row of its own"
        )

    def test_require_consensus_withholds_an_uncorroborated_read_entirely(
        self, db_session, monkeypatch, frame,
    ):
        """Strict mode: nothing uncorroborated at all, not even pending_review."""
        monkeypatch.setattr(worker.settings, "plate_min_observations", 2)
        monkeypatch.setattr(worker.settings, "plate_require_consensus", True)
        camera = _camera(db_session, "INT-P5")
        plate = _fresh_plate()
        _drive(db_session, monkeypatch, camera, frame, [(plate, 0.95)])

        assert self._sighting(db_session, plate) is None
        assert db_session.query(models.Vehicle).filter(
            models.Vehicle.plate_text == plate
        ).one_or_none() is None, "strict mode must not create a vehicle identity either"

    def test_min_observations_one_restores_the_previous_behaviour(
        self, db_session, monkeypatch, frame,
    ):
        """One env var brings back persisting on the first passing read."""
        monkeypatch.setattr(worker.settings, "plate_min_observations", 1)
        camera = _camera(db_session, "INT-P6")
        plate = _fresh_plate()
        _drive(db_session, monkeypatch, camera, frame, [(plate, 0.95)])

        sighting = self._sighting(db_session, plate)
        assert sighting is not None
        assert sighting.corroborated is True
        assert sighting.review_status == "auto_accepted"

    def test_repeated_low_confidence_garbage_never_persists(
        self, db_session, monkeypatch, frame,
    ):
        """Five identical reads under the floor are five failures, not agreement."""
        monkeypatch.setattr(worker.settings, "plate_min_observations", 2)
        camera = _camera(db_session, "INT-P7")
        plate = _fresh_plate()
        _drive(db_session, monkeypatch, camera, frame, [(plate, 0.05)] * 5)

        assert self._sighting(db_session, plate) is None
        assert db_session.query(models.Vehicle).filter(
            models.Vehicle.plate_text == plate
        ).one_or_none() is None

    def test_a_plate_shaped_read_with_an_impossible_state_code_never_persists(
        self, db_session, monkeypatch, frame,
    ):
        """QQ00QQ0000 passes the regex at high confidence; only the state
        code check stops it."""
        monkeypatch.setattr(worker.settings, "plate_min_observations", 2)
        camera = _camera(db_session, "INT-P8")
        _drive(db_session, monkeypatch, camera, frame, [("QQ00QQ0000", 0.97)] * 3)

        assert self._sighting(db_session, "QQ00QQ0000") is None
        assert db_session.query(models.Vehicle).filter(
            models.Vehicle.plate_text == "QQ00QQ0000"
        ).one_or_none() is None


class TestConsensusStateIsolation:
    """Votes never leak between vehicles: same track id on two cameras, or
    a track id reused after the first car left."""

    def test_the_same_track_id_on_two_cameras_is_two_vehicles(
        self, db_session, monkeypatch, frame,
    ):
        """Track ids are per model instance, one per camera, so 284 on C1 and
        284 on C2 are unrelated."""
        monkeypatch.setattr(worker.settings, "plate_min_observations", 2)
        first_camera = _camera(db_session, "INT-I1")
        second_camera = _camera(db_session, "INT-I2")
        first, second = _fresh_plate(), _fresh_plate()

        _drive(db_session, monkeypatch, first_camera, frame, [(first, 0.9)], track_id=284)
        _drive(db_session, monkeypatch, second_camera, frame, [(second, 0.9)], track_id=284)

        for plate in (first, second):
            row = (
                db_session.query(models.Plate)
                .filter(models.Plate.plate_text_normalized == plate).one()
            )
            assert row.corroborated is False, (
                "one read on each camera must not add up to consensus across them"
            )
        assert plate_tracker.consensus(str(first_camera.id), "284").text == first
        assert plate_tracker.consensus(str(second_camera.id), "284").text == second

    def test_a_reused_track_id_does_not_inherit_the_previous_vehicles_votes(
        self, db_session, monkeypatch, frame,
    ):
        """A reissued track id starts from nothing; the TTL prune makes sure."""
        monkeypatch.setattr(worker.settings, "plate_min_observations", 2)
        camera = _camera(db_session, "INT-I3")
        departed, arrived = _fresh_plate(), _fresh_plate()

        _drive(db_session, monkeypatch, camera, frame, [(departed, 0.9)], track_id=284)

        # first vehicle leaves and its track is pruned. aged directly, not by
        # sleeping: monotonic is ~15.6ms granular on Windows
        state = plate_tracker.get(str(camera.id), "284")
        state.last_seen_mono -= worker.settings.plate_track_ttl_seconds + 1.0
        plate_tracker.touch(str(camera.id), "999")  # any touch triggers the sweep
        assert plate_tracker.get(str(camera.id), "284") is None, "precondition: pruned"

        # A different vehicle is now issued the same id.
        _drive(db_session, monkeypatch, camera, frame, [(arrived, 0.9)], track_id=284)

        result = plate_tracker.consensus(str(camera.id), "284")
        assert result.text == arrived
        assert result.total_observations == 1, (
            "the new vehicle must not inherit the departed vehicle's observation"
        )
        assert result.competing_text is None

    def test_stopping_a_camera_drops_its_consensus_state(self, db_session, monkeypatch, frame):
        """release_camera runs with release_model, which restarts ByteTrack's
        ids, so votes go at the same time or a restarted camera's track 1
        inherits the old track 1."""
        monkeypatch.setattr(worker.settings, "plate_min_observations", 2)
        camera = _camera(db_session, "INT-I4")
        plate = _fresh_plate()
        _drive(db_session, monkeypatch, camera, frame, [(plate, 0.9)], track_id=1)

        assert plate_tracker.consensus(str(camera.id), "1") is not None
        plate_tracker.release_camera(str(camera.id))
        assert plate_tracker.consensus(str(camera.id), "1") is None
