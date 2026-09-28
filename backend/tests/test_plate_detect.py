"""Plate localization inside a vehicle crop.

OCR used to get the whole vehicle box. Synthetic crops (bright plate-shaped
patch with stroke-like marks on a dark body): these check the contract
(finds a plate-shaped region, rejects junk, never raises, returns None so the
caller falls back), not accuracy, which needs real frames.
"""
import cv2
import numpy as np
import pytest

from app.config import settings
from app.pipeline import plate_detect


def _vehicle_crop_with_plate(
    width: int = 400, height: int = 300,
    plate_x: int = 120, plate_y: int = 210, plate_w: int = 160, plate_h: int = 40,
) -> np.ndarray:
    """A dark 'vehicle' with a bright, character-bearing plate patch low on it."""
    crop = np.full((height, width, 3), 40, dtype=np.uint8)
    cv2.rectangle(crop, (plate_x, plate_y), (plate_x + plate_w, plate_y + plate_h), (235, 235, 235), -1)
    # Vertical strokes: the glyph edges the morphological close is designed to
    # join into a single plate-shaped blob.
    for i in range(8):
        x = plate_x + 10 + i * 18
        cv2.rectangle(crop, (x, plate_y + 8), (x + 6, plate_y + plate_h - 8), (20, 20, 20), -1)
    return crop


def test_finds_a_plate_shaped_region_on_a_synthetic_vehicle():
    result = plate_detect.locate_plate(_vehicle_crop_with_plate())
    assert result is not None, "a clear, well-lit, correctly-proportioned plate must be located"
    _, bbox = result
    x1, y1, x2, y2 = bbox
    # The located box should substantially overlap the plate we drew, rather
    # than being any arbitrary region that happened to pass the filters.
    assert 90 <= x1 <= 150, f"located box starts at x={x1}, expected near the drawn plate at x=120"
    assert 180 <= y1 <= 230, f"located box starts at y={y1}, expected near the drawn plate at y=210"
    assert x2 > x1 and y2 > y1


def test_located_bbox_is_within_the_crop_bounds():
    """The box gets offset to full frame and drawn on evidence; out of
    bounds would break the image."""
    crop = _vehicle_crop_with_plate()
    _, (x1, y1, x2, y2) = plate_detect.locate_plate(crop)
    h, w = crop.shape[:2]
    assert 0 <= x1 < x2 <= w
    assert 0 <= y1 < y2 <= h


def test_returns_none_on_a_featureless_crop():
    """Flat surface, no plate: None, and the caller reads the whole crop."""
    assert plate_detect.locate_plate(np.full((300, 400, 3), 90, dtype=np.uint8)) is None


def test_returns_none_on_an_empty_or_degenerate_crop():
    """A clamped edge box can give a zero-size crop; must not raise in a worker."""
    assert plate_detect.locate_plate(np.zeros((0, 0, 3), dtype=np.uint8)) is None
    assert plate_detect.locate_plate(None) is None
    assert plate_detect.locate_plate(np.zeros((4, 4, 3), dtype=np.uint8)) is None


def test_rejects_a_region_that_is_the_wrong_shape_for_a_plate():
    """Tall bright panel (window, reflective strip) isn't a plate; the aspect
    filter keeps OCR off car furniture."""
    crop = np.full((300, 400, 3), 40, dtype=np.uint8)
    cv2.rectangle(crop, (150, 60), (200, 240), (235, 235, 235), -1)  # aspect ~0.28
    for i in range(6):
        y = 70 + i * 28
        cv2.rectangle(crop, (158, y), (192, y + 6), (20, 20, 20), -1)
    result = plate_detect.locate_plate(crop)
    if result is not None:
        _, (x1, y1, x2, y2) = result
        aspect = (x2 - x1) / max(1.0, (y2 - y1))
        assert aspect >= 1.8, f"located a region with plate-implausible aspect {aspect:.2f}"


def test_output_is_ocr_ready_and_upscaled():
    """Small plate crops get upscaled first, glyph height drives accuracy."""
    # A small but realistically-proportioned plate (160x24 -> aspect ~6.7,
    # about what a full 10-glyph Indian plate's text extent measures).
    crop = _vehicle_crop_with_plate(plate_h=24)
    plate_image, _ = plate_detect.locate_plate(crop)
    assert plate_image.shape[0] >= settings.plate_ocr_target_height
    assert plate_image.ndim == 2, "OCR input is contrast-normalized grayscale"


def test_missing_configured_plate_model_falls_back_without_raising(monkeypatch):
    """Configured but missing weights (not bundled) fall back to classical,
    no crash and no pretending a model ran."""
    plate_detect._get_plate_model.cache_clear()
    monkeypatch.setattr(settings, "plate_model_name", "definitely-not-a-real-model.pt")
    try:
        assert plate_detect._get_plate_model() is None
        assert plate_detect.locate_plate(_vehicle_crop_with_plate()) is not None
    finally:
        plate_detect._get_plate_model.cache_clear()


@pytest.mark.parametrize("plate_y", [150, 200, 240])
def test_finds_plates_at_varying_heights_on_the_vehicle(plate_y):
    """Low plates score higher but high ones aren't rejected; trucks and
    buses carry them well above the bumper."""
    assert plate_detect.locate_plate(_vehicle_crop_with_plate(plate_y=plate_y)) is not None
