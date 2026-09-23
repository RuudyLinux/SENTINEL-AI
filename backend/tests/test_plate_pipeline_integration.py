"""End-to-end coherence of the V2 plate pipeline.

The unit tests cover each stage in isolation. This drives the REAL chain in
`worker._run_anpr` and asserts the stages actually compose:

    vehicle detection -> ByteTrack id -> plate localization -> OCR
      -> vote/aggregate -> vehicle identity -> sighting row -> cross-camera route

Only the two genuinely external things are faked — the plate DETECTOR and the
OCR ENGINE — because a real EasyOCR pass on a synthetic frame measures nothing.
Everything between and after them is the real code: cropping the detected region
out, perspective correction, preprocessing variants, offsetting the box to
full-frame, the whole-crop fallback, track association, voting, the persistence
gate, sighting dedup and route reconstruction.

That boundary is deliberate and is itself under test. Faking
`worker._read_plate_for_track` instead would be faking production code, and the
original defect this pipeline exists to fix — OCR being handed the entire
vehicle crop rather than a plate region — would then pass unnoticed. See
`TestOcrReceivesThePlateNotTheVehicle`, which asserts on the image OCR actually
received.
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


# The plate's position INSIDE the vehicle crop, and the vehicle box's origin in
# the frame. Kept as module constants because two things must agree on them: the
# stubbed detector below, and the full-frame `plate_bbox` the sighting is
# asserted to store.
_PLATE_BOX_IN_CROP = (40, 120, 150, 150)
_VEHICLE_ORIGIN = (10, 10)


def _stub_detection_and_ocr(monkeypatch, text: str, confidence: float, seen=None):
    """Stub the two REAL external dependencies — the plate detector and the OCR
    engine — and nothing else.

    Deliberately NOT stubbing `worker._read_plate_for_track`: that is production
    code, and everything it does (cropping the detected region out, perspective
    correction, building preprocessing variants, offsetting the box to
    full-frame, the whole-crop fallback) would then go uncovered. Stubbing there
    would let the original defect this pipeline exists to fix — OCR being handed
    the entire vehicle crop — pass these tests unnoticed.

    `seen`, when given, collects the images OCR actually received, so a test can
    assert on WHAT was read rather than only on what came back.
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
    """Feed a sequence of (text, confidence) OCR results through the real
    pipeline, one per inference cycle, as if the vehicle stayed in frame.

    Only the detector and the OCR engine are stubbed — see
    `_stub_detection_and_ocr`. Localization geometry, preprocessing, the
    persistence gate, voting and sighting cardinality are all real code here.
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


class TestOcrReceivesThePlateNotTheVehicle:
    """The wiring these tests exist to protect.

    The defect this whole pipeline was built to fix is OCR being handed the
    WHOLE VEHICLE CROP — a car, complete with bumper stickers and dealer badges
    — instead of a plate region. That is a wiring property: it lives between the
    detector and the OCR call, in `_read_plate_for_track`. Stubbing that function
    would make these assertions impossible, which is why the suite stubs the
    detector and the OCR engine on either side of it instead.
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
        # Asserted on SHAPE, not size: the plate crop is upscaled to a readable
        # glyph height before OCR, so it can legitimately end up wider in pixels
        # than the vehicle crop it came from. What it cannot be is vehicle-SHAPED.
        # The vehicle crop is 190x170 (aspect ~1.1); the detected plate region is
        # 110x30 (aspect ~3.7), and upscaling preserves that ratio.
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
        """A miss must degrade the read, never drop it — and the sighting must
        record `plate_bbox = NULL` rather than a box describing a region the
        read did not come from."""
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
        """The localizer works in the vehicle crop's space; the stored box must
        be offset to the frame, or an evidence image draws the plate marker in
        the wrong place."""
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
    """The persistence gate, asserted against PERSISTED ROWS rather than the
    in-memory tally.

    The behavior under test: a plate becomes TRUSTED intelligence only once
    enough independent frames agree on it. Before this gate, the first
    gate-passing read created a durable Vehicle identity — one lucky frame, one
    plate-shaped-but-wrong read clearing the confidence floor, and the system
    held a vehicle that was never there.

    What the gate must NOT do is discard real observations: a vehicle crossing
    frame in a single inference cycle gets exactly one read and will never get
    another.
    """

    def _sighting(self, db_session, plate_text):
        return (
            db_session.query(models.Plate)
            .filter(models.Plate.plate_text_normalized == plate_text)
            .one_or_none()
        )

    def test_one_strong_read_is_recorded_but_not_trusted(self, db_session, monkeypatch, frame):
        """A single 0.95 read is a real observation and is kept — but it is NOT
        corroborated, so it goes to the review queue instead of being presented
        as a settled vehicle identity."""
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
        """Two frames reading two DIFFERENT plates is not two observations of
        one plate — it is a track the system is confused about, and neither
        candidate may be promoted to trusted."""
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
        """Strict mode: the operator has chosen to hold nothing uncorroborated.
        No Vehicle, no Plate — not even a pending_review one."""
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
        """The escape hatch is real: one env var returns the pre-gate behavior
        of persisting a trusted plate on the first passing read."""
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
        """Consistency is not correctness. Five identical reads below the
        confidence floor are five failures, not corroboration."""
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
        """`QQ00QQ0000` satisfies the format regex at high confidence. Only the
        state-code check stops it becoming a vehicle record."""
        monkeypatch.setattr(worker.settings, "plate_min_observations", 2)
        camera = _camera(db_session, "INT-P8")
        _drive(db_session, monkeypatch, camera, frame, [("QQ00QQ0000", 0.97)] * 3)

        assert self._sighting(db_session, "QQ00QQ0000") is None
        assert db_session.query(models.Vehicle).filter(
            models.Vehicle.plate_text == "QQ00QQ0000"
        ).one_or_none() is None


class TestConsensusStateIsolation:
    """Consensus must never leak between vehicles. Two ways it could: the same
    ByteTrack id on two cameras, and a track id reused after the original
    vehicle left."""

    def test_the_same_track_id_on_two_cameras_is_two_vehicles(
        self, db_session, monkeypatch, frame,
    ):
        """ByteTrack ids are only unique per model instance, and detector.py
        keeps one instance PER CAMERA — so track 284 on C1 and track 284 on C2
        are unrelated objects and must not pool votes."""
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
        """A track id that retired and was later reissued to a different vehicle
        must start from nothing. The TTL prune is what guarantees it — ByteTrack
        never announces that a track ended."""
        monkeypatch.setattr(worker.settings, "plate_min_observations", 2)
        camera = _camera(db_session, "INT-I3")
        departed, arrived = _fresh_plate(), _fresh_plate()

        _drive(db_session, monkeypatch, camera, frame, [(departed, 0.9)], track_id=284)

        # The first vehicle leaves: its track goes stale and is pruned. Aged
        # directly rather than by sleeping — time.monotonic() has ~15.6ms
        # granularity on Windows, so a short real sleep can measure as zero.
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
        """`release_camera` is called from `stop_worker` alongside
        `release_model`, which restarts ByteTrack's id sequence — so the votes
        must go at the same moment the ids do, or a restarted camera's track 1
        would inherit the previous session's track 1."""
        monkeypatch.setattr(worker.settings, "plate_min_observations", 2)
        camera = _camera(db_session, "INT-I4")
        plate = _fresh_plate()
        _drive(db_session, monkeypatch, camera, frame, [(plate, 0.9)], track_id=1)

        assert plate_tracker.consensus(str(camera.id), "1") is not None
        plate_tracker.release_camera(str(camera.id))
        assert plate_tracker.consensus(str(camera.id), "1") is None
