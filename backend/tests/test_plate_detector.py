"""Plate detection: candidate regions, confidence source, quads.

Synthetic crops, no weights, GPU or camera. Checks the geometry the
classical localizer must respect, not a detection rate (that's
tools/anpr_bench.py).
"""
import asyncio

import cv2
import numpy as np
import pytest

from app.config import settings
from app.pipeline import plate_detector, worker
from app.pipeline.anpr import OcrRead



def _vehicle_with_plate(plate_y: int = 150, plate_w: int = 120, plate_h: int = 30) -> np.ndarray:
    """Dark vehicle-ish rectangle with a light high-contrast plate carrying
    glyph-like bars."""
    crop = np.full((220, 300, 3), 60, dtype=np.uint8)
    x = (300 - plate_w) // 2
    cv2.rectangle(crop, (x, plate_y), (x + plate_w, plate_y + plate_h), (235, 235, 235), -1)
    for index in range(6):
        bar_x = x + 6 + index * 18
        cv2.rectangle(crop, (bar_x, plate_y + 6), (bar_x + 8, plate_y + plate_h - 6), (15, 15, 15), -1)
    return crop


class TestDetection:
    def test_a_plate_shaped_region_is_found(self):
        boxes = plate_detector.detect_plates(_vehicle_with_plate())
        assert boxes, "no candidate region found on a synthetic plate"

    def test_candidates_are_ranked_best_first(self):
        boxes = plate_detector.detect_plates(_vehicle_with_plate())
        confidences = [box.confidence for box in boxes]
        assert confidences == sorted(confidences, reverse=True)

    def test_the_candidate_list_is_bounded(self):
        """Each candidate read is a full OCR pass."""
        boxes = plate_detector.detect_plates(_vehicle_with_plate())
        assert len(boxes) <= plate_detector.MAX_CANDIDATES

    def test_boxes_stay_inside_the_crop(self):
        crop = _vehicle_with_plate()
        height, width = crop.shape[:2]
        for box in plate_detector.detect_plates(crop):
            assert 0 <= box.x1 < box.x2 <= width
            assert 0 <= box.y1 < box.y2 <= height

    def test_a_featureless_crop_yields_nothing(self):
        """Nothing found = read the whole crop; inventing a region would send
        OCR somewhere random."""
        assert plate_detector.detect_plates(np.full((200, 300, 3), 128, dtype=np.uint8)) == []

    def test_empty_and_degenerate_input_is_refused(self):
        assert plate_detector.detect_plates(None) == []
        assert plate_detector.detect_plates(np.zeros((0, 0, 3), dtype=np.uint8)) == []
        assert plate_detector.detect_plates(np.zeros((4, 4, 3), dtype=np.uint8)) == []

    @pytest.mark.parametrize("plate_y", [40, 100, 170])
    def test_plates_are_found_at_varying_heights(self, plate_y):
        """Position affects the score but never rejects; truck plates sit higher."""
        assert plate_detector.detect_plates(_vehicle_with_plate(plate_y=plate_y))


class TestConfidenceProvenance:
    def test_classical_detections_are_labelled_heuristic(self):
        """A geometric score isn't a model probability; callers can tell which."""
        for box in plate_detector.detect_plates(_vehicle_with_plate()):
            assert box.source == "heuristic"

    def test_confidence_is_a_real_number_in_range(self):
        for box in plate_detector.detect_plates(_vehicle_with_plate()):
            assert 0.0 <= box.confidence <= 1.0

    def test_a_plate_shaped_region_outscores_a_wrong_shaped_one(self):
        wide = plate_detector.heuristic_score(0, 150, 120, 29, 300, 220)   # aspect ~4.1
        square = plate_detector.heuristic_score(0, 150, 60, 60, 300, 220)  # aspect 1.0
        assert wide > square

    def test_a_missing_configured_model_falls_back_without_raising(self, monkeypatch):
        """Missing weights take the no-asset path, no crash, no fake model run."""
        plate_detector.get_plate_model.cache_clear()
        try:
            monkeypatch.setattr(settings, "plate_model_name", "definitely-not-here.pt")
            assert plate_detector.get_plate_model() is None
            assert plate_detector.detect_plates(_vehicle_with_plate()), "must still detect classically"
        finally:
            plate_detector.get_plate_model.cache_clear()


