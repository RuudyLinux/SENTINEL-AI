"""Backward-compatible facade over `plate_detector` + `plate_preprocess`.

The plate-localization logic moved to `plate_detector` (candidate regions with
explicit confidence and provenance) and `plate_preprocess` (perspective
correction and preprocessing variants). This module stays because it is the
published import path — `tools/anpr_bench.py`, `tools/live_detect_probe.py` and
the existing test suite all call `plate_detect.locate_plate` — and breaking a
working contract to rename a module buys nothing.

Everything here delegates. No localization logic lives in this file.
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
    """Upscale + contrast-normalize a plate region before OCR.

    Preserved verbatim in behavior (upscale to `plate_ocr_target_height`, then
    grayscale + CLAHE) — it is now expressed as the `clahe` preprocessing
    variant, which is the production default precisely so that this path is
    unchanged.
    """
    variants = plate_preprocess.build_variants(plate_crop, quad=None, variant_names=("clahe",))
    return variants[0][1] if variants else plate_crop


def locate_plate(vehicle_crop: np.ndarray) -> "tuple[np.ndarray, list[float]] | None":
    """Find the plate inside a vehicle crop.

    Returns `(ocr_ready_plate_image, [x1, y1, x2, y2])` with the bbox in the
    vehicle crop's own coordinate space, or None when no plausible plate region
    was found (caller falls back to whole-crop OCR).

    Kept as the single-best-box API the pre-existing callers expect.
    `plate_detector.detect_plates` is the richer interface for new code.
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
