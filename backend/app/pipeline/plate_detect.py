"""Old entry point, delegating to plate_detector + plate_preprocess.

Kept because tools/anpr_bench.py, tools/live_detect_probe.py and the tests
call plate_detect.locate_plate. No logic of its own.
"""
import numpy as np

from . import plate_detector, plate_preprocess
from .plate_detector import (  # noqa: F401  (re-exported for existing callers/tests)
    MAX_AREA_FRACTION as _MAX_AREA_FRACTION,
    MAX_ASPECT as _MAX_ASPECT,
    MIN_AREA_FRACTION as _MIN_AREA_FRACTION,
    MIN_ASPECT as _MIN_ASPECT,
    MIN_PLATE_HEIGHT_PX as _MIN_PLATE_HEIGHT_PX,
    MIN_PLATE_WIDTH_PX as _MIN_PLATE_WIDTH_PX,
    get_plate_model as _get_plate_model,
)


def _preprocess_for_ocr(plate_crop: np.ndarray) -> np.ndarray:
    """Upscale + contrast-normalize a plate before OCR. Same behaviour as
    before (upscale, gray, CLAHE), now just the default `clahe` variant."""
    variants = plate_preprocess.build_variants(plate_crop, quad=None, variant_names=("clahe",))
    return variants[0][1] if variants else plate_crop


def locate_plate(vehicle_crop: np.ndarray) -> "tuple[np.ndarray, list[float]] | None":
    """Find the plate in a vehicle crop.

    (ocr_ready_image, [x1, y1, x2, y2]) in crop coordinates, or None (caller
    reads the whole crop). Single best box for old callers;
    plate_detector.detect_plates is the richer one.
    """
    boxes = plate_detector.detect_plates(vehicle_crop)
    if not boxes:
        return None
    box = boxes[0]
    plate_crop = plate_detector.crop_plate(vehicle_crop, box)
    if plate_crop is None:
        return None
    quad = plate_detector.quad_in_crop(box, vehicle_crop)
    variants = plate_preprocess.build_variants(plate_crop, quad=quad, variant_names=("clahe",))
    if not variants:
        return None
    padded_x1 = max(0, box.x1 - max(2, int(box.width * 0.04)))
    padded_y1 = max(0, box.y1 - max(2, int(box.height * 0.12)))
    crop_h, crop_w = vehicle_crop.shape[:2]
    padded_x2 = min(crop_w, box.x2 + max(2, int(box.width * 0.04)))
    padded_y2 = min(crop_h, box.y2 + max(2, int(box.height * 0.12)))
    return variants[0][1], [float(padded_x1), float(padded_y1), float(padded_x2), float(padded_y2)]