class TestCropping:
    def test_the_crop_covers_the_box_with_padding(self):
        crop = _vehicle_with_plate()
        box = plate_detector.detect_plates(crop)[0]
        plate_crop = plate_detector.crop_plate(crop, box)
        assert plate_crop is not None
        assert plate_crop.shape[0] >= box.height
        assert plate_crop.shape[1] >= box.width

    def test_a_quad_is_translated_into_the_cropped_image_space(self):
        """The quad is in vehicle-crop coords and must be re-origined after
        cropping, or the warp hits the wrong region."""
        crop = _vehicle_with_plate()
        box = plate_detector.detect_plates(crop)[0]
        if box.quad is None:
            pytest.skip("this candidate carried no rotated rect")
        translated = plate_detector.quad_in_crop(box, crop)
        assert translated is not None
        for (original_x, original_y), (moved_x, moved_y) in zip(box.quad, translated):
            assert moved_x <= original_x and moved_y <= original_y

    def test_no_quad_means_no_translation(self):
        box = plate_detector.PlateBox(0, 0, 10, 10, 0.5, "model", quad=None)
        assert plate_detector.quad_in_crop(box, _vehicle_with_plate()) is None


class TestFullFrameOffsetting:
    """_read_plate_for_track converts crop-space boxes to full frame; get it
    wrong and the marker lands in the wrong place on evidence."""

    def test_the_stored_bbox_is_offset_by_the_vehicle_box_origin(self, monkeypatch):
        box = plate_detector.PlateBox(x1=40, y1=120, x2=150, y2=150, confidence=0.7, source="heuristic")
        monkeypatch.setattr(plate_detector, "detect_plates", lambda crop: [box])
        monkeypatch.setattr(
            plate_detector, "crop_plate", lambda crop, b: np.zeros((30, 110, 3), dtype=np.uint8),
        )
        # A gate-passing read, so the whole-crop fallback does not fire and
        # discard the localized bbox (which is the behavior under test).
        monkeypatch.setattr(
            worker, "read_plate_structured",
            lambda variants: OcrRead(
                raw="GJ05AB1234", normalized="GJ05AB1234", confidence=0.9, variant="clahe",
            ),
        )
        _read, plate_bbox, _crop = asyncio.run(
            worker._read_plate_for_track(
                np.zeros((220, 300, 3), dtype=np.uint8), "C-1", offset_x=10, offset_y=10,
            )
        )
        assert plate_bbox == [50, 130, 160, 160]

    def test_a_localization_miss_records_a_null_bbox_not_a_guess(self, monkeypatch):
        """Whole-crop fallback stores NO plate box. The vehicle box would claim
        a localization that didn't happen."""
        monkeypatch.setattr(plate_detector, "detect_plates", lambda crop: [])
        monkeypatch.setattr(
            worker, "read_plate_structured",
            lambda variants: OcrRead(
                raw="GJ05AB1234", normalized="GJ05AB1234", confidence=0.9, variant="clahe",
            ),
        )
        _read, plate_bbox, plate_crop = asyncio.run(
            worker._read_plate_for_track(
                np.zeros((220, 300, 3), dtype=np.uint8), "C-1", offset_x=10, offset_y=10,
            )
        )
        assert plate_bbox is None
        assert plate_crop is None


def test_plate_model_path_is_resolved_from_the_backend_not_the_database(monkeypatch, tmp_path):
    """DB_PATH can be anywhere (docker volume, test dir), the weights ship
    with the backend. Resolving next to the DB lost the detector whenever
    they were apart."""
    from app.config import BASE_DIR
    weights = BASE_DIR / "qa-plate-probe.pt"
    weights.write_bytes(b"not a real model")
    try:
        monkeypatch.setattr(plate_detector.settings, "db_path", tmp_path / "elsewhere" / "x.db")
        monkeypatch.setattr(plate_detector.settings, "plate_model_name", "qa-plate-probe.pt")
        seen = {}
        monkeypatch.setattr("ultralytics.YOLO", lambda path: seen.setdefault("path", path))
        plate_detector.get_plate_model.cache_clear()
        plate_detector.get_plate_model()
        assert seen.get("path") == str(weights)
    finally:
        weights.unlink()
        plate_detector.get_plate_model.cache_clear()
